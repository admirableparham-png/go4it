"""go4it Intelligence - Georgian local-business harvester (OpenStreetMap / Overpass).

    ./.venv/bin/python scripts/harvest_ge_businesses.py

Harvests real Georgian businesses that BUY our products, straight from OpenStreetMap
via the free Overpass API - no key, no login:
  * gyms / fitness & sports centres, stadiums, sport pitches  -> rubber gym/sports tiles
  * building-material / hardware / DIY shops                  -> construction-side buyers

OSM gives verified names + city + coordinates; contact tags (phone/website) are present
only on some records - those without get flagged for Clay enrichment downstream. This is
real target coverage (hundreds of named venues) that an LLM cannot produce.

Writes docs/research/ge_businesses.json (committed backup). Named buyers load into go4it
as Leads via scripts/load_intel.py.
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from geo_en import translit_ka  # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "docs", "research", "ge_businesses.json")
OVERPASS = "https://overpass-api.de/api/interpreter"

# OSM selector -> (our product_key, human role)
SELECTORS = [
    ('nwr["leisure"="fitness_centre"]', "rubber-tiles", "gym / fitness centre"),
    ('nwr["leisure"="sports_centre"]', "rubber-tiles", "sports centre"),
    ('nwr["leisure"="stadium"]', "rubber-tiles", "stadium"),
    ('nwr["leisure"="pitch"]["sport"]', "rubber-tiles", "sports pitch"),
    ('nwr["shop"="sports"]', "rubber-tiles", "sports shop"),
    ('nwr["shop"="building_materials"]', "construction-buyer", "building-materials shop"),
    ('nwr["shop"="hardware"]', "construction-buyer", "hardware shop"),
    ('nwr["shop"="doityourself"]', "construction-buyer", "DIY / builders' merchant"),
]


def query(selector):
    ql = ('[out:json][timeout:60];area["ISO3166-1"="GE"][admin_level=2]->.ge;'
          f'({selector}(area.ge);); out center tags 800;')
    data = urllib.parse.urlencode({"data": ql}).encode()
    for i in range(3):
        try:
            raw = urllib.request.urlopen(
                urllib.request.Request(OVERPASS, data=data,
                                       headers={"User-Agent": "go4it-intel"}), timeout=75).read()
            return json.loads(raw).get("elements", [])
        except Exception as e:  # noqa: BLE001
            if i == 2:
                print(f"   ! {selector} -> {e}")
                return []
            time.sleep(3)
    return []


def run():
    businesses, seen = [], set()
    for selector, pkey, role in SELECTORS:
        els = query(selector)
        named = kept = 0
        for e in els:
            t = e.get("tags", {})
            raw_name = t.get("name") or t.get("official_name") or t.get("name:en")
            if not raw_name:
                continue
            named += 1
            # Display name in English: OSM name:en if tagged, else transliterate.
            name = (t.get("name:en") or translit_ka(raw_name).title()).strip()
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            kept += 1
            phone = t.get("phone") or t.get("contact:phone") or t.get("contact:mobile") or ""
            web = t.get("website") or t.get("contact:website") or t.get("url") or ""
            businesses.append({
                "company": name,
                "name_en": t.get("name:en", ""),
                "country": "GE",
                "city": translit_ka(t.get("addr:city") or t.get("addr:place") or "").title(),
                "role": role, "product_key": pkey,
                "sport": t.get("sport", ""),
                "phone": phone, "website": web,
                "street": translit_ka(" ".join(x for x in [t.get("addr:street", ""), t.get("addr:housenumber", "")] if x)),
                "lat": e.get("lat") or (e.get("center") or {}).get("lat"),
                "lon": e.get("lon") or (e.get("center") or {}).get("lon"),
                "needs_enrichment": not (phone or web),
            })
        print(f"   {role:28} {len(els):>4} osm -> {named:>3} named -> +{kept} new")
        time.sleep(1.0)

    gyms = [b for b in businesses if b["product_key"] == "rubber-tiles"]
    with_contact = [b for b in businesses if not b["needs_enrichment"]]
    result = {
        "source": "OpenStreetMap via Overpass API (area=Georgia)",
        "total": len(businesses),
        "rubber_buyers": len(gyms),
        "with_contact": len(with_contact),
        "needs_enrichment": len(businesses) - len(with_contact),
        "businesses": businesses,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    print(f"\n{len(businesses)} named GE businesses  |  {len(gyms)} rubber-tile buyers  "
          f"|  {len(with_contact)} have phone/website, {result['needs_enrichment']} need enrichment")
    print("\nSample (gyms/sports - direct rubber-flooring buyers):")
    for b in gyms[:12]:
        c = b["phone"] or b["website"] or "(enrich)"
        print(f"  {b['company'][:36]:<36} {b['city'][:14]:<14} {b['role'][:18]:<18} {c}")
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    run()
