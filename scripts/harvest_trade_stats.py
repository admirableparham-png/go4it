"""go4it Intelligence — Georgia trade-statistics harvester (real data, not LLM).

    ./.venv/bin/python scripts/harvest_trade_stats.py

Thin CLI wrapper over app.research_engine.market_report(): pulls Georgia's ACTUAL import
records from the UN Comtrade public API (free, no key) for the HS codes behind the products
we evaluate, and writes docs/research/trade_stats_georgia.json in the shape the /intel tab
reads. All the fetch/aggregate/verdict logic now lives in the shared engine, so the
Research console (/research) and this backup harvester stay in lock-step.

Writes the committed backup + prints a summary.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.research_engine import market_report  # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "docs", "research", "trade_stats_georgia.json")

GEORGIA = 268
YEARS = [2021, 2022, 2023, 2024]
# HS codes behind the three /intel products (4- and 6-digit as Georgia files them) + product_key.
GE_COMMODITIES = [
    ("2715", "cold-asphalt"),
    ("400691", "rubber-tiles"),
    ("4016", "rubber-tiles"),
    ("4004", "pour-in-place-rubber"),
    ("4002", "pour-in-place-rubber"),
]


def run():
    hs_list = [hs for hs, _ in GE_COMMODITIES]
    rep = market_report(GEORGIA, hs_list, years=YEARS, refresh=True)
    by_hs = {c["hs_code"]: c for c in rep["commodities"]}

    out = {"source": "UN Comtrade (reporter Georgia 268, flow=Import)",
           "generated_period": "-".join(str(y) for y in (YEARS[0], YEARS[-1])),
           "commodities": []}
    for hs, pkey in GE_COMMODITIES:
        c = by_hs.get(hs)
        if not c:
            continue
        a = c["assessment"]
        out["commodities"].append({
            "hs_code": hs, "label": c["label"], "product_key": pkey,
            "years": c["years"], "trend_pct_first_to_last": c["trend_pct_first_to_last"],
            "cheapest_source": a["cheapest"], "iran_present": a["iran_present"],
            "uae_present": a["uae_present"], "top_suppliers": c["top_suppliers"],
        })
        latest = c["years"].get(str(YEARS[-1]), {})
        print(f"\n=== HS {hs} — {c['label']} ===")
        print(f"   {YEARS[-1]}: {latest.get('tonnes')} t  ${latest.get('cif_usd', 0):,}  "
              f"{latest.get('unit_price_usd_kg')} $/kg  | [{a['tone'].upper()}] {a['headline']}")

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    run()
