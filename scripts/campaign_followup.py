"""Add a follow-up email to a LIVE campaign, held until the founder approves it — Phase 12. Dry run unless --apply.

    docker exec go4it-app python scripts/campaign_followup.py add 33 \
        --template campaigns/trsharks-anchors-followup --delay-days 7 --hold [--replace] [--apply]
    docker exec go4it-app python scripts/campaign_followup.py reopen 33 [--apply]
    docker exec go4it-app python scripts/campaign_followup.py approve 33 --email 2 [--apply]

add      appends the email to the END of the campaign's current sequence version, in place: email 1 and everything
         already sent stay exactly as they are, and every buyer keeps their version, so email 1 can never go out twice.
         Buyers who had already finished are re-opened, each due at THEIR OWN last email + --delay-days. --hold keeps
         the new email waiting (it uses no send slot) until 'approve'. With --apply it refuses while a send is in
         flight, pauses the campaign (no task is raised), appends, re-opens, re-checks everything the start button
         checks and resumes it. Any failure leaves the campaign PAUSED and says so. Run it outside the sending window.
         --replace changes the text of the last email instead (a revised draft) — only while it is held and never
         sent; it stays held and keeps its delay. A changed text never queues behind a held draft.
reopen   re-opens buyers who finished before the follow-up existed ('add' already does this; safe to repeat).
approve  releases a held email. From the worker's next cycle each buyer gets it when due — as a reply in their own
         thread (In-Reply-To/References + "Re: <the subject their first email had>") — and never after a reply, a
         bounce or an unsubscribe.

This script never sends anything (its network is locked); the worker sends.
"""
import argparse
import os
import sys
import time
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from sqlmodel import Session, func, select                                     # noqa: E402

from app import campaign_render as CR                                          # noqa: E402
from app import campaign_service as CAMP                                       # noqa: E402
from app import pipeline                                                       # noqa: E402
from app import send_guard as SG                                               # noqa: E402
from app.db import engine                                                      # noqa: E402
from app.models import Campaign, CampaignRecipient, CampaignSend, Lead, MailAccount  # noqa: E402
from scripts import campaign_setup as SETUP                                    # noqa: E402

IN_FLIGHT = ("claimed", "sending")
# a worker cycle that started before the pause may still hold the old 'running' status in memory — it ends within
# its deadline, so waiting that long (only inside the sending window) means no send can start after the append
SETTLE_SECONDS = int(os.getenv("CAMPAIGN_SEND_DEADLINE_SEC", "60")) + 5


def _in_flight(s, cid) -> int:
    return s.exec(select(func.count()).where(CampaignSend.campaign_id == cid,
                                              CampaignSend.status.in_(IN_FLIGHT))).one()


def _settle(s, c) -> bool:
    """After the pause: let a worker cycle already under way finish, then require that nothing is mid-send. A campaign
    in buyer-local hours may be sending at any UTC hour, so it always waits."""
    if SETTLE_SECONDS > 0 and ((c.local_hours or "").strip() or SG.within_window(c, datetime.utcnow())):
        print(f"inside the sending window — waiting {SETTLE_SECONDS}s for the worker's current cycle to end …")
        time.sleep(SETTLE_SECONDS)
    for _ in range(30):
        if not _in_flight(s, c.id):
            return True
        time.sleep(2)
    return False


def _header(s, c):
    timing = (f"buyer-local hours {c.local_hours}" if (c.local_hours or "").strip()
              else f"window {c.send_window_start}-{c.send_window_end} UTC days {c.send_days}")
    print(f"campaign #{c.id} {c.name!r} · {c.status} · sequence v{c.sequence_version} · {c.daily_limit}/day · {timing}")
    for st in CAMP.steps_for(s, c):
        held = ("HELD — starts by itself once every earlier email is out"
                if (st.release_when or "") == CAMP.RELEASE_EARLIER_DONE else "HELD")
        print(f"  email {st.step_index + 1}: {st.subject!r} · {st.delay_days} day(s) after the previous · "
              f"{held if st.manual_review else 'approved'}"
              + (f" · attachment {os.path.basename(st.attachment_path)}" if st.attachment_path else ""))


def _plan_report(plan, delay_days=None):
    print(f"re-open: {plan['eligible']} buyer(s) who had finished get it" +
          (f", each {delay_days} day(s) after their own last email" if delay_days is not None else ""))
    if plan["due"]:
        ats = sorted(at for _rid, at in plan["due"])
        print(f"  first due {ats[0]:%a %Y-%m-%d %H:%M} UTC · last due {ats[-1]:%a %Y-%m-%d %H:%M} UTC "
              f"(it goes out in the sending hours, within the daily limit)")
    for why, n in sorted(plan["left"].items()):
        print(f"  left alone: {n} × {why}")
    if plan.get("error"):
        print(f"  {plan['error']}")


def _sample(s, c, step, rcpt_ids):
    """Print ONE real message of `step` for a buyer who has a sent email — threaded exactly as the worker will send
    it. (A buyer whose details can't be rendered is skipped by the worker on its own, like for email 1.)"""
    mb = s.get(MailAccount, c.mailbox_id) if c.mailbox_id else None
    if not rcpt_ids:
        rcpt_ids = s.exec(select(CampaignSend.recipient_id).where(
            CampaignSend.campaign_id == c.id, CampaignSend.status == "sent").order_by(CampaignSend.id).limit(20)).all()
    for rid in rcpt_ids:
        r = s.get(CampaignRecipient, rid)
        anchor = CAMP.thread_anchor(s, c, r) if r else {}
        if not anchor:
            continue
        ld = s.get(Lead, r.lead_id) if r.lead_id else None
        msg = CR.render_campaign_message(s, c, step, ld, mb, thread_subject=anchor["subject"])
        print(f"\n--- sample: recipient {r.id} (lead {r.lead_id}), email {step.step_index + 1} ---")
        if not msg["ok"]:
            print(f"this buyer would be skipped ({msg['scope']}): {msg['error']}")
            return
        print(f"Subject: {msg['subject']}")
        print(f"In-Reply-To: {anchor['in_reply_to']}")
        print(f"References: {anchor['references']}")
        for k, v in msg["headers"].items():
            print(f"{k}: {v}")
        print("\n".join(msg["text"].splitlines()[:30]))
        print(f"[HTML part: {len(msg['html'].encode('utf-8'))} bytes]"
              + "".join(f" [attachment: {n}]" for n, _d in msg.get("attachments") or []))
        print("---")
        return
    print("\n(no buyer has a sent email yet — no sample)")


def _stopped(c, was_running, why) -> int:
    if was_running:
        print(f"\nCAMPAIGN #{c.id} IS PAUSED — {why}\nFix it, then resume it on /campaigns/{c.id} (the start button "
              "re-checks everything) or re-run this command.")
    else:
        print(f"\nFAILED — {why} (campaign #{c.id} stays {c.status})")
    return 1


def cmd_add(s, c, a) -> int:
    step, errs = SETUP.load_template(a.template)
    if errs:
        print("TEMPLATE PROBLEMS:\n  - " + "\n  - ".join(errs))
        return 2
    if a.delay_days < 1:
        print("REFUSED: --delay-days must be at least 1 (a follow-up right after email 1 is not a follow-up)")
        return 2
    held = a.hold or a.replace                            # a replaced draft always stays held
    step.update(delay_days=a.delay_days, manual_review=held)
    _header(s, c)
    if c.status in CAMP.FINAL_CAMPAIGN:
        print(f"REFUSED: the campaign is {c.status}")
        return 2
    steps = CAMP.steps_for(s, c)
    if not steps:
        print("REFUSED: the campaign has no first email yet — set it up with scripts/campaign_setup.py")
        return 2
    same = CAMP.same_email(steps, CAMP.build_step(c, c.sequence_version, len(steps), step))
    if same is not None:
        print(f"\nemail {same.step_index + 1} already has this text — it is not added again "
              f"({'HELD' if same.manual_review else 'approved'}, {same.delay_days} day(s))")
        plan, shown = CAMP.reopen_completed(s, c), same
    else:                   # exactly the checks --apply will make, so a refusal never leaves a live campaign paused
        chk = (CAMP.replace_held_step if a.replace else CAMP.append_step)(s, c, step, apply=False)
        if chk["error"]:
            print(f"REFUSED: {chk['error']}")
            return 2
        shown = CAMP.build_step(c, c.sequence_version, chk["step_index"], dict(step, manual_review=held))
        att = f"attachment {os.path.basename(shown.attachment_path)}" if shown.attachment_path else "no attachment"
        print(f"\n{'replace the text of' if a.replace else 'add'} email {shown.step_index + 1}: {shown.subject!r} · "
              f"{shown.delay_days} day(s) after email {shown.step_index} · "
              f"{'HELD until approve' if held else 'NOT held'} · {att} · "
              f"List-Unsubscribe {'yes' if shown.list_unsubscribe else 'no'}")
        if not held:
            print("WARNING: not held — it goes out automatically when due. Add --hold to keep it until 'approve'.")
        plan = CAMP.reopen_completed(s, c, steps=None if a.replace else list(steps) + [shown])
    waiting = s.exec(select(func.count()).where(
        CampaignRecipient.campaign_id == c.id, CampaignRecipient.status.not_in(CAMP.TERMINAL_RECIPIENT),
        CampaignRecipient.current_step < shown.step_index)).one()
    _plan_report(plan, shown.delay_days)
    print(f"{waiting} buyer(s) still before it get it {shown.delay_days} day(s) after their own "
          f"email {shown.step_index}")
    _sample(s, c, shown, [rid for rid, _at in plan["due"]])
    busy = _in_flight(s, c.id)
    was_running = c.status == "running"
    problems = CAMP.start_problems(s, c) if was_running else []
    if busy:
        print(f"\n{busy} send(s) of this campaign are in flight (claimed/sending) — wait a minute and re-run.")
    for p in problems:
        print(f"START PROBLEM (it could not be resumed afterwards): {p}")
    if not a.apply:
        print("\nDRY RUN — nothing changed. Re-run with --apply"
              + (" outside the sending window." if was_running and SG.within_window(c, datetime.utcnow()) else "."))
        return 1 if (busy or problems) else 0
    if busy or problems:
        print("REFUSED — nothing changed.")
        return 1
    baseline = c.bounce_baseline
    if was_running:
        CAMP.transition(s, c, "paused", None, "")            # empty reason: a technical pause raises no task
        s.commit()
        print(f"\npaused campaign #{c.id}")
        if not _settle(s, c):
            return _stopped(c, True, "a send was still in flight after the pause — the sequence was not changed")
    try:
        if same is None:
            res = (CAMP.replace_held_step if a.replace else CAMP.append_step)(s, c, step)
            if res["error"]:
                return _stopped(c, was_running, f"not changed: {res['error']}")
            print(f"{'REPLACED the text of' if a.replace else 'ADDED'} email {res['step_index'] + 1} in sequence "
                  f"v{c.sequence_version}" + (" — HELD until approve" if held else ""))
        reo = CAMP.reopen_completed(s, c, apply=True)
        print(f"re-opened {reo['reopened']} buyer(s) who had finished")
    except Exception as e:  # noqa: BLE001 — whatever happened, the campaign must not be resumed blind
        s.rollback()
        return _stopped(c, was_running, f"error: {e}"[:300])
    if not was_running:
        print(f"campaign #{c.id} stays {c.status} (it was not running) — start it on /campaigns/{c.id} when ready")
        return 0
    problems = CAMP.start_problems(s, c)
    if problems:
        return _stopped(c, True, "it can't start: " + "; ".join(problems))
    CAMP.transition(s, c, "running", None, "resumed by campaign_followup")
    c.bounce_baseline = baseline                    # a technical pause: the bounce breaker keeps judging from the start
    s.add(c); s.commit()
    print(f"RESUMED campaign #{c.id}." + (" The new email waits for: campaign_followup.py approve "
                                         f"{c.id} --email {shown.step_index + 1} --apply" if held else ""))
    return 0


def cmd_reopen(s, c, a) -> int:
    _header(s, c)
    plan = CAMP.reopen_completed(s, c)
    _plan_report(plan)
    if plan.get("error"):
        return 2
    if not a.apply:
        print("\nDRY RUN — nothing changed. Re-run with --apply.")
        return 0
    res = CAMP.reopen_completed(s, c, apply=True)
    print(f"re-opened {res['reopened']} buyer(s)")
    if c.status != "running":
        print(f"note: campaign #{c.id} is {c.status} — nothing is sent until it runs")
    return 0


def cmd_approve(s, c, a) -> int:
    _header(s, c)
    idx = a.email - 1
    st = next((x for x in CAMP.steps_for(s, c) if x.step_index == idx), None)
    if st is None:
        print(f"REFUSED: there is no email {a.email} in sequence v{c.sequence_version}")
        return 2
    now = datetime.utcnow()
    waiting = s.exec(select(CampaignRecipient).where(
        CampaignRecipient.campaign_id == c.id, CampaignRecipient.status.not_in(CAMP.TERMINAL_RECIPIENT),
        CampaignRecipient.sequence_version == c.sequence_version, CampaignRecipient.current_step == idx)
        .order_by(CampaignRecipient.id)).all()
    due = [r for r in waiting if r.next_action_at is None or r.next_action_at <= now]
    print(f"\nemail {a.email}: {st.subject!r} · {'HELD' if st.manual_review else 'approved'} · {len(waiting)} buyer(s) "
          f"waiting for it, {len(due)} already due (they go out within the window and the daily limit)")
    _sample(s, c, st, [r.id for r in waiting[:20]])
    if not st.manual_review:
        print(f"email {a.email} is already approved — nothing to do")
        return 0
    if a.when_earlier_done:
        left = CAMP.earlier_emails_left(s, c, idx)
        print(f"\nemail {a.email} stays HELD and starts by itself once no buyer can still get an earlier email "
              f"({left if left <= 50 else 'more than 50'} buyer(s) still before it now)")
        if not a.apply:
            print("\nDRY RUN — nothing changed. Re-run with --apply.")
            return 0
        st.release_when = CAMP.RELEASE_EARLIER_DONE
        s.add(st)
        pipeline.audit(s, None, "campaign", c.id, "release_rule",
                       {"step_index": idx, "release_when": st.release_when}, tenant_id=c.tenant_id)
        s.commit()
        print(f"SET: email {a.email} starts by itself once every earlier email is out (the worker checks every cycle "
              "and sends a Telegram message when it starts)")
        return 0
    if not a.apply:
        print("\nDRY RUN — nothing changed. Re-run with --apply to release it.")
        return 0
    ok, msg = CAMP.approve_step(s, c, idx)
    print(("APPROVED: " if ok else "REFUSED: ") + msg)
    if ok and c.status != "running":
        print(f"note: campaign #{c.id} is {c.status} — nothing is sent until it runs")
    return 0 if ok else 2


def main(argv=None):
    ap = argparse.ArgumentParser(description="add / re-open / approve a campaign follow-up (dry run unless --apply)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("add", help="append a follow-up email to the campaign's sequence")
    p.add_argument("campaign_id", type=int)
    p.add_argument("--template", required=True, help="template folder (subject.txt, body.txt, optional body.html)")
    p.add_argument("--delay-days", type=int, required=True, help="days after each buyer's previous email")
    p.add_argument("--hold", action="store_true", help="hold it until 'approve' (recommended)")
    p.add_argument("--replace", action="store_true",
                   help="change the text of the last email instead — only while it is held and never sent")
    p.add_argument("--apply", action="store_true")
    p = sub.add_parser("reopen", help="give the follow-up to buyers who had already finished")
    p.add_argument("campaign_id", type=int)
    p.add_argument("--apply", action="store_true")
    p = sub.add_parser("approve", help="release a held email")
    p.add_argument("campaign_id", type=int)
    p.add_argument("--email", type=int, required=True, help="which email (2 = the first follow-up)")
    p.add_argument("--when-earlier-done", action="store_true",
                   help="don't release now: start it by itself once every earlier email (e.g. every first email) is out")
    p.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    with Session(engine) as s:
        c = s.get(Campaign, a.campaign_id)
        if c is None:
            print(f"campaign #{a.campaign_id} not found")
            return 2
        return {"add": cmd_add, "reopen": cmd_reopen, "approve": cmd_approve}[a.cmd](s, c, a)


if __name__ == "__main__":
    from scripts.campaign_dryrun import lock_network
    lock_network()                     # nothing here sends — and nothing it touches can
    sys.exit(main())
