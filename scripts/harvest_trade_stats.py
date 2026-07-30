"""go4it Intelligence — Customs / trade-statistics harvester (real data, not LLM).

    ./.venv/bin/python scripts/harvest_trade_stats.py

Pulls Georgia's ACTUAL import records from the UN Comtrade public API (free preview
endpoint, no key) for the HS codes behind the products we're evaluating, and turns the
raw customs rows into a market picture that a competitor cannot reproduce by prompting an
LLM:

  * market size per year (tonnes + CIF USD) and YoY trend
  * real landed unit price ($/kg CIF) for the whole market and per source country
  * the current supplier landscape — who already ships this to Georgia and how cheaply
  * where Iran / UAE / Turkmenistan / Turkey / China sit vs. the premium (EU) benchmark

Writes docs/research/trade_stats_georgia.json (committed backup) and prints a summary.
This is the first module of the real-source intelligence layer; the auto-hunter (live
RFQ crawl) and procurement/maps/supplier harvesters are the siblings.
"""
import json
import os
import time
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "docs", "research", "trade_stats_georgia.json")

API = "https://comtradeapi.un.org/public/v1/preview/C/A/HS"
GEORGIA = 268            # M49 reporter code
YEARS = [2021, 2022, 2023, 2024]

# HS codes behind the three products (4- and 6-digit as Georgia reports them).
COMMODITIES = [
    ("2715",   "Cold asphalt / bituminous mixtures", "cold-asphalt"),
    ("400691", "Rubber floor coverings & mats (rubber tiles)", "rubber-tiles"),
    ("4016",   "Other articles of vulcanised rubber (parent)", "rubber-tiles"),
    ("4004",   "Waste/parings/scrap rubber + granules (crumb for pour-in-place)", "pour-in-place-rubber"),
    ("4002",   "Synthetic rubber, primary (SBR granules)", "pour-in-place-rubber"),
]

# M49 partner codes -> readable names + a flag for the countries we care about.
PARTNERS = {
    0: "World", 364: "Iran", 784: "UAE", 795: "Turkmenistan", 31: "Azerbaijan",
    51: "Armenia", 643: "Russia", 792: "Turkey", 156: "China", 380: "Italy",
    300: "Greece", 804: "Ukraine", 616: "Poland", 528: "Netherlands", 826: "UK",
    112: "Belarus", 703: "Slovakia", 276: "Germany", 250: "France", 724: "Spain",
    398: "Kazakhstan", 860: "Uzbekistan", 356: "India", 410: "South Korea",
    203: "Czechia", 40: "Austria", 705: "Slovenia", 100: "Bulgaria", 642: "Romania",
    348: "Hungary", 208: "Denmark", 752: "Sweden", 246: "Finland", 620: "Portugal",
    56: "Belgium", 372: "Ireland", 442: "Luxembourg", 191: "Croatia", 233: "Estonia",
    440: "Lithuania", 428: "Latvia", 268: "Georgia", 498: "Moldova", 158: "Taiwan",
    344: "Hong Kong", 702: "Singapore", 458: "Malaysia", 764: "Thailand", 360: "Indonesia",
    704: "Vietnam", 818: "Egypt", 682: "Saudi Arabia", 634: "Qatar", 414: "Kuwait",
    48: "Bahrain", 512: "Oman", 400: "Jordan", 422: "Lebanon", 792000: "Turkey",
}
# Our sourcing angle: Iran + UAE are the two we can supply from.
OUR_SOURCES = {364, 784}
# Low-cost regional incumbents worth watching (the real competition).
WATCH = {795, 792, 156, 643, 31, 51, 398, 860}


def fetch(cmd, year):
    url = f"{API}?reporterCode={GEORGIA}&period={year}&cmdCode={cmd}&flowCode=M"
    req = urllib.request.Request(url, headers={"User-Agent": "go4it-intel/1.0"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8")).get("data", [])
        except Exception as e:  # noqa: BLE001
            if attempt == 2:
                print(f"   ! {cmd}/{year} failed: {e}")
                return []
            time.sleep(2)
    return []


def unit_price(value, kg):
    return round(value / kg, 3) if kg else None


def run():
    out = {"source": "UN Comtrade (reporter Georgia 268, flow=Import)",
           "generated_period": "auto", "commodities": []}

    for cmd, label, pkey in COMMODITIES:
        print(f"\n=== HS {cmd} — {label} ===")
        years_data = {}
        partner_totals = {}   # partnerCode -> {"kg":..,"usd":..}
        for year in YEARS:
            rows = fetch(cmd, year)
            time.sleep(0.6)     # be polite to the free endpoint
            world = next((r for r in rows if r["partnerCode"] == 0), None)
            if world:
                kg = world.get("qty") or world.get("netWgt") or 0
                usd = world.get("primaryValue") or 0
                years_data[year] = {"tonnes": round(kg / 1000, 1), "cif_usd": round(usd),
                                    "unit_price_usd_kg": unit_price(usd, kg)}
                print(f"   {year}: {round(kg/1000,1):>8} t   ${round(usd):>10,}   "
                      f"{unit_price(usd,kg)} $/kg")
            for r in rows:
                pc = r["partnerCode"]
                if pc == 0:
                    continue
                kg = r.get("qty") or r.get("netWgt") or 0
                usd = r.get("primaryValue") or 0
                agg = partner_totals.setdefault(pc, {"kg": 0.0, "usd": 0.0})
                agg["kg"] += kg
                agg["usd"] += usd

        # supplier landscape (summed across the years we pulled), cheapest-first by $/kg
        suppliers = []
        for pc, agg in partner_totals.items():
            if agg["usd"] <= 0:
                continue
            suppliers.append({
                "country": PARTNERS.get(pc, f"code{pc}"),
                "code": pc,
                "tonnes_total": round(agg["kg"] / 1000, 1),
                "cif_usd_total": round(agg["usd"]),
                "unit_price_usd_kg": unit_price(agg["usd"], agg["kg"]),
                "we_supply": pc in OUR_SOURCES,
                "watch_competitor": pc in WATCH,
            })
        suppliers.sort(key=lambda s: (s["unit_price_usd_kg"] is None,
                                      s["unit_price_usd_kg"] if s["unit_price_usd_kg"] else 9e9))

        yr = sorted(years_data)
        trend = None
        if len(yr) >= 2 and years_data[yr[0]]["cif_usd"]:
            first, last = years_data[yr[0]]["cif_usd"], years_data[yr[-1]]["cif_usd"]
            trend = round((last - first) / first * 100) if first else None

        iran = next((s for s in suppliers if s["code"] == 364), None)
        uae = next((s for s in suppliers if s["code"] == 784), None)
        cheapest = suppliers[0] if suppliers else None

        out["commodities"].append({
            "hs_code": cmd, "label": label, "product_key": pkey,
            "years": years_data, "trend_pct_first_to_last": trend,
            "cheapest_source": cheapest,
            "iran_present": iran, "uae_present": uae,
            "top_suppliers": suppliers[:12],
        })

        if suppliers:
            print("   cheapest sources ($/kg):")
            for s in suppliers[:6]:
                tag = " <-- WE SUPPLY" if s["we_supply"] else (" [competitor]" if s["watch_competitor"] else "")
                print(f"      {s['unit_price_usd_kg']:>6} $/kg  {s['country']:<14} "
                      f"{s['tonnes_total']:>7} t  ${s['cif_usd_total']:>9,}{tag}")
            print(f"   Iran currently in this market: {'YES' if iran else 'no'}"
                  f"   |   UAE: {'YES' if uae else 'no'}")

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    run()
