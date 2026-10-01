"""Set up a campaign from a template folder (subject.txt, body.txt, optional body.html) — Phase 11.

SMOKE test (real sends to the founder's own inboxes, rendered with real buyer names/countries):
    docker exec go4it-app python scripts/campaign_setup.py --template campaigns/trsharks-anchors \
        --mailbox info@qmatalsaha.com \
        --smoke "you+1@gmail.com|IHL Canada (Investments Hardware Ltd.)|CA" \
        --smoke "you+2@gmail.com|Inoxa Sp. z o.o.|PL" --start
    add --followup campaigns/trsharks-anchors-followup [--followup-delay 0] for a 2-email smoke: email 2 goes out as a
    reply in each inbox's thread (with delay 0, on the worker's next cycle after email 1).

REAL campaign for a request (first run shows the audience; add --enrol, then --start once approved):
    docker exec go4it-app python scripts/campaign_setup.py --template campaigns/trsharks-anchors \
        --mailbox info@qmatalsaha.com --request SR-202608-0001 --name "TRSHARKS anchors" --daily-limit 10 \
        [--campaign <id>] [--enrol] [--start]
    --daily-limit sets a NEW campaign's limit (default 10), or a draft's when given. A campaign that is running, has
    sent or follows a warm-up plan keeps its limit — change that on the campaign page.

Smoke buyers live on their own inactive "smoke seller" request, so no real seller ever sees them. The smoke campaign
may send at any hour; a real campaign sends Mon–Fri 08–18 UTC within its daily limit. Nothing is sent by this script:
the worker sends once the campaign is running (and outreach Pause-All is off). A campaign that is running or has
already sent keeps its sequence untouched — a follow-up is added to it with scripts/campaign_followup.py.
"""
import argparse
import os
import secrets
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from sqlmodel import Session, func, select                                     # noqa: E402

from app import campaign_render as CR                                          # noqa: E402
from app import campaign_service as CAMP                                       # noqa: E402
from app import pipeline                                                       # noqa: E402
from app.auth import hash_password                                             # noqa: E402
from app.db import engine, init_db                                             # noqa: E402
from app.models import (Campaign, CampaignRecipient, CampaignSend, Lead, MailAccount, ServiceRequest,  # noqa: E402
                        StageEvent, User, UserProfile)

DEFAULT_DAILY_LIMIT = 10
SMOKE_SELLER = "smoke-seller@qmatalsaha.com"
SMOKE_REQUEST = "SMOKE-TEST"


def load_template(folder):
    path = folder if os.path.isabs(folder) else os.path.join(BASE, folder)

    def read(name):
        p = os.path.join(path, name)
        return open(p, encoding="utf-8").read() if os.path.exists(p) else ""
    step = {"subject": read("subject.txt").strip().splitlines()[0] if read("subject.txt").strip() else "",
            "body": read("body.txt").strip(), "body_html": read("body.html").strip(), "delay_days": 0}
    for line in read("options.txt").splitlines():      # e.g. plain_text_only=yes / list_unsubscribe=no
        k, _, v = line.partition("=")
        if k.strip() in ("plain_text_only", "list_unsubscribe"):
            step[k.strip()] = v.strip().lower() in ("yes", "true", "1", "on")
    pdfs = sorted(f for f in os.listdir(path) if f.lower().endswith(".pdf")) if os.path.isdir(path) else []
    errs = CR.validate_step(step["subject"], step["body"], step["body_html"])
    if pdfs:
        step["attachment_path"] = os.path.relpath(os.path.join(path, pdfs[0]), BASE)
        errs += [e for e in [CR.load_attachment(step["attachment_path"])[2]] if e]
    elif "attached" in step["body"].lower():
        errs.append("the text says a file is attached, but there is no PDF in the template folder")
    return step, errs


def find_mailbox(session, email):
    rows = session.exec(select(MailAccount).where(func.lower(MailAccount.email) == email.strip().lower())).all()
    rows.sort(key=lambda m: (not m.admin_owned, not m.active))
    return rows[0] if rows else None


def smoke_request(session):
    """The inactive smoke seller + its buyer-hunt request (created once, reused)."""
    seller = session.exec(select(User).where(User.email == SMOKE_SELLER)).first()
    if seller is None:
        seller = User(email=SMOKE_SELLER, name="Smoke Test Seller", role="agent", active=False,
                      password_hash=hash_password(secrets.token_urlsafe(32)))
        session.add(seller); session.flush()
        session.add(UserProfile(user_id=seller.id, account_class="seller", role_key="seller", scope="own",
                                account_status="disabled", full_name="Smoke Test Seller"))
    sr = session.exec(select(ServiceRequest).where(ServiceRequest.tracking_code == SMOKE_REQUEST)).first()
    if sr is None:
        sr = ServiceRequest(tracking_code=SMOKE_REQUEST, request_type="buyer_hunt", product="Smoke test",
                            status="running", owner_id=seller.id, requester_id=seller.id,
                            details="Internal smoke test — sends only to the founder's own addresses.")
        session.add(sr); session.flush()
    return sr


def smoke_leads(session, sr, specs):
    """specs: 'email|company|ISO'. One managed buyer per address (reused when the address is already there)."""
    out = []
    for spec in specs:
        parts = [p.strip() for p in spec.split("|")]
        if len(parts) != 3 or "@" not in parts[0]:
            raise SystemExit(f"--smoke must be 'email|company|ISO', got {spec!r}")
        email, company, iso = parts[0].lower(), parts[1], parts[2].upper()
        ld = session.exec(select(Lead).where(Lead.request_id == sr.id, Lead.email == email)).first()
        if ld is None:
            ld = Lead(source="smoke-test", external_id=f"smoke:{email}", product=sr.product, category="smoke",
                      owner_id=None, managed=True, seller_id=sr.owner_id, request_id=sr.id,
                      pipeline_stage="identified", buyer_company=company, dest_country=iso, email=email)
            session.add(ld); session.flush()
            pipeline.assign_anon_ref(session, ld)
            session.add(StageEvent(lead_id=ld.id, request_id=sr.id, from_stage="", to_stage="identified",
                                   note="smoke-test buyer"))
        else:
            ld.buyer_company, ld.dest_country = company, iso
            session.add(ld)
        out.append(ld)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="set up a campaign from a template folder")
    ap.add_argument("--template", required=True)
    ap.add_argument("--mailbox", required=True, help="the connected Go4it mailbox to send from")
    ap.add_argument("--smoke", action="append", default=[], help="'email|company|ISO' — a test buyer (repeatable)")
    ap.add_argument("--request", default="", help="SR code or id (real campaign)")
    ap.add_argument("--name", default="")
    ap.add_argument("--campaign", type=int, default=0, help="update this existing campaign instead of creating one")
    ap.add_argument("--daily-limit", type=int, default=None,
                    help=f"a new campaign's daily limit (default {DEFAULT_DAILY_LIMIT}); a live one keeps its own")
    ap.add_argument("--enrol", action="store_true", help="enrol the previewed audience")
    ap.add_argument("--start", action="store_true", help="start it if nothing blocks")
    ap.add_argument("--followup", default="",
                    help="SMOKE only: template folder of email 2, sent as a reply in the thread")
    ap.add_argument("--followup-delay", type=int, default=0, help="days between the smoke's email 1 and email 2")
    a = ap.parse_args(argv)
    if bool(a.smoke) == bool(a.request):
        ap.error("use either --smoke (test) or --request (real campaign)")
    if a.followup and not a.smoke:
        ap.error("--followup is for smoke tests — add a follow-up to a real campaign with scripts/campaign_followup.py")
    step, errs = load_template(a.template)
    seq = [step]
    if a.followup:
        fu, fu_errs = load_template(a.followup)
        fu["delay_days"] = max(0, a.followup_delay)
        errs += [f"follow-up: {e}" for e in fu_errs]
        seq.append(fu)
    if errs:
        print("TEMPLATE PROBLEMS:\n  - " + "\n  - ".join(errs))
        return 2
    init_db()
    with Session(engine) as s:
        mb = find_mailbox(s, a.mailbox)
        if mb is None or not mb.admin_owned:
            print(f"REFUSED: {a.mailbox} is not a connected Go4it-owned mailbox — connect it on /mail and tick "
                  "'Go4it-owned' first")
            return 2
        smoke = bool(a.smoke)
        if smoke:
            sr = smoke_request(s)
            smoke_leads(s, sr, a.smoke)
        else:
            ref = a.request.strip()
            sr = (s.get(ServiceRequest, int(ref)) if ref.isdigit() else
                  s.exec(select(ServiceRequest).where(ServiceRequest.tracking_code == ref)).first())
            if sr is None:
                print(f"REFUSED: request {ref} not found")
                return 2
        if a.campaign:
            c = s.get(Campaign, a.campaign)
            if c is None or c.request_id != sr.id:
                print("REFUSED: that campaign is not this request's")
                return 2
        else:
            name = a.name or (f"SMOKE — {os.path.basename(a.template.rstrip('/'))}" if smoke else sr.tracking_code)
            c = Campaign(name=name[:120], context_kind="request", request_id=sr.id, tenant_id=sr.owner_id,
                         owner_id=mb.user_id, mailbox_id=mb.id, status="draft")
            s.add(c); s.flush()
        c.mailbox_id = mb.id
        live = bool(a.campaign) and (c.status == "running" or s.exec(select(func.count()).where(
            CampaignSend.campaign_id == c.id)).one() > 0)
        if smoke:
            c.daily_limit = max(1, len(a.smoke))
        elif not a.campaign:
            c.daily_limit = max(0, DEFAULT_DAILY_LIMIT if a.daily_limit is None else a.daily_limit)
        elif a.daily_limit is not None:
            # the warm-up ramp raises a live campaign's limit day by day: a re-run (e.g. to enrol a later wave) must
            # never knock it back
            if live or (c.warmup_plan or "").strip():
                print(f"daily limit kept at {c.daily_limit}/day — campaign #{c.id} is {c.status}, has sent or follows "
                      "a warm-up plan (change it on the campaign page)")
            else:
                c.daily_limit = max(0, a.daily_limit)
        if smoke:                                          # a test goes out now, whatever the hour or day
            c.send_window_start, c.send_window_end, c.send_days = 0, 24, "0,1,2,3,4,5,6"
        s.add(c); s.commit(); s.refresh(c)
        # what was already sent stays as it was: re-running on a live campaign never resets (and so never deletes) its
        # emails — a follow-up is appended with scripts/campaign_followup.py
        if live:
            print(f"sequence kept — campaign #{c.id} is {c.status} or has already sent "
                  "(add a follow-up with scripts/campaign_followup.py)")
        else:
            CAMP.set_sequence(s, c, seq, None)
        per_buyer = max(1, len(CAMP.steps_for(s, c)))     # a smoke sends every email of the sequence to each address
        f = {"request_id": sr.id}
        prev = CAMP.audience_preview(s, c, f)
        if smoke:                                          # every test address goes out in this one run
            c.daily_limit = max(1, prev["final_eligible"]) * per_buyer
            s.add(c); s.commit()
        print(f"campaign #{c.id} {c.name!r} · request {sr.tracking_code} · mailbox {mb.email} · "
              f"{c.daily_limit}/day · window {c.send_window_start}-{c.send_window_end} UTC days {c.send_days}")
        print("audience: " + ", ".join(f"{k}={v}" for k, v in prev.items() if k != "eligible_lead_ids"))
        if a.enrol or smoke:
            res = CAMP.enroll(s, c, None, f, expected=prev["final_eligible"])
            if smoke:                                      # a test goes ONLY to the addresses given this run
                wanted = {sp.split("|")[0].strip().lower() for sp in a.smoke}
                for r in s.exec(select(CampaignRecipient).where(CampaignRecipient.campaign_id == c.id)).all():
                    if r.to_email not in wanted:
                        s.delete(r); res["created"] -= 1
                c.daily_limit = len(wanted) * per_buyer
                s.add(c); s.commit()
            print(f"enrolled: {res}")
        problems = CAMP.start_problems(s, c)
        if problems:
            print("NOT READY TO START:\n  - " + "\n  - ".join(problems))
        elif a.start:
            CAMP.transition(s, c, "running", None, "started by campaign_setup")
            s.commit()
            print("STARTED — the worker sends within its next cycle (outreach Pause-All must be off).")
        else:
            print("ready — re-run with --start (or use the campaign page) to start.")
        print(f"dry-run: docker exec go4it-app python scripts/campaign_dryrun.py {c.id}")
        return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
