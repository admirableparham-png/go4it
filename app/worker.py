"""Background ingestion worker.

    python -m app.worker           # poll the inbox forever (INGEST_INTERVAL secs)
    python -m app.worker --once    # run a single pass and exit (used by tests / cron)

Dependency-free: a simple poll loop (no APScheduler). Re-ingesting the same file
is safe because dedup is idempotent, so a crash-and-restart never double-imports.
"""
import logging
import sys
import time

from .config import INBOX_DIR, INGEST_INTERVAL
from .db import init_db
from .ingest import ingest_source
from .sources.go4world_csv import Go4WorldCsvSource

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("go4it.worker")


def run_once() -> dict:
    init_db()
    return ingest_source(Go4WorldCsvSource(INBOX_DIR))


def main():
    if "--once" in sys.argv:
        print(run_once())
        return
    logger.info("go4it ingestion worker started; watching %s every %ss", INBOX_DIR, INGEST_INTERVAL)
    while True:
        try:
            result = run_once()
            if result["new"] or result["seen"]:
                logger.info("ingested %s", result)
        except Exception:
            logger.exception("ingestion pass failed")
        time.sleep(INGEST_INTERVAL)


if __name__ == "__main__":
    main()
