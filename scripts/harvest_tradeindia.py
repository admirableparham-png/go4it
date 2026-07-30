"""go4it Intelligence - TradeIndia buy-lead (RFQ) harvester.

    ./.venv/bin/python scripts/harvest_tradeindia.py

Pulls active BUY REQUESTS from TradeIndia's public buy-lead board (no login, no anti-bot)
for our three product areas. Honest caveat: TradeIndia buyers are ~99% India-based, so
this is mainly India-import demand intel + RFQ-board coverage; the harvester tags each
lead's country so genuinely regional (Georgia/Turkey/CIS/EU) buyers are separated out and
are the ones loaded into go4it. Contact details sit behind TradeIndia login; the public
card gives product, city, country, date, quantity, spec and a stable RFQ id.

Writes docs/research/tradeindia_rfqs.json (committed backup).
"""
import html
import json
import os
import re
import time
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "docs", "research", "tradeindia_rfqs.json")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) go4it-intel"

# (buy-lead category path, our product_key, keep-if-any-of these terms in title/desc)
CATS = [
    ("Energy-Power/Bitumen", "cold-asphalt",
     ["bitumen", "asphalt", "cold mix", "cold-mix", "cutback", "emulsion"]),
    ("Chemicals/Rubber-Rubber-Products", "pour-in-place-rubber",
     ["crumb", "granule", "epdm", "sbr", "rubber tile", "rubber mat", "rubber floor", "reclaim"]),
    ("Construction-Real-Estate/Floor-Tiles", "rubber-tiles",
     ["rubber", "gym", "sport", "playground", "interlock", "epdm"]),
]


def get(url):
    for i in range(3):
        try:
            return urllib.request.urlopen(
                urllib.request.Request(url, headers={"User-Agent": UA}), timeout=30
            ).read().decode("utf-8", "ignore")
        except Exception as e:  # noqa: BLE001
            if i == 2:
                print(f"   ! {url} -> {e}")
                return ""
            time.sleep(1.5)
    return ""


def strip(s):
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or ""))).strip()


def parse_cards(page):
    cards = re.split(r'<div class="blListCard"', page)[1:]
    out = []
    for c in cards:
        tm = re.search(r'href="(/buyoffer/(\d+)/[^"]+)"[^>]*class="title"', c) or \
             re.search(r'class="title"[^>]*href="(/buyoffer/(\d+)/[^"]+)"', c)
        if not tm:
            continue
        title = strip(re.search(r'class="title"[^>]*>(.*?)</a>', c, re.S).group(1)) if re.search(r'class="title"[^>]*>(.*?)</a>', c, re.S) else ""
        iso = re.search(r'/countries-flags/([a-z]{2})\.jpg', c)
        out.append({
            "rfq_id": tm.group(2),
            "path": tm.group(1),
            "title": title,
            "city": strip(re.search(r'class="blCity"[^>]*>(.*?)</div>', c, re.S).group(1)) if re.search(r'class="blCity"', c) else "",
            "country_iso": (iso.group(1).upper() if iso else ""),
            "date": strip(re.search(r'class="blDate"[^>]*>(.*?)</span>', c, re.S).group(1)) if re.search(r'class="blDate"', c) else "",
            "qty": strip(re.search(r'class="blCustomData"[^>]*>(.*?)</div>', c, re.S).group(1)) if re.search(r'class="blCustomData"', c) else "",
            "spec": strip(re.search(r'class="blDiscription"[^>]*>(.*?)</div>', c, re.S).group(1))[:300] if re.search(r'class="blDiscription"', c) else "",
        })
    return out


def run():
    leads, seen = [], set()
    for path, pkey, terms in CATS:
        cat_n = 0
        for page_no in (1, 2):
            url = f"https://www.tradeindia.com/TradeLeads/buy/{path}/" + (f"?page_no={page_no}" if page_no > 1 else "")
            page = get(url)
            cards = parse_cards(page)
            if not cards:
                break
            for c in cards:
                if c["rfq_id"] in seen:
                    continue
                blob = (c["title"] + " " + c["spec"]).lower()
                if not any(t in blob for t in terms):
                    continue
                seen.add(c["rfq_id"])
                c["product_key"] = pkey
                c["is_regional"] = c["country_iso"] not in ("IN", "")   # non-India = on-target geography
                leads.append(c)
                cat_n += 1
            time.sleep(0.6)
        print(f"   {path:42} -> {cat_n} on-target RFQs")

    regional = [x for x in leads if x["is_regional"]]
    result = {
        "source": "tradeindia.com public buy-leads",
        "total_on_target": len(leads),
        "regional_non_india": len(regional),
        "note": "Buyers are mostly India; only non-India (regional) leads load into go4it CRM.",
        "leads": leads,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    print(f"\n{len(leads)} on-target RFQs  |  {len(regional)} non-India (regional) -> go4it")
    print("\nSample RFQs:")
    for x in leads[:12]:
        tag = "REGIONAL" if x["is_regional"] else "IN"
        print(f"  [{tag:>8}] {x['country_iso']:<3} {x['title'][:44]:<44} {x['date']}")
    if regional:
        print("\nRegional (non-India) leads:")
        for x in regional[:10]:
            print(f"  {x['country_iso']} | {x['title'][:50]} | {x['city']}")
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    run()
