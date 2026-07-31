"""go4it Intelligence - UAE BUYERS for the Decora Store home-decor range.

    ./.venv/bin/python scripts/harvest_uae_decor_buyers.py

Founder sources decorative homeware/giftware from Iran (decorastore1.ir - gold/silver
serveware, clocks, lamps, decor) and wants BUYERS in the UAE. yellowpages-uae.com lists
~1,000-1,300 unique UAE decor/gift/tableware buyers with PUBLIC phone numbers, via the
simple /uae/{slug}?page=N pattern (verified). We reuse the same card parser as
scripts/harvest_suppliers.py (which harvests the UAE supplier side of this site).

Phones are ~100% public; website/email sit on some detail pages only -> we capture
name/city/phone/category from the list pages (fast) and flag the rest needs_enrichment
for a Clay pass. Dedup across slugs (same trading cos recur in tableware/crockery/hotel).

Writes docs/research/uae_decor_buyers.json. Loaded into go4it by scripts/load_uae.py.
"""
import html
import json
import os
import re
import time
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "docs", "research", "uae_decor_buyers.json")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) go4it-intel"

# buyer slug -> (readable buyer type, which Decora families fit, max pages)
SLUGS = {
    "home-decor":     ("Home-decor retailer", "all families", 44),
    "tableware":      ("Tableware / serveware", "serveware/tableware", 11),
    "crockery":       ("Crockery", "serveware/tableware", 6),
    "housewares":     ("Housewares", "serveware + decor", 3),
    "handicrafts":    ("Handicrafts", "decor objects + serveware", 1),
    "lightings":      ("Lighting shop", "lighting (lamps/chandeliers)", 2),
    "gift-items":     ("Gift / corporate-gift", "decor objects + clocks + gifting", 13),
    "gift-shops":     ("Gift shop", "decor objects + clocks", 1),
    "hotel-supplies": ("Hotel & hospitality supplier", "serveware/tableware", 9),
}
EMIRATES = ["Dubai", "Sharjah", "Abu Dhabi", "Ajman", "Ras Al Khaimah",
            "Umm Al Quwain", "Fujairah", "Al Ain"]


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


def city_from(text):
    for e in EMIRATES:
        if e.lower() in (text or "").lower():
            return e
    return ""


def parse_cards(h):
    """Yield (cid, name, city, phones, established, profile) from a list page."""
    anchors = list(re.finditer(r'title="([^"]+)"[^>]*href="(/[a-z0-9-]+-(\d{4,7}))\?p=', h))
    out = []
    for idx, m in enumerate(anchors):
        seg = h[m.end(): anchors[idx + 1].start() if idx + 1 < len(anchors) else m.end() + 1600]
        loc = re.search(r'Location\s*:\s*</span><span[^>]*>([^<]+)', seg)
        loc = html.unescape(loc.group(1)).strip() if loc else ""
        phones = []
        for p in re.findall(r'tel:([+\d]{7,})', seg):
            if p not in phones:
                phones.append(p)
        est = re.search(r'Established:</span>\s*(\d{4})', seg)
        web = re.search(r'href="(https?://(?!www\.yellowpages-uae)[^"]+)"[^>]*>\s*(?:website|visit)', seg, re.I)
        out.append({
            "cid": m.group(3), "company": html.unescape(m.group(1)).strip(),
            "location": loc, "city": city_from(loc), "phones": phones[:3],
            "established": est.group(1) if est else "",
            "website": web.group(1) if web else "", "profile": m.group(2),
        })
    return out


def run():
    buyers = {}   # cid -> record (dedup across slugs; collect categories)
    for slug, (label, fit, maxp) in SLUGS.items():
        got, slug_seen = 0, set()
        for page in range(1, maxp + 1):
            url = f"https://www.yellowpages-uae.com/uae/{slug}" + (f"?page={page}" if page > 1 else "")
            cards = parse_cards(get(url))
            if not cards:
                break
            page_new = 0                       # new companies for THIS slug this page
            for c in cards:
                if c["cid"] in slug_seen:
                    continue
                slug_seen.add(c["cid"])
                page_new += 1
                got += 1
                rec = buyers.get(c["cid"])
                if rec is None:
                    rec = dict(c)
                    rec["categories"] = []
                    rec["fits"] = set()
                    rec["needs_enrichment"] = not (c["website"])
                    buyers[c["cid"]] = rec
                else:
                    if not rec["website"] and c["website"]:
                        rec["website"] = c["website"]; rec["needs_enrichment"] = False
                    for p in c["phones"]:
                        if p not in rec["phones"]:
                            rec["phones"].append(p)
                if label not in rec["categories"]:
                    rec["categories"].append(label)
                rec["fits"].add(fit)
            time.sleep(0.4)
            if page_new == 0:                  # out-of-range page (site repeats page 1) -> stop
                break
        print(f"   {slug:16} -> {got} listings")

    recs = list(buyers.values())
    for r in recs:
        r["fits"] = sorted(r.pop("fits"))
        r.pop("cid", None)
    with_phone = [r for r in recs if r["phones"]]
    with_web = [r for r in recs if r["website"]]
    result = {
        "source": "yellowpages-uae.com", "dest": "UAE",
        "total": len(recs), "with_phone": len(with_phone), "with_website": len(with_web),
        "by_category": {label: sum(1 for r in recs if label in r["categories"])
                        for label, _, _ in SLUGS.values()},
        "buyers": recs,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    print(f"\n{len(recs)} unique UAE buyers ({len(with_phone)} with phone, {len(with_web)} with website).")
    print("by category:", result["by_category"])
    print("\nsample:")
    for r in recs[:12]:
        print(f"  {r['company'][:38]:<38} {r['city'][:12]:<12} "
              f"{(r['phones'][0] if r['phones'] else '-'):<15} {', '.join(r['categories'][:2])}")
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    run()
