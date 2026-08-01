"""Fill blank buyer emails/phones from each lead's own website (no third-party credits).

    ./.venv/bin/python scripts/enrich_leads.py                 # DRY-RUN: report hit-rate, no writes
    ./.venv/bin/python scripts/enrich_leads.py --apply         # write found contacts back
    ./.venv/bin/python scripts/enrich_leads.py --source osm-ge --limit 50 --apply
    ./.venv/bin/python scripts/enrich_leads.py --apply --pause 0.5   # gentler on target sites

Selects leads that have a website but no email, scrapes homepage + contact pages for a role
mailbox (info@/sales@/export@) and any tel: phone, and writes ONLY into blank fields — never
overwriting a human-entered contact. Every fill is recorded as an Activity note and the batch as
an IngestionRun (source=enrich-web) for observability. Dry-run by default so you can see the yield
before spending time hitting sites.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlmodel import Session  # noqa: E402

from app.db import engine, init_db  # noqa: E402
from app.enrich_service import run_web_enrichment  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="Enrich lead contacts from their own websites.")
    ap.add_argument("--apply", action="store_true", help="write results (default: dry-run)")
    ap.add_argument("--source", default=None, help="only leads from this source (e.g. osm-ge)")
    ap.add_argument("--limit", type=int, default=None, help="cap number of leads to try")
    ap.add_argument("--pause", type=float, default=0.3, help="seconds between page fetches")
    args = ap.parse_args()

    init_db()
    with Session(engine) as session:
        summary = run_web_enrichment(
            session, source=args.source, limit=args.limit,
            apply=args.apply, pause=args.pause,
        )
    if not args.apply:
        print("\n(dry-run — nothing written. re-run with --apply to save these contacts.)")
    print(f"summary: {summary}")


if __name__ == "__main__":
    main()
