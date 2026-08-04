"""Score + enrich a product line's buyers from its spec.

    ./.venv/bin/python scripts/enrich_line.py cd-dvd

Adds match_score + `buys` (fit families) + bulk_likely to each buyer in docs/research/<buyers_file>;
for UAE (yellowpages) lines it also scrapes the detail page for email/website/phones for buyers scoring
>= spec.scoring.min_enrich. Reusable + re-runnable. Replaced scripts/enrich_uae_*.py.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.harvest_lib import detail_contacts  # noqa: E402
from app.line_spec import load_spec, research_path  # noqa: E402
from app.scoring import bulk_likely, score_and_fits  # noqa: E402


def run(slug):
    spec = load_spec(slug)
    scoring = spec.get("scoring", {})
    signals = spec.get("bulk_signals", [])
    min_enrich = int(scoring.get("min_enrich", 55))
    path = research_path(spec["buyers_file"])
    data = json.load(open(path, encoding="utf-8"))
    buyers = data.get("buyers", [])

    for b in buyers:
        b["match_score"], b["buys"] = score_and_fits(b.get("company"), b.get("categories"), scoring)
        b["bulk_likely"] = bulk_likely(b.get("company"), signals)

    # detail-page enrichment only for UAE directory buyers (they carry a `profile`); OSM has none
    todo = [b for b in buyers if b.get("match_score", 0) >= min_enrich and b.get("profile")]
    print(f"scoring done. enriching {len(todo)}/{len(buyers)} buyers (score >= {min_enrich}) "
          f"from detail pages...")
    enr_email = enr_web = 0
    for i, b in enumerate(todo, 1):
        email, website, phones = detail_contacts(b.get("profile", ""))
        if email and not b.get("email"):
            b["email"] = email
            enr_email += 1
        if website and not b.get("website"):
            b["website"] = website
            enr_web += 1
        for p in phones:
            if p not in b["phones"]:
                b["phones"].append(p)
        b["needs_enrichment"] = not (b.get("email") or b.get("website"))
        if i % 100 == 0:
            print(f"   ...{i}/{len(todo)}  (+{enr_email} email, +{enr_web} website)")
        time.sleep(0.35)

    data["high_matches"] = sum(1 for b in buyers if b.get("match_score", 0) >= 75)
    data["with_email"] = sum(1 for b in buyers if b.get("email"))
    data["with_website"] = sum(1 for b in buyers if b.get("website"))
    data["bulk_likely_count"] = sum(1 for b in buyers if b.get("bulk_likely"))
    json.dump(data, open(path, "w", encoding="utf-8"), indent=2, ensure_ascii=False)

    print(f"\nHigh-rate matches (>=75): {data['high_matches']} | with email: {data['with_email']} "
          f"| with website: {data['with_website']} | bulk-flagged: {data['bulk_likely_count']}")
    for b in sorted(buyers, key=lambda x: -x.get("match_score", 0))[:12]:
        tag = "BULK " if b.get("bulk_likely") else "     "
        print(f"  {b.get('match_score', 0):>3} {tag}{(b.get('company') or '')[:44]:<44} "
              f"{b.get('email') or (b.get('phones') or [''])[0]}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: enrich_line.py <line-slug>   (e.g. cd-dvd, decoration)")
        sys.exit(1)
    run(sys.argv[1])
