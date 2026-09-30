"""Production canary — founder-only, allowlist-gated (Phase 4 hardening).

A controlled, minimal live-path exercise for ONE Go4it admin-owned mailbox. It never touches a production
campaign audience: live sends go ONLY to a configured allowlisted internal test address. The safety
MECHANISM checks (suppression blocks a resend, Pause-All stops the worker, sellers can see nothing) run in an
ISOLATED in-memory database so they never mutate production; only the live-send steps use the real mailbox.

    ./.venv/bin/python scripts/canary.py                 # run: mechanism checks always; live steps if configured

Enable the LIVE steps by setting, in the environment:
    CANARY_ENABLED=1
    CANARY_ALLOWLIST=you@yourdomain.com          # comma-separated permitted test recipients
    CANARY_TEST_RECIPIENT=you@yourdomain.com     # the internal test inbox (MUST be in the allowlist)
    CANARY_BAD_RECIPIENT=nouser@yourdomain.com   # a known-invalid address for the controlled-bounce test
…and connect a live admin-owned mailbox with valid SMTP credentials (see /mail).

If live credentials are unavailable the live canary is reported as **NOT RUN** — it is never simulated as a
success. See docs/CANARY_RUNBOOK.md for the exact live procedure.
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from sqlalchemy import text   # noqa: E402
from sqlalchemy.pool import StaticPool   # noqa: E402
from sqlmodel import Session, SQLModel, create_engine, select   # noqa: E402

from app import campaign_service as CAMP   # noqa: E402
from app import send_guard as SG   # noqa: E402
from app import suppression as SUP   # noqa: E402
from app.config import (CANARY_ALLOWLIST, CANARY_ENABLED, CANARY_TEST_RECIPIENT)   # noqa: E402
from app.models import (Campaign, CampaignRecipient, Lead, MailAccount, User)   # noqa: E402


# --------------------------------------------------------------------- isolated mechanism harness
def _isolated():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(eng)
    with eng.connect() as conn:
        for ddl in (
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_workitem_idem_open ON workitem(idempotency_key) "
            "WHERE idempotency_key != '' AND status IN ('open','in_progress','waiting')",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_outreach_campaign_send ON "
            "outreach(campaign_id,campaign_recipient_id,campaign_version,campaign_step) WHERE campaign_id IS NOT NULL",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_suppression_addr_scope ON "
            "suppression(email_normalized,scope,tenant_id) WHERE active = 1",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_campaignsend_crvs ON "
            "campaignsend(campaign_id,recipient_id,sequence_version,step_index)",
        ):
            conn.execute(text(ddl))
        conn.commit()
    with Session(eng) as s:
        s.add(User(email="admin@canary.local", name="Admin", role="admin", active=True, password_hash="x"))
        s.add(User(email="seller@canary.local", name="Seller", role="agent", active=True, password_hash="x"))
        s.commit()
        ids = {u.email.split("@")[0]: u.id for u in s.exec(select(User)).all()}
        mb = MailAccount(user_id=ids["admin"], email="canary@sender.example", admin_owned=True, active=True,
                         daily_limit=100, sender_company="Sender Trading LLC", postal_address="1 Test Street, Dubai")
        s.add(mb); s.commit(); s.refresh(mb)
        ids["mailbox"] = mb.id
    return eng, ids


def _running_campaign(s, ids):
    c = Campaign(name="canary", tenant_id=ids["seller"], owner_id=ids["admin"], mailbox_id=ids["mailbox"],
                 status="draft", sequence_version=1, daily_limit=100, send_days="0,1,2,3,4,5,6",
                 send_window_start=0, send_window_end=24)
    s.add(c); s.commit(); s.refresh(c)
    CAMP.set_sequence(s, c, [{"subject": "Hi", "body": "hello", "delay_days": 0}], None)
    c.status = "running"; s.add(c); s.commit(); s.refresh(c)
    return c


def _ok(mb, to, subject, text, html=None, reply_to="", in_reply_to="", message_id="", references="", **kw):
    return True, "", f"<mid-{to}>"


def check_suppression_blocks():
    eng, ids = _isolated()
    with Session(eng) as s:
        c = _running_campaign(s, ids)
        ld = Lead(product="x", managed=True, seller_id=ids["seller"], email="sup@x.com")
        s.add(ld); s.commit(); s.refresh(ld)
        SUP.suppress(s, "sup@x.com", "unsubscribe", None, scope="platform"); s.commit()
        r = CampaignRecipient(campaign_id=c.id, tenant_id=ids["seller"], lead_id=ld.id, to_email="sup@x.com",
                              sequence_version=1, current_step=0, status="pending")
        s.add(r); s.commit(); s.refresh(r)
        out = CAMP.send_step(s, c, r, s.get(MailAccount, ids["mailbox"]), sender=_ok)
        return out.get("reason") == "suppressed", f"send → {out.get('status')}/{out.get('reason')}"


def check_pause_all_stops_worker():
    import app.worker as worker
    eng, ids = _isolated()
    with Session(eng) as s:
        _running_campaign(s, ids)
        SG.set_pause_all(s, True, None); s.commit()
    old = worker.engine
    try:
        worker.engine = eng
        out = worker.run_campaign_send()
    finally:
        worker.engine = old
    return out.get("paused") is True, f"worker → {out}"


def check_sellers_see_nothing():
    from fastapi.testclient import TestClient
    import app.main as main
    eng, ids = _isolated()
    with Session(eng) as s:
        # seed a buyer + a suppressed address the seller must never see
        ld = Lead(product="copper", managed=True, seller_id=ids["seller"], email="secretbuyer@x.com")
        s.add(ld); s.commit(); s.refresh(ld)
        lid = ld.id
        for u in s.exec(select(User)).all():
            from app.auth import hash_password
            u.password_hash = hash_password("pw"); s.add(u)
        s.commit()
    old = main.engine
    try:
        main.engine = eng
        c = TestClient(main.app)
        c.post("/login", data={"email": "seller@canary.local", "password": "pw"}, follow_redirects=False)
        blocked = all(c.get(p).status_code == 403
                      for p in ("/campaigns", "/inbox", "/suppression", "/mail", "/outreach/analytics"))
        leak = "secretbuyer@x.com" in (c.get(f"/inbox/{lid}").text if c.get(f"/inbox/{lid}").status_code == 200
                                       else "")
        return blocked and not leak, f"seller 403 on outreach pages={blocked}, buyer leak={leak}"
    finally:
        main.engine = old


def check_reply_correlation():
    """A durable RFC Message-ID is persisted before send and an inbound reply correlates back through
    In-Reply-To AND References — exercised offline so the live canary only confirms real delivery."""
    from app.inbound_email import handle_inbound
    from app.models import CampaignSend, Outreach
    eng, ids = _isolated()
    with Session(eng) as s:
        c = _running_campaign(s, ids)
        ld = Lead(product="copper", managed=True, seller_id=ids["seller"], email="buyer@x.com")
        s.add(ld); s.commit(); s.refresh(ld)
        r = CampaignRecipient(campaign_id=c.id, tenant_id=ids["seller"], lead_id=ld.id, to_email="buyer@x.com",
                              sequence_version=1, current_step=0, status="pending")
        s.add(r); s.commit(); s.refresh(r)
        CAMP.send_step(s, c, r, s.get(MailAccount, ids["mailbox"]), sender=_ok)
        cs = s.exec(select(CampaignSend)).one()
        mid = cs.rfc_message_id
        has_id = bool(mid) and mid == (s.exec(select(Outreach).where(Outreach.direction == "out")).one().message_id)
        via_refs = handle_inbound(s, "someone@buyer.com", "Re", "yes", message_id="<rp@x>",
                                  references=f"<root@x> {mid}")
        return has_id and via_refs == "threaded", f"msgid persisted={has_id}, reply via References={via_refs}"


# --------------------------------------------------------------------- live preflight (gated)
def live_preflight():
    reasons = []
    if not CANARY_ENABLED:
        reasons.append("CANARY_ENABLED is not set")
    if not CANARY_ALLOWLIST:
        reasons.append("CANARY_ALLOWLIST is empty")
    if not CANARY_TEST_RECIPIENT:
        reasons.append("CANARY_TEST_RECIPIENT is not set")
    elif CANARY_TEST_RECIPIENT not in CANARY_ALLOWLIST:
        reasons.append("CANARY_TEST_RECIPIENT is not inside CANARY_ALLOWLIST")
    # a live admin-owned mailbox with stored SMTP credentials
    try:
        from app.db import engine as prod_engine
        with Session(prod_engine) as s:
            mb = s.exec(select(MailAccount).where(MailAccount.admin_owned == True,   # noqa: E712
                                                  MailAccount.active == True)).first()   # noqa: E712
        if not mb or not (mb.smtp_password_enc or "").strip():
            reasons.append("no live admin-owned mailbox with SMTP credentials")
    except Exception as e:  # noqa: BLE001
        reasons.append(f"mailbox check failed: {e}")
    return (not reasons), reasons


def allowlist_ok(addr):
    return (addr or "").strip().lower() in CANARY_ALLOWLIST


def main_report():
    print("=" * 72)
    print("GO4IT PRODUCTION CANARY")
    print("=" * 72)
    print("\nSafety mechanism checks (isolated in-memory DB — production untouched, no external email):")
    checks = [("suppression blocks a second send", check_suppression_blocks),
              ("Pause-All stops the campaign worker", check_pause_all_stops_worker),
              ("sellers see no mailbox / inbox / suppression / buyer", check_sellers_see_nothing),
              ("RFC Message-ID persisted + reply correlates (In-Reply-To/References)", check_reply_correlation)]
    all_ok = True
    for label, fn in checks:
        try:
            ok, detail = fn()
        except Exception as e:  # noqa: BLE001
            ok, detail = False, f"ERROR {e}"
        all_ok = all_ok and ok
        print(f"  [{'PASS' if ok else 'FAIL'}] {label:52s} — {detail}")

    ready, reasons = live_preflight()
    print("\nLive canary (one allowlisted internal test address only):")
    if ready:
        print("  READY — live credentials + allowlist configured. Run the live steps per docs/CANARY_RUNBOOK.md.")
        print("  (This script performs the isolated mechanism checks; the live send steps are executed by the")
        print("   founder following the runbook so a human confirms each real inbox/reply/bounce.)")
        live = "READY (not auto-sent — founder runs the runbook)"
    else:
        print("  NOT RUN — the live path is gated. Reasons:")
        for r in reasons:
            print(f"    · {r}")
        print("  No external email was sent. The live result is NOT simulated.")
        live = "NOT RUN"

    print("\nRESULT:")
    print(f"  mechanism checks : {'ALL PASS' if all_ok else 'FAILURE — see above'}")
    print(f"  live canary      : {live}")
    print("  runbook          : docs/CANARY_RUNBOOK.md")
    print("=" * 72)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main_report())
