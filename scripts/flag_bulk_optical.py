"""Flag bulk-likely UAE optical-media buyers so the /uae tab + report can colour them.

    ./.venv/bin/python scripts/flag_bulk_optical.py

Bulk buyers = the ones who RESELL or MASS-CONSUME discs (not single retail shops): distributors,
wholesalers, CD/DVD duplication houses, free-zone traders (FZE/FZC - import/re-export), and
media/optical-named firms. Adds `bulk_likely: true/false` to each buyer in
docs/research/uae_optical_buyers.json. Reusable + no network - re-run after a fresh harvest/enrich.
"""
import json
import os

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "docs", "research", "uae_optical_buyers.json")

# name signals for a bulk buyer (matched against " <lowercased company> ") — the reseller/distributor
# channel that moves printable discs in volume, incl. the medical-equipment supply chain.
BULK_SIGNALS = ["distribut", "wholesale", "whole sale", " fze", " fzc", "fz llc", "fz-llc",
                "general trading", " traders", "import", "export", " media",
                "medical equipment", "equipment suppl", "equipment trading", "medical suppl",
                "it distribut", "healthcare"]


def bulk_likely(company: str) -> bool:
    lo = " " + (company or "").lower() + " "
    return any(k in lo for k in BULK_SIGNALS)


def run():
    with open(SRC, encoding="utf-8") as f:
        data = json.load(f)
    n = 0
    for b in data.get("buyers", []):
        b["bulk_likely"] = bulk_likely(b.get("company"))
        n += 1 if b["bulk_likely"] else 0
    data["bulk_likely_count"] = n
    with open(SRC, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"flagged {n} bulk-likely / {len(data.get('buyers', []))} buyers")


if __name__ == "__main__":
    run()
