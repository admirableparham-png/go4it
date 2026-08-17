"""Background ingestion + enrichment worker.

    python -m app.worker            # loop: inbox often, portal hourly, web-enrich if ENRICH_INTERVAL>0
    python -m app.worker --once     # one pass (inbox + portal + enrich) — used by launchd/cron
    python -m app.worker --portal   # portal only (captures the login page to ./debug)
    python -m app.worker --enrich   # one web-enrich pass only (fill blank contacts from sites)
    python -m app.worker --inbound  # one inbound-email poll (thread buyer replies; needs IMAP_*)

Run it as ONE process (a separate worker, not inside gunicorn's web workers — that would double-fire
every job). Idempotent dedup means a crash-and-restart never double-imports; the enrich pass skips
leads it already attempted, so it works down NEW harvested leads instead of re-scraping dead sites.
"""
import logging
import sys
import time

from sqlmodel import Session

from datetime import datetime, timedelta

from sqlmodel import select

from .config import (BASE_URL, ENRICH_BATCH, ENRICH_INTERVAL, FOLLOWUP_ENABLED, FOLLOWUP_INTERVAL,
                     GO4WORLD_ENABLED, GO4WORLD_INTERVAL, IMAP_ENABLED, IMAP_INTERVAL, INBOX_DIR,
                     INGEST_INTERVAL, REQUEST_REMINDER_HOURS, REQUEST_REMINDER_INTERVAL, SMTP_ENABLED)
from .db import engine, init_db
from .enrich_service import run_web_enrichment
from .followups import process_followups
from .inbound_email import poll_inbox
from .ingest import ingest_source
from .models import ServiceRequest, User
from .telegram import send_message
from .sources.go4world_csv import Go4WorldCsvSource
from .sources.go4world_portal import Go4WorldPortalSource

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("go4it.worker")


def run_inbox() -> dict:
    return ingest_source(Go4WorldCsvSource(INBOX_DIR))


def run_portal() -> dict:
    return ingest_source(Go4WorldPortalSource())


def run_enrich() -> dict:
    """One bounded web-enrichment pass over not-yet-attempted website leads."""
    with Session(engine) as session:
        return run_web_enrichment(session, limit=ENRICH_BATCH, skip_attempted=True,
                                  log=logger.info)


def run_inbound_email() -> dict:
    """One inbound-email poll: thread buyer replies onto their leads (no-op unless IMAP configured)."""
    with Session(engine) as session:
        return poll_inbox(session, log=logger.info)


def run_followups() -> dict:
    """One follow-up sweep: threaded FU emails + 'call' nudges. No-op unless armed (FOLLOWUP_ENABLED) +
    SMTP configured. Only ever acts on leads whose first email the founder already sent."""
    if not (FOLLOWUP_ENABLED and SMTP_ENABLED):
        return {"skipped": "disabled"}
    with Session(engine) as session:
        return process_followups(session, log=logger.info)


_reminded_requests = set()   # in-memory dedup: ping the founder about each stale request only once


def run_request_reminders() -> dict:
    """Ping the founder about concierge requests still 'submitted' after REQUEST_REMINDER_HOURS — once
    each, so no trader's request is silently dropped. In-memory dedup (a restart may re-ping; harmless)."""
    if REQUEST_REMINDER_INTERVAL <= 0:
        return {"skipped": "disabled"}
    cutoff = datetime.utcnow() - timedelta(hours=REQUEST_REMINDER_HOURS)
    sent = 0
    with Session(engine) as session:
        stale = session.exec(select(ServiceRequest).where(
            ServiceRequest.status == "submitted", ServiceRequest.created_at <= cutoff)).all()
        for sr in stale:
            if sr.id in _reminded_requests:
                continue
            requester = session.get(User, sr.requester_id)
            who = (requester.name or requester.email) if requester else "a trader"
            try:
                send_message(f"⏰ <b>Pending request {sr.tracking_code}</b> from {who} — "
                             f"{sr.product or '-'} → {sr.market or '-'}\nApprove: {BASE_URL}/admin/requests")
            except Exception:  # noqa: BLE001
                pass
            _reminded_requests.add(sr.id)
            sent += 1
    return {"reminded": sent}


def run_once():
    """One full pass: CSV inbox always, portal if creds set, enrich/inbound-email if enabled."""
    init_db()
    out = [run_inbox()]
    if GO4WORLD_ENABLED:
        out.append(run_portal())
    if ENRICH_INTERVAL > 0:
        out.append(run_enrich())
    if IMAP_ENABLED:
        out.append(run_inbound_email())
    if FOLLOWUP_ENABLED:
        out.append(run_followups())
    out.append(run_request_reminders())
    return out


def main():
    if "--portal" in sys.argv:
        init_db()
        print(run_portal())
        return
    if "--enrich" in sys.argv:
        init_db()
        print(run_enrich())
        return
    if "--inbound" in sys.argv:
        init_db()
        print(run_inbound_email())
        return
    if "--followups" in sys.argv:
        init_db()
        print(run_followups())
        return
    if "--reminders" in sys.argv:
        init_db()
        print(run_request_reminders())
        return
    if "--once" in sys.argv:
        for r in run_once():
            print(r)
        return

    logger.info("worker started; inbox every %ss, portal %s, web-enrich %s, inbound-email %s",
                INGEST_INTERVAL,
                f"every {GO4WORLD_INTERVAL}s" if GO4WORLD_ENABLED else "disabled (no creds)",
                f"every {ENRICH_INTERVAL}s (batch {ENRICH_BATCH})" if ENRICH_INTERVAL > 0
                else "disabled (set ENRICH_INTERVAL)",
                f"every {IMAP_INTERVAL}s" if (IMAP_ENABLED and IMAP_INTERVAL > 0)
                else "disabled (set IMAP_*)")
    last_portal = last_enrich = last_imap = last_followup = last_reminder = 0.0
    while True:
        try:
            init_db()
            r = run_inbox()
            if r["new"] or r["seen"]:
                logger.info("inbox %s", r)
            now = time.time()
            if GO4WORLD_ENABLED and now - last_portal >= GO4WORLD_INTERVAL:
                logger.info("portal %s", run_portal())
                last_portal = now
            if ENRICH_INTERVAL > 0 and now - last_enrich >= ENRICH_INTERVAL:
                run_enrich()
                last_enrich = now
            if IMAP_ENABLED and IMAP_INTERVAL > 0 and now - last_imap >= IMAP_INTERVAL:
                run_inbound_email()
                last_imap = now
            if FOLLOWUP_ENABLED and FOLLOWUP_INTERVAL > 0 and now - last_followup >= FOLLOWUP_INTERVAL:
                logger.info("followups %s", run_followups())
                last_followup = now
            if REQUEST_REMINDER_INTERVAL > 0 and now - last_reminder >= REQUEST_REMINDER_INTERVAL:
                rr = run_request_reminders()
                if rr.get("reminded"):
                    logger.info("request reminders %s", rr)
                last_reminder = now
        except Exception:
            logger.exception("worker pass failed")
        time.sleep(INGEST_INTERVAL)


if __name__ == "__main__":
    main()
