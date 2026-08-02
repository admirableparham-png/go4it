"""go4it Intelligence - UAE BUYERS for PRINTABLE blank optical media (CD-R / DVD-R / Blu-ray).

    ./.venv/bin/python scripts/harvest_uae_optical_buyers.py

We stock PRINTABLE (white full-face inkjet) blank discs and want the people who genuinely need them in
HIGH VOLUME / bulk (brands Bingo/Arita/Princo/RiDATA/Sony). Flagship demand = MEDICAL IMAGING: hospitals,
MRI/CT/radiology, diagnostic/medical labs burn PATIENT SCAN data onto a logo-printed disc; plus the
medical-equipment / IT / media DISTRIBUTOR channel (bulk resale) and photo studios. We deliberately DROP
print shops (printing-press / digital-printing) + generic electronics/office (the 'maybe' noise) - not
our target. Slugs below were PROBED live 2026-08-02. Tiers = likely VOLUME (distributors first, then
large medical end-users, then photo). enrich_uae_optical_buyers.py then scores + ranks by fit/volume.

Phones are ~public on list pages; website/email sit on detail pages. Dedup across slugs by company id.
Writes docs/research/uae_optical_buyers.json.
"""
import html
import json
import os
import re
import time
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "docs", "research", "uae_optical_buyers.json")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) go4it-intel"

# buyer slug -> (readable buyer type, which disc families fit, max pages). Dict order = harvest order
# = likely VOLUME (bulk distributors first). Slugs probed live 2026-08-02. Print shops intentionally
# excluded. Several slugs share a label (e.g. all medical-equipment* -> "Medical equipment supplier").
SLUGS = {
    # Tier 1 - bulk resellers / distributors (highest volume, the surest bulk bet)
    "medical-equipment-suppliers": ("Medical equipment supplier", "all printable", 10),
    "medical-equipment":           ("Medical equipment supplier", "all printable", 8),
    "medical-equipment-trading":   ("Medical equipment supplier", "all printable", 3),
    "it-distributors":             ("IT distributor", "all printable", 8),
    "computer-supplies":           ("Computer supplies", "all printable", 6),
    "distributors":                ("Distributor", "all printable", 2),
    # Tier 2 - high-intent medical end-users (burn patient scans onto printable discs)
    "hospitals":                   ("Hospital", "cd-r + dvd-r", 8),
    "medical-centers":             ("Medical centre", "cd-r + dvd-r", 8),
    "medical-laboratories":        ("Medical laboratory", "cd-r + dvd-r", 8),
    "medical-labs":                ("Medical laboratory", "cd-r + dvd-r", 3),
    "laboratories":                ("Laboratory", "cd-r + dvd-r", 8),
    "radiology":                   ("Radiology / imaging", "all printable", 2),
    "mri":                         ("Radiology / imaging", "all printable", 1),
    "clinics":                     ("Clinic", "cd-r + dvd-r", 8),
    "dental-clinics":              ("Dental clinic", "cd-r + dvd-r", 6),
    "healthcare":                  ("Healthcare", "cd-r + dvd-r", 6),
    "polyclinics":                 ("Polyclinic", "cd-r + dvd-r", 1),
    # Tier 3 - other genuine users (real want, lower volume)
    "photography":                 ("Photography / photo studio", "cd-r + dvd-r", 4),
    "photo-studios":               ("Photography / photo studio", "cd-r + dvd-r", 2),
    "photography-studios":         ("Photography / photo studio", "cd-r + dvd-r", 2),
    "recording-studios":           ("Recording studio", "cd-r + dvd-r", 2),
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
    """Yield card dicts from a yellowpages-uae list page."""
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
            page_new = 0
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
        print(f"   {slug:22} -> {got} listings")

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
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    run()
