"""go4it Research engine — real-source market intelligence for ANY country + product.

The moat is REAL customs data, not an LLM: this pulls a destination country's ACTUAL
import records from the UN Comtrade public API (free preview endpoint, no key) for a given
HS code, and turns the raw rows into a market picture a competitor can't reproduce by
prompting ChatGPT:

  * market size per year (tonnes + CIF USD) and YoY trend
  * real landed unit price ($/kg CIF) for the whole market and per source country
  * the current supplier landscape — who already ships this and how cheaply (the competition)
  * where Iran / UAE (our sourcing angle) sit vs the incumbents, and whether there's headroom
  * a deterministic good/watch/bad VERDICT + an opportunity score, all from the numbers

Results are cached under docs/research/market/{reporter}_{hs}_{years}.json so the first query
for a (country, HS) pair fetches live (seconds) and every repeat is instant. The cache doubles
as a committed backup.

Importable + side-effect-free: the web routes (app/main.py) and the CLI harvester
(scripts/harvest_trade_stats.py) both call market_report(). Python 3.9 — no `X | Y`
annotations (bare defaults / typing.Optional only).
"""
import json
import math
import os
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List, Optional, Tuple

BASE = Path(__file__).resolve().parent.parent
CACHE_DIR = BASE / "docs" / "research" / "market"

API = "https://comtradeapi.un.org/public/v1/preview/C/A/HS"
DEFAULT_YEARS = [2022, 2023, 2024]
OUR_SOURCES_DEFAULT = (364, 784)          # Iran, UAE — the two we can supply from

# M49 partner/reporter codes -> readable names (covers the corridors we trade + the real
# competition). Any code Comtrade returns that isn't here shows as "code{n}".
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
    48: "Bahrain", 512: "Oman", 400: "Jordan", 422: "Lebanon", 4: "Afghanistan",
    368: "Iraq", 586: "Pakistan", 887: "Yemen", 12: "Algeria", 504: "Morocco",
    788: "Tunisia", 434: "Libya", 231: "Ethiopia", 404: "Kenya", 800: "Uganda",
    834: "Tanzania", 894: "Zambia", 716: "Zimbabwe", 710: "South Africa",
    # UN Comtrade legacy/specific partner codes that differ from plain M49:
    699: "India", 842: "USA", 840: "USA", 251: "France", 757: "Switzerland",
    579: "Norway", 490: "Other Asia (Taiwan)", 381: "Italy", 58: "Belgium-Lux",
}

# Low-cost regional incumbents worth watching (the real competition on price).
WATCH = {795, 792, 156, 643, 31, 51, 398, 860, 356, 699}

# Product -> (display label, [HS codes as the reporters file them]). Extend freely; a user can
# always enter a raw HS code the console doesn't know. Keys double as the product_key.
PRODUCT_HS: Dict[str, Tuple[str, List[str]]] = {
    "cold-asphalt": ("Cold asphalt / bituminous mixtures", ["2715"]),
    "bitumen": ("Bitumen / petroleum asphalt", ["2713", "2714"]),
    "rubber-tiles": ("Rubber tiles & flooring (gym / outdoor)", ["400691", "4016"]),
    "pour-in-place-rubber": ("Pour-in-place rubber (SBR / crumb)", ["4002", "4004"]),
    "ceramic-tiles": ("Ceramic tiles (floor / wall)", ["6907"]),
    "sanitaryware": ("Ceramic sanitaryware", ["6910"]),
    "tableware-glass": ("Glassware & table/kitchen glass", ["7013"]),
    "tableware-ceramic": ("Ceramic tableware / kitchenware", ["6911", "6912"]),
    "home-decor-ceramic": ("Ceramic ornaments / statuettes", ["6913"]),
    "home-decor-metal": ("Base-metal ornaments / decor", ["8306"]),
    "houseware-plastic": ("Plastic tableware & houseware", ["3924"]),
    "houseware-steel": ("Steel kitchen / table articles", ["7323"]),
    "polyethylene": ("Polyethylene (PE) primary", ["3901"]),
    "polypropylene": ("Polypropylene (PP) primary", ["3902"]),
    "methanol": ("Methanol", ["2905"]),
    "urea": ("Urea fertilizer", ["3102"]),
    "sulphur": ("Sulphur", ["2503"]),
    "cement": ("Cement (clinker / portland)", ["2523"]),
    "steel-rebar": ("Steel bars & rods (rebar)", ["7213", "7214"]),
    "marble-stone": ("Worked marble / stone", ["6802"]),
    "dates": ("Dates", ["0804"]),
    "pistachios": ("Pistachios / nuts", ["0802"]),
    "saffron": ("Saffron", ["091020"]),
    "carpets": ("Carpets (knotted / woven)", ["5701", "5702"]),
    "detergents": ("Soap / organic surfactants", ["3401", "3402"]),
    "optical-media": ("Blank optical media (CD-R / DVD-R / Blu-ray)", ["852341"]),
}

# Curated shortlist the "what should I focus on for country Y?" scan ranks — goods Iran/UAE
# can realistically supply. Kept modest so the first (uncached) scan stays tolerable.
SCAN_BASKET = [
    "cold-asphalt", "bitumen", "rubber-tiles", "pour-in-place-rubber", "ceramic-tiles",
    "tableware-glass", "home-decor-metal", "houseware-plastic", "steel-rebar", "cement",
    "polyethylene", "urea", "sulphur", "marble-stone", "dates",
]

_HS_LABEL = {hs: label for label, hs_list in PRODUCT_HS.values() for hs in hs_list}


# --------------------------------------------------------------------------- fetch + aggregate

def _fetch(reporter_code, cmd, year):
    """One UN Comtrade preview call: imports of `cmd` into `reporter_code` for `year`.
    Returns a list of rows on success ([] is a GENUINE empty market), or None on a
    network/rate-limit failure — so callers can avoid caching a transient empty as 'dead'."""
    url = f"{API}?reporterCode={reporter_code}&period={year}&cmdCode={cmd}&flowCode=M"
    req = urllib.request.Request(url, headers={"User-Agent": "go4it-research/1.0"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8")).get("data", []) or []
        except Exception:  # noqa: BLE001
            if attempt == 2:
                return None
            time.sleep(1.5)
    return None


def _unit_price(value, kg):
    return round(value / kg, 3) if kg else None


def _years_tag(years):
    ys = sorted(years)
    return f"{ys[0]}-{ys[-1]}" if len(ys) > 1 else str(ys[0])


def _cache_path(reporter_code, cmd, years):
    return CACHE_DIR / f"{reporter_code}_{cmd}_{_years_tag(years)}.json"


def _raw_commodity(reporter_code, cmd, years):
    """Fetch + aggregate one HS code into a cacheable (our_sources-independent) dict."""
    years_data = {}
    partner_totals = {}                      # partnerCode -> {"kg":.., "usd":..}
    incomplete = False                       # any year fetch failed -> don't cache as 'dead'
    for year in years:
        rows = _fetch(reporter_code, cmd, year)
        if rows is None:
            incomplete = True
            rows = []
        time.sleep(0.6)                      # be polite to the free endpoint
        world = next((r for r in rows if r.get("partnerCode") == 0), None)
        if world:
            kg = world.get("netWgt") or world.get("qty") or 0   # netWgt is kg; qty may be m²/units
            usd = world.get("primaryValue") or 0
            years_data[str(year)] = {"tonnes": round(kg / 1000, 1), "cif_usd": round(usd),
                                     "unit_price_usd_kg": _unit_price(usd, kg)}
        for r in rows:
            pc = r.get("partnerCode")
            if pc in (0, None):
                continue
            kg = r.get("netWgt") or r.get("qty") or 0   # netWgt is kg; qty may be m²/units
            usd = r.get("primaryValue") or 0
            agg = partner_totals.setdefault(pc, {"kg": 0.0, "usd": 0.0})
            agg["kg"] += kg
            agg["usd"] += usd

    suppliers = []
    for pc, agg in partner_totals.items():
        if agg["usd"] <= 0:
            continue
        suppliers.append({
            "country": PARTNERS.get(pc, f"code{pc}"), "code": pc,
            "tonnes_total": round(agg["kg"] / 1000, 1), "cif_usd_total": round(agg["usd"]),
            "unit_price_usd_kg": _unit_price(agg["usd"], agg["kg"]),
            "watch_competitor": pc in WATCH,
        })
    total_cif = sum(s["cif_usd_total"] for s in suppliers)   # full-market total (for share math)
    # Keep the suppliers that actually shape the market — the biggest by value — plus always
    # retain Iran/UAE if present. Keeping the 15 CHEAPEST instead would fill the list with
    # 1-tonne micro-shipments at freak $/kg and truncate the real players out.
    suppliers.sort(key=lambda s: -s["cif_usd_total"])
    keep = suppliers[:20]
    for code in OUR_SOURCES_DEFAULT:
        row = next((s for s in suppliers if s["code"] == code), None)
        if row and row not in keep:
            keep.append(row)
    keep.sort(key=lambda s: (s["unit_price_usd_kg"] is None,     # display cheapest-first
                             s["unit_price_usd_kg"] if s["unit_price_usd_kg"] else 9e9))
    suppliers = keep

    keys = sorted(years_data)
    trend = None
    if len(keys) >= 2 and years_data[keys[0]]["cif_usd"]:
        first, last = years_data[keys[0]]["cif_usd"], years_data[keys[-1]]["cif_usd"]
        trend = round((last - first) / first * 100) if first else None
    latest = years_data[keys[-1]] if keys else {}

    return {
        "hs_code": cmd, "label": _HS_LABEL.get(cmd, f"HS {cmd}"),
        "years": years_data, "trend_pct_first_to_last": trend,
        "latest_year": keys[-1] if keys else None,
        "latest_tonnes": latest.get("tonnes"), "latest_cif": latest.get("cif_usd"),
        "latest_price": latest.get("unit_price_usd_kg"),
        "top_suppliers": suppliers,
        "total_cif": total_cif,
        "_incomplete": incomplete,
    }


def _commodity(reporter_code, cmd, years, refresh=False):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _cache_path(reporter_code, cmd, years)
    if not refresh and path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    raw = _raw_commodity(reporter_code, cmd, years)
    # Only cache a COMPLETE fetch. A fetch that hit a network/rate-limit error (incomplete) or that
    # came back genuinely empty is NOT persisted, so it re-fetches next time instead of poisoning
    # the opportunity ranking with a permanent false 'tiny market'.
    if not raw.get("_incomplete") and raw.get("total_cif"):
        try:
            path.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    return raw


# --------------------------------------------------------------------------- verdict + scoring

def _assess(raw, our_sources):
    """Deterministic direction call from the real numbers. Returns tone/headline/reasons +
    a 0-100 opportunity score and the Iran/UAE position — all derived, nothing invented."""
    suppliers = raw.get("top_suppliers") or []
    cif = raw.get("latest_cif") or 0
    trend = raw.get("trend_pct_first_to_last")
    total = raw.get("total_cif") or sum(s["cif_usd_total"] for s in suppliers) or 0
    for s in suppliers:
        s["we_supply"] = s["code"] in our_sources
    # "cheapest incumbent" must be a MATERIAL competitor (>=3% of the market), not a 1-tonne
    # sample that lands at a freak $/kg. Fall back to all suppliers if none clear the bar.
    material = [s for s in suppliers if total and s["cif_usd_total"] >= 0.03 * total]
    pool = material or suppliers
    cheapest = min(pool, key=lambda s: (s["unit_price_usd_kg"] or 9e9)) if pool else None
    leader = max(suppliers, key=lambda s: s["cif_usd_total"]) if suppliers else None
    ours = [s for s in suppliers if s["code"] in our_sources]
    our_best = min(ours, key=lambda s: (s["unit_price_usd_kg"] or 9e9)) if ours else None
    top_share = round(100 * leader["cif_usd_total"] / total) if (leader and total) else 0

    reasons = []
    if cif:
        reasons.append(f"Market size {cif:,} USD CIF in {raw.get('latest_year')} "
                       f"({raw.get('latest_tonnes')} t).")
    if trend is not None:
        arrow = "growing" if trend > 0 else ("shrinking" if trend < 0 else "flat")
        reasons.append(f"Demand {arrow} {trend:+d}% over the window.")

    # tone/headline
    if cif < 50_000:
        tone = "bad"
        headline = "Tiny market — not worth opening a lane."
    elif our_best and cheapest and our_best["code"] == cheapest["code"]:
        tone = "good"
        headline = f"{our_best['country']} is ALREADY the cheapest supplier ({our_best['unit_price_usd_kg']} $/kg) — proven lane."
    elif our_best:
        cp = cheapest["unit_price_usd_kg"] if cheapest else None
        if cp and our_best["unit_price_usd_kg"] and our_best["unit_price_usd_kg"] <= cp * 1.1:
            tone = "good"
            headline = f"{our_best['country']} already competitive (within 10% of the cheapest incumbent)."
        else:
            tone = "watch"
            headline = (f"{our_best['country']} present but above {cheapest['country']}'s "
                        f"{cheapest['unit_price_usd_kg']} $/kg — win on logistics/terms, not price.")
        reasons.append(f"Our best source {our_best['country']} lands at {our_best['unit_price_usd_kg']} $/kg.")
    else:
        if top_share >= 60 and leader:
            tone = "bad"
            headline = (f"{leader['country']} owns {top_share}% of the market — hard to "
                        f"displace; Iran/UAE absent.")
        else:
            tone = "good"
            headline = ("Fragmented market, no dominant incumbent and Iran/UAE not yet in — "
                        "room to enter.")
        if cheapest:
            reasons.append(f"Cheapest material incumbent: {cheapest['country']} at "
                           f"{cheapest['unit_price_usd_kg']} $/kg. Leader {leader['country']} "
                           f"holds {top_share}%.")

    if trend is not None and trend < -25:
        reasons.append("Caution: demand falling sharply.")

    # opportunity score 0-100 = size(40) + growth(20) + our-position(40)
    size_pts = min(40, math.log10(cif + 1) * 6.5) if cif else 0
    growth_pts = max(0, min(20, 10 + (trend or 0) / 10))
    if our_best and cheapest and our_best["code"] == cheapest["code"]:
        pos_pts = 40
    elif our_best:
        cp = cheapest["unit_price_usd_kg"] if cheapest else None
        pos_pts = 32 if (cp and our_best["unit_price_usd_kg"] and our_best["unit_price_usd_kg"] <= cp * 1.1) else 20
    elif top_share < 60:
        pos_pts = 26
    else:
        pos_pts = 10
    score = int(round(max(0, min(100, size_pts + growth_pts + pos_pts))))

    return {
        "tone": tone, "headline": headline, "reasons": reasons, "score": score,
        "cheapest": cheapest, "leader": leader, "our_best": our_best, "top_share": top_share,
        "iran_present": next((s for s in suppliers if s["code"] == 364), None),
        "uae_present": next((s for s in suppliers if s["code"] == 784), None),
    }


# --------------------------------------------------------------------------- public API

def market_report(reporter_code, hs_codes, our_sources=OUR_SOURCES_DEFAULT,
                  years=None, refresh=False):
    """Full directional report for a destination country + one or more HS codes.

    Returns {reporter, reporter_code, years, our_sources, commodities:[{...+assessment}]}.
    Cached per (reporter, HS, year-range); first call fetches live, repeats are instant.
    """
    years = years or DEFAULT_YEARS
    commodities = []
    for hs in hs_codes:
        raw = _commodity(reporter_code, str(hs).strip(), years, refresh=refresh)
        raw = dict(raw)
        raw["assessment"] = _assess(raw, our_sources)
        commodities.append(raw)
    commodities.sort(key=lambda c: -(c["assessment"]["score"]))
    return {
        "reporter_code": reporter_code, "reporter": PARTNERS.get(reporter_code, f"code{reporter_code}"),
        "years": years, "our_sources": list(our_sources), "commodities": commodities,
    }


def rank_opportunities(reporter_code, our_sources=OUR_SOURCES_DEFAULT,
                       product_keys=None, years=None, refresh=False):
    """The "what should I focus on for country Y?" scan: score a basket of Iran/UAE-supplyable
    products by opportunity for the destination country and return them best-first. Uses each
    product's primary HS + a 2-year window, fetched CONCURRENTLY (a first uncached scan is
    ~15 markets, so serial fetching would block far too long); results cache like everything."""
    keys = product_keys or SCAN_BASKET
    years = years or DEFAULT_YEARS[-2:]

    def _one(k):
        label, hs_list = PRODUCT_HS.get(k, (k, [k]))
        rep = market_report(reporter_code, hs_list[:1], our_sources, years, refresh)
        best = rep["commodities"][0] if rep["commodities"] else None
        if not best or not best.get("latest_cif"):
            return None
        a = best["assessment"]
        return {
            "product_key": k, "label": label, "hs_code": best["hs_code"],
            "score": a["score"], "tone": a["tone"], "headline": a["headline"],
            "latest_cif": best["latest_cif"], "latest_tonnes": best["latest_tonnes"],
            "latest_price": best["latest_price"], "trend": best["trend_pct_first_to_last"],
            "cheapest": a["cheapest"], "iran_present": bool(a["iran_present"]),
            "uae_present": bool(a["uae_present"]),
        }

    with ThreadPoolExecutor(max_workers=5) as ex:
        rows = [r for r in ex.map(_one, keys) if r]
    rows.sort(key=lambda r: -r["score"])
    return rows


# --------------------------------------------------------------------------- lookup helpers (UI)

def country_options():
    """Sorted [(name, code)] for the destination dropdown (World + duplicate codes dropped)."""
    seen = {}
    for code, name in PARTNERS.items():
        if code == 0 or code > 900:          # skip World + the 792000-style dupes
            continue
        seen.setdefault(name, code)
    return sorted(seen.items())


def product_options():
    """[(key, label, 'hs a / b')] for the product picker."""
    return [(k, v[0], " / ".join(v[1])) for k, v in PRODUCT_HS.items()]


def resolve_query(product="", hs=""):
    """Map a product picker key/label OR a raw HS string to (label, [hs_codes], product_key).
    Returns ([]) for hs_codes when nothing resolves so the route can prompt for an HS code."""
    hs = (hs or "").strip()
    if hs:
        codes = [c.strip() for c in hs.replace(",", " ").split() if c.strip().isdigit()]
        if codes:
            return (_HS_LABEL.get(codes[0], f"HS {', '.join(codes)}"), codes, "")
    p = (product or "").strip().lower()
    if p in PRODUCT_HS:
        return (PRODUCT_HS[p][0], PRODUCT_HS[p][1], p)
    for k, (label, codes) in PRODUCT_HS.items():
        if p and (p in label.lower() or p in k):
            return (label, codes, k)
    return (product or "", [], "")
