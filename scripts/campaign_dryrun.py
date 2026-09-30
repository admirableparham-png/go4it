"""Zero-send dry run of a campaign (Phase 11). Renders EVERY remaining message with the SAME function the sender uses
(app/campaign_render.render_campaign_message) and checks it — without sending or writing anything.

    docker exec go4it-app python scripts/campaign_dryrun.py <campaign_id> [--preview] [--samples 3]
        [--exclude-countries US,MX] [--seed 1]

--preview checks the audience that WOULD be enrolled (scoped to the campaign's request) instead of the enrolled
recipients. The network is LOCKED (SMTP / IMAP / sockets / Telegram raise) and the database is opened READ-ONLY.
Output: counts per country and per email, errors by type (lead ids only), a schedule estimate and sample renders.
Exit 1 on any error.
"""
import argparse
import os
import random
import smtplib
import socket
import sys
from collections import Counter, defaultdict

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from sqlmodel import Session, create_engine, select                            # noqa: E402

from app import campaign_render as CR                                          # noqa: E402
from app import campaign_service as CAMP                                       # noqa: E402
from app import outreach_events as OE                                          # noqa: E402
from app import suppression as SUP                                             # noqa: E402
from app.models import Campaign, CampaignRecipient, Lead, MailAccount          # noqa: E402

NET_ATTEMPTS = []


def lock_network(patch=None):
    """Make every way out of this process raise (and count the attempt). `patch(obj, name, value)` lets tests use
    monkeypatch so the lock is undone afterwards; by default it is permanent for this process."""
    import imaplib
    from app import telegram

    def _blocked(what):
        def fn(*a, **k):
            NET_ATTEMPTS.append(what)
            raise RuntimeError(f"network locked by campaign_dryrun ({what})")
        return fn
    setter = patch or setattr
    for obj, name in ((smtplib, "SMTP"), (smtplib, "SMTP_SSL"), (smtplib, "LMTP"), (imaplib, "IMAP4"),
                      (imaplib, "IMAP4_SSL"), (socket, "create_connection"), (telegram, "send_message")):
        setter(obj, name, _blocked(f"{obj.__name__}.{name}"))
    setter(socket.socket, "connect", _blocked("socket.connect"))


def readonly_engine():
    from scripts.migrate import _db_path
    path = _db_path()
    if not path:
        raise SystemExit("campaign_dryrun needs the SQLite database file")
    return create_engine(f"sqlite:///file:{path}?mode=ro&uri=true", connect_args={"check_same_thread": False})


def _targets(session, campaign, preview):
    """(lead, to_email, first_step_index, sequence_version) for every recipient the check covers — each enrolled
    buyer is checked against the sequence VERSION they will actually be sent (a running edit makes a new one)."""
    if preview:
        f = {"request_id": campaign.request_id or ""}
        prev = CAMP.audience_preview(session, campaign, f)
        out = []
        for lid in prev["eligible_lead_ids"]:
            ld = session.get(Lead, lid)
            out.append((ld, SUP.normalize_email(ld.email), 0, campaign.sequence_version))
        return out
    rows = session.exec(select(CampaignRecipient).where(
        CampaignRecipient.campaign_id == campaign.id,
        CampaignRecipient.status.not_in(CAMP.TERMINAL_RECIPIENT)).order_by(CampaignRecipient.id)).all()
    return [(session.get(Lead, r.lead_id) if r.lead_id else None, r.to_email, r.current_step, r.sequence_version)
            for r in rows]


def _quoted_replies(subject, text):
    quoted = "\n".join("> " + ln for ln in text.splitlines())
    attribution = "Thanks, please send prices.\n\nOn Mon, 5 Oct 2026 at 10:00, Sender <s@x> wrote:\n"
    return [("Re: " + subject, attribution + quoted),
            ("Re: " + subject, attribution + text),                       # a client that quotes without '>'
            ("RE: " + subject, "Thanks, please send prices.\n\nFrom: Sender <s@x>\nSent: Monday, 5 October 2026\n"
             "Subject: " + subject + "\n\n" + text)]


def check(session, campaign, preview=False, exclude_countries=(), samples=0, seed=1):
    """Pure read. Returns {errors: {type: [lead ids]}, warnings: [...], counts, per_step, messages, samples}."""
    from app.outreach import mail_decrypt
    errors, warnings = defaultdict(list), []
    for p in CAMP.start_problems(session, campaign):
        errors[f"campaign: {p}"].append(0)
    mb = session.get(MailAccount, campaign.mailbox_id) if campaign.mailbox_id else None
    if mb is not None:
        try:
            if not mail_decrypt(mb.smtp_password_enc):
                errors["mailbox: no usable stored App Password"].append(0)
        except Exception:  # noqa: BLE001
            errors["mailbox: stored App Password can't be decrypted"].append(0)
        if mb.daily_limit < campaign.daily_limit:
            warnings.append(f"mailbox daily limit {mb.daily_limit} is below the campaign's {campaign.daily_limit}")
    versions = {}
    excluded = {c.strip().upper() for c in exclude_countries if c.strip()}
    seen, per_country, per_step, rendered = set(), Counter(), Counter(), []
    others = {r.to_email for r in session.exec(select(CampaignRecipient).join(
        Campaign, Campaign.id == CampaignRecipient.campaign_id).where(
        Campaign.status == "running", Campaign.id != campaign.id)).all()}
    for ld, to, first, version in _targets(session, campaign, preview):
        if version not in versions:
            versions[version] = CAMP.steps_for(session, campaign, version)
        steps = versions[version]
        if first >= len(steps):
            warnings.append(f"lead {getattr(ld, 'id', 0)}: no emails left (the worker will mark it completed)")
            continue
        lid = getattr(ld, "id", 0)
        iso = (getattr(ld, "dest_country", "") or "??").upper()
        per_country[iso] += 1
        if ld is None:
            errors["recipient has no buyer record"].append(lid)
            continue
        if not ld.managed or ld.owner_id is not None or ld.seller_id != campaign.tenant_id \
                or (campaign.request_id and ld.request_id != campaign.request_id):
            errors["buyer is not this request's confidential managed buyer"].append(lid)
        if not CAMP._EMAIL_RE.match(to or ""):
            errors["invalid email address"].append(lid)
        if to in seen:
            errors["duplicate address"].append(lid)
        seen.add(to)
        if to in others:
            warnings.append(f"lead {lid} is also in another running campaign")
        if iso in excluded:
            errors[f"excluded country {iso}"].append(lid)
        if SUP.is_suppressed(session, to, tenant_id=campaign.tenant_id):
            errors["on the do-not-contact list"].append(lid)
        for st in steps[first:]:
            per_step[st.step_index + 1] += 1
            if mb is None:
                continue
            msg = CR.render_campaign_message(session, campaign, st, ld, mb)
            if not msg["ok"]:
                errors[f"email {st.step_index + 1} blocked ({msg['scope']}): {msg['error'][:120]}"].append(lid)
                continue
            html_text = CR.html_to_text(msg["html"])
            if CR.OPT_OUT_LINE not in msg["text"] or CR.OPT_OUT_LINE not in html_text:
                errors["footer missing from a part"].append(lid)
            if not msg["headers"].get("List-Unsubscribe", "").startswith("<mailto:"):
                errors["List-Unsubscribe header missing"].append(lid)
            if len(msg["subject"]) > 120:
                warnings.append(f"lead {lid}: email {st.step_index + 1} subject is {len(msg['subject'])} chars")
            everything = " ".join([msg["subject"], msg["text"], msg["html"], mb.from_name or "", mb.email,
                                   *msg["headers"].values()])
            if CR.INTERNAL_BRAND.search(everything):
                errors["internal platform name visible to the buyer"].append(lid)
            for subj, body in _quoted_replies(msg["subject"], msg["text"]):
                own = OE.reply_text(body)
                if OE.is_unsubscribe(subj, own) or OE.is_auto_reply(subj, own):
                    errors[f"email {st.step_index + 1}: a normal reply quoting it would be misread as an "
                           "unsubscribe/auto-reply"].append(lid)
                    break
            rendered.append((lid, st.step_index + 1, msg))
    per_day = max(1, min(getattr(mb, "daily_limit", 0) or 0, campaign.daily_limit) or 1)
    first_emails = per_step.get(1, 0)
    rng = random.Random(seed)
    picks = rng.sample(rendered, min(samples, len(rendered))) if samples else []
    return {"errors": dict(errors), "warnings": warnings, "per_country": dict(per_country),
            "per_step": dict(per_step), "messages": len(rendered), "recipients": len(seen),
            "days_for_first_email": -(-first_emails // per_day), "per_day": per_day, "samples": picks}


def print_report(c, rep):
    print(f"campaign #{c.id} {c.name!r} · status {c.status} · request {c.request_id} · {rep['recipients']} recipients"
          f" · {rep['messages']} messages rendered")
    print("per country: " + ", ".join(f"{k} {v}" for k, v in sorted(rep["per_country"].items(), key=lambda x: -x[1])))
    print("per email:   " + ", ".join(f"#{k} {v}" for k, v in sorted(rep["per_step"].items())))
    print(f"schedule: first email to everyone in ~{rep['days_for_first_email']} sending day(s) at {rep['per_day']}/day")
    for w in sorted(set(rep["warnings"]))[:30]:
        print(f"WARNING  {w}")
    if rep["errors"]:
        for k, ids in sorted(rep["errors"].items(), key=lambda x: -len(x[1])):
            ex = ", ".join(str(i) for i in ids[:5] if i)
            print(f"ERROR    {len(ids):>4} × {k}" + (f"  (lead ids: {ex})" if ex else ""))
    for lid, n, msg in rep["samples"]:
        print(f"\n--- sample: lead {lid}, email {n} ---")
        print(f"Subject: {msg['subject']}")
        for k, v in msg["headers"].items():
            print(f"{k}: {v}")
        print("\n".join(msg["text"].splitlines()[:30]))
        print(f"[HTML part: {len(msg['html'].encode('utf-8'))} bytes]")


def main(argv=None):
    ap = argparse.ArgumentParser(description="zero-send campaign dry run")
    ap.add_argument("campaign_id", type=int)
    ap.add_argument("--preview", action="store_true", help="check the audience that WOULD be enrolled")
    ap.add_argument("--samples", type=int, default=3)
    ap.add_argument("--exclude-countries", default="")
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args(argv)
    lock_network()
    eng = readonly_engine()
    with Session(eng, autoflush=False) as s:
        c = s.get(Campaign, a.campaign_id)
        if not c:
            print("campaign not found")
            return 2
        rep = check(s, c, a.preview, a.exclude_countries.split(","), a.samples, a.seed)
        print_report(c, rep)
        s.rollback()
    n_err = sum(len(v) for v in rep["errors"].values())
    print(f"\nRESULT: {'PASS — 0 errors' if not n_err else f'FAIL — {n_err} error(s)'} · nothing was sent · "
          f"network attempts: {len(NET_ATTEMPTS)}")
    return 1 if n_err else 0


if __name__ == "__main__":
    sys.exit(main())
