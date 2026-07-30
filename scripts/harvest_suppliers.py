"""go4it Intelligence - UAE + Iran supplier-directory harvester (real makers).

    ./.venv/bin/python scripts/harvest_suppliers.py

Grows the SUPPLY side of the catalog with real manufacturers/exporters we can source
from, focused on the winning product (rubber tiles/flooring) plus crumb-rubber and
cold-asphalt. Two verified, no-login directories:

  * UAE  - yellowpages-uae.com/uae/{keyword}?page=N   (PRIMARY: clean cards, phones
           exposed as tel: links; website/email withheld by the directory -> enrich later)
  * Iran - parscenter.com/Suppliers/Industrial-Flooring_501400/{page}  (SECONDARY:
           broad "industrial flooring" bucket, Persian, noisy - we keep names+city and
           tag the rubber-specific ones; website/phone live on the /Company/CMP.. page)

Writes docs/research/suppliers_uae_iran.json (committed backup). Loads into go4it as
Supplier + Product catalog rows via scripts/load_intel.py.
"""
import html
import json
import os
import re
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from geo_en import fa_translit  # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "docs", "research", "suppliers_uae_iran.json")

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) go4it-intel"

# UAE keyword -> our product_key
UAE_KEYWORDS = {
    "rubber-tiles": "rubber-tiles",
    "rubber-flooring": "rubber-tiles",
    "gym-flooring": "rubber-tiles",
    "epdm-rubber-granules": "pour-in-place-rubber",
    "rubber-granules": "pour-in-place-rubber",
    "cold-asphalt": "cold-asphalt",
}
EMIRATES = ["Dubai", "Sharjah", "Abu Dhabi", "Ajman", "Ras Al Khaimah",
            "Umm Al Quwain", "Fujairah", "Al Ain"]

# Iran (parscenter) rubber-signal terms to tag/keep the relevant ones out of the noise.
IR_RUBBER_TERMS = ["لاستیک", "رابر", "گرانول", "تارتان", "epdm", "ای پی دی ام", "رزین"]


def get(url, tries=3):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for i in range(tries):
        try:
            return urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "ignore")
        except Exception as e:  # noqa: BLE001
            if i == tries - 1:
                print(f"   ! {url} -> {e}")
                return ""
            time.sleep(1.5)
    return ""


def city_from(text):
    for e in EMIRATES:
        if e.lower() in (text or "").lower():
            return e
    return ""


def harvest_uae():
    suppliers, seen = [], set()
    for kw, pkey in UAE_KEYWORDS.items():
        for page in range(1, 4):
            url = f"https://www.yellowpages-uae.com/uae/{kw}" + (f"?page={page}" if page > 1 else "")
            h = get(url)
            if not h:
                break
            # each card: <a title="Name" ... href="/slug-<id>?p=...">  then Location + tel:
            anchors = list(re.finditer(r'title="([^"]+)"[^>]*href="(/[a-z0-9-]+-(\d{4,7}))\?p=', h))
            if not anchors:
                break
            new_here = 0
            for idx, m in enumerate(anchors):
                cid = m.group(3)
                if cid in seen:
                    continue
                seen.add(cid)
                new_here += 1
                name = html.unescape(m.group(1)).strip()
                seg = h[m.end(): anchors[idx + 1].start() if idx + 1 < len(anchors) else m.end() + 1500]
                loc = ""
                lm = re.search(r'Location\s*:\s*</span><span[^>]*>([^<]+)', seg)
                if lm:
                    loc = html.unescape(lm.group(1)).strip()
                phones = []
                for pm in re.findall(r'tel:([+\d]{7,})', seg):
                    if pm not in phones:
                        phones.append(pm)
                est = re.search(r'Established:</span>\s*(\d{4})', seg)
                suppliers.append({
                    "company": name, "country": "AE", "city": city_from(loc),
                    "location": loc, "phones": phones[:3],
                    "product_key": pkey, "keyword": kw,
                    "established": est.group(1) if est else "",
                    "profile": m.group(2),
                })
            print(f"   UAE {kw:22} p{page}: +{new_here} suppliers")
            time.sleep(0.5)
            if len(anchors) < 20:
                break
    return suppliers


def harvest_iran(max_pages=4):
    suppliers, seen = [], set()
    for page in range(1, max_pages + 1):
        url = "https://parscenter.com/Suppliers/Industrial-Flooring_501400" + (f"/{page}" if page > 1 else "")
        h = get(url)
        if not h:
            break
        items = re.split(r'data-list-item', h)[1:]
        if not items:
            break
        for it in items:
            cm = re.search(r'/Company/(CMP\d+)', it)
            nm = re.search(r'alt="([^"]{2,80})"', it)
            if not cm or not nm:
                continue
            cid = cm.group(1)
            if cid in seen:
                continue
            seen.add(cid)
            raw_name = html.unescape(nm.group(1)).strip()
            loc = re.search(r'موقعیت[:：]?\s*(?:</?\w+>)?\s*([^<\n]{2,30})', it)
            raw_city = loc.group(1).strip() if loc else ""
            blob = (raw_name + " " + it).lower()
            is_rubber = any(t.lower() in blob for t in IR_RUBBER_TERMS)
            name = fa_translit(raw_name)                    # -> Latin display name
            city = fa_translit(raw_city)
            suppliers.append({
                "company": name, "country": "IR", "city": city, "location": city,
                "phones": [], "company_id": cid,
                "product_key": "rubber-tiles" if is_rubber else "industrial-flooring",
                "rubber_specific": is_rubber,
                "profile": f"/Company/{cid}",
            })
        print(f"   IR industrial-flooring p{page}: {len(items)} items, total {len(suppliers)}")
        time.sleep(0.6)
    return suppliers


def run():
    print("=== UAE (yellowpages-uae.com) ===")
    uae = harvest_uae()
    print("=== Iran (parscenter.com) ===")
    iran = harvest_iran()
    ir_rubber = [s for s in iran if s.get("rubber_specific")]

    result = {
        "sources": ["yellowpages-uae.com", "parscenter.com"],
        "uae_count": len(uae), "iran_count": len(iran),
        "iran_rubber_specific": len(ir_rubber),
        "suppliers": uae + iran,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    print(f"\nUAE suppliers: {len(uae)}  |  Iran suppliers: {len(iran)} "
          f"({len(ir_rubber)} rubber-specific)")
    print("\nUAE sample (name | city | phone | product):")
    for s in uae[:12]:
        print(f"  {s['company'][:38]:<38} {s['city']:<12} "
              f"{(s['phones'][0] if s['phones'] else '-'):<15} {s['product_key']}")
    print("\nIran rubber-specific sample:")
    for s in ir_rubber[:8]:
        print(f"  {s['company'][:40]:<40} {s['city']}")
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    run()
