"""Background ingestion + enrichment worker.

    python -m app.worker            # loop: inbox often, portal hourly, web-enrich if ENRICH_INTERVAL>0
    python -m app.worker --once     # one pass (inbox + portal + enrich) — used by launchd/cron
    python -m app.worker --portal   # portal only (captures the login page to ./debug)
    python -m app.worker --enrich   # one web-enrich pass only (fill blank contacts from sites)

Run it as ONE process (a separate worker, not inside gunicorn's web workers — that would double-fire
every job). Idempotent dedup means a crash-and-restart never double-imports; the enrich pass skips
leads it already attempted, so it works down NEW harvested leads instead of re-scraping dead sites.
"""
import logging
import sys
import time

from sqlmodel import Session

from .config import (ENRICH_BATCH, ENRICH_INTERVAL, GO4WORLD_ENABLED,
                     GO4WORLD_INTERVAL, INBOX_DIR, INGEST_INTERVAL)
from .db import engine, init_db
from .enrich_service import run_web_enrichment
from .ingest import ingest_source
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


def run_once():
    """One full pass: CSV inbox always, portal if creds set, enrich if ENRICH_INTERVAL>0."""
    init_db()
    out = [run_inbox()]
    if GO4WORLD_ENABLED:
        out.append(run_portal())
    if ENRICH_INTERVAL > 0:
        out.append(run_enrich())
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
    if "--once" in sys.argv:
        for r in run_once():
            print(r)
        return

    logger.info("worker started; inbox every %ss, portal %s, web-enrich %s", INGEST_INTERVAL,
                f"every {GO4WORLD_INTERVAL}s" if GO4WORLD_ENABLED else "disabled (no creds)",
                f"every {ENRICH_INTERVAL}s (batch {ENRICH_BATCH})" if ENRICH_INTERVAL > 0
                else "disabled (set ENRICH_INTERVAL)")
    last_portal = last_enrich = 0.0
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
        except Exception:
            logger.exception("worker pass failed")
        time.sleep(INGEST_INTERVAL)


if __name__ == "__main__":
    main()
