"""Background ingestion worker.

    python -m app.worker            # loop: inbox often, go4world portal hourly
    python -m app.worker --once     # one pass (inbox + portal) — used by launchd/cron
    python -m app.worker --portal   # portal only (captures the login page to ./debug)

Idempotent dedup means a crash-and-restart never double-imports.
"""
import logging
import sys
import time

from .config import (GO4WORLD_ENABLED, GO4WORLD_INTERVAL, INBOX_DIR,
                     INGEST_INTERVAL)
from .db import init_db
from .ingest import ingest_source
from .sources.go4world_csv import Go4WorldCsvSource
from .sources.go4world_portal import Go4WorldPortalSource

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("go4it.worker")


def run_inbox() -> dict:
    return ingest_source(Go4WorldCsvSource(INBOX_DIR))


def run_portal() -> dict:
    return ingest_source(Go4WorldPortalSource())


def run_once():
    """One full pass: CSV inbox always, go4world portal if credentials are set."""
    init_db()
    out = [run_inbox()]
    if GO4WORLD_ENABLED:
        out.append(run_portal())
    return out


def main():
    if "--portal" in sys.argv:
        init_db()
        print(run_portal())
        return
    if "--once" in sys.argv:
        for r in run_once():
            print(r)
        return

    logger.info("worker started; inbox every %ss, go4world portal %s", INGEST_INTERVAL,
                f"every {GO4WORLD_INTERVAL}s" if GO4WORLD_ENABLED else "disabled (no creds)")
    last_portal = 0.0
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
        except Exception:
            logger.exception("ingestion pass failed")
        time.sleep(INGEST_INTERVAL)


if __name__ == "__main__":
    main()
