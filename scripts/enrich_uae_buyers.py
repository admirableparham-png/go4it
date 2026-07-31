"""Enrich + score the UAE decor buyers.

    ./.venv/bin/python scripts/enrich_uae_buyers.py

Two jobs, writing back into docs/research/uae_decor_buyers.json:
  1. MATCH SCORE (0-100) + `buys` (which Decora product families fit each buyer) - so the
     report shows what each buyer would buy from our site AND ranks the HIGH-RATE MATCHES
     (real serveware/gift/decor/hospitality buyers) above the interior/maintenance
     contractors that pollute the "home-decor" listing.
  2. WEBSITE + EMAIL + extra phones - scraped from each buyer's yellowpages-uae detail page
     (verified listings expose them). Only fetches detail pages for score >= MIN_ENRICH
     (the buyers worth contacting), to stay fast + polite.
"""
import html
import json
import os
import re
import time
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(BASE, "docs", "research", "uae_decor_buyers.json")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) go4it-intel"
MIN_ENRICH = 55        # only fetch detail pages for buyers scoring >= this

CAT_BASE = {
    "Tableware / serveware": 90, "Crockery": 88, "Gift shop": 90,
    "Hotel & hospitality supplier": 85, "Housewares": 80, "Handicrafts": 78,
    "Lighting shop": 68, "Gift / corporate-gift": 70, "Home-decor retailer": 60,
}
FAMILIES = {
    "Tableware / serveware": ["Serveware & tableware"],
    "Crockery": ["Serveware & tableware"],
    "Hotel & hospitality supplier": ["Serveware & tableware"],
    "Housewares": ["Serveware & tableware", "Decorative objects"],
    "Gift shop": ["Decorative objects", "Clocks", "Serveware & tableware"],
    "Gift / corporate-gift": ["Decorative objects", "Clocks", "Serveware & tableware"],
    "Handicrafts": ["Decorative objects", "Serveware & tableware"],
    "Lighting shop": ["Lighting"],
    "Home-decor retailer": ["Serveware & tableware", "Decorative objects", "Clocks", "Lighting"],
}
BOOST = ["trading", "general trading", "gift", "gifts", "home", "homeware", "houseware",
         "decor", "crockery", "tableware", "kitchen", "glass", "luxury", "hospitality",
         "hotel", "souvenir", "handicraft", "gallery", "porcelain", "chinaware"]
PENALTY = ["interior", "maintenance", "fit out", "fit-out", "fitout", "contracting",
           "contractor", "construction", "designer", "signage", "advertising",
           "promotional", "promotion", "printing", "media", "technical", "engineering",
           "cleaning", "facility", "real estate", "consultan"]
JUNK = ["yellowpages-uae", "google", "facebook", "instagram", "twitter", "x.com", "threads",
        "youtube", "whatsapp", "pinterest", "w3.org", "gstatic", "cloudflare", "cdnjs",
        "jquery", "schema.org", "dmca", "undefined", "linkedin", "tiktok"]
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def score_and_fits(b):
    cats = b.get("categories") or []
    base = max((CAT_BASE.get(c, 55) for c in cats), default=55)
    name = (b.get("company") or "").lower()
    s = base + 6 * sum(1 for k in BOOST if k in name) - 12 * sum(1 for k in PENALTY if k in name)
    fams = []
    for c in cats:
        for fam in FAMILIES.get(c, []):
            if fam not in fams:
                fams.append(fam)
    return max(0, min(100, s)), fams


def get(url):
    for i in range(2):
        try:
            return urllib.request.urlopen(
                urllib.request.Request(url, headers={"User-Agent": UA}), timeout=25
            ).read().decode("utf-8", "ignore")
        except Exception:  # noqa: BLE001
            if i == 1:
                return ""
            time.sleep(1.0)
    return ""


def detail_contacts(profile):
    h = get(f"https://www.yellowpages-uae.com{profile}")
    if not h:
        return "", "", []
    emails = [e for e in EMAIL_RE.findall(h)
              if not e.lower().endswith((".png", ".jpg", ".webp", ".css", ".js"))
              and not any(j in e.lower() for j in ("yellowpages", "sentry", "example", "wixpress"))]
    email = emails[0] if emails else ""
    sites = [u for u in re.findall(r'https?://[^"\s<>]+', h)
             if not any(j in u.lower() for j in JUNK)]
    website = sites[0] if sites else ""
    phones = []
    for p in re.findall(r"tel:([+\d]{7,})", h):
        if p not in phones:
            phones.append(p)
    return email, website, phones


def run():
    data = json.load(open(SRC, encoding="utf-8"))
    buyers = data["buyers"]
    for b in buyers:
        b["match_score"], b["buys"] = score_and_fits(b)

    todo = [b for b in buyers if b["match_score"] >= MIN_ENRICH]
    print(f"scoring done. enriching {len(todo)}/{len(buyers)} buyers (score >= {MIN_ENRICH}) "
          f"from detail pages...")
    enr_email = enr_web = 0
    for i, b in enumerate(todo, 1):
        email, website, phones = detail_contacts(b.get("profile", ""))
        if email and not b.get("email"):
            b["email"] = email; enr_email += 1
        if website and not b.get("website"):
            b["website"] = website; enr_web += 1
        for p in phones:
            if p not in b["phones"]:
                b["phones"].append(p)
        b["needs_enrichment"] = not (b.get("email") or b.get("website"))
        if i % 100 == 0:
            print(f"   ...{i}/{len(todo)}  (+{enr_email} email, +{enr_web} website)")
        time.sleep(0.35)

    high = [b for b in buyers if b["match_score"] >= 75]
    data["high_matches"] = len(high)
    data["with_email"] = sum(1 for b in buyers if b.get("email"))
    data["with_website"] = sum(1 for b in buyers if b.get("website"))
    json.dump(data, open(SRC, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
    print(f"\nHigh-rate matches (score>=75): {len(high)} | with email: {data['with_email']} "
          f"| with website: {data['with_website']}")
    print("top matches:")
    for b in sorted(buyers, key=lambda x: -x["match_score"])[:12]:
        print(f"  {b['match_score']:>3}  {b['company'][:40]:<40} {b.get('email') or b.get('phones',[''])[0]}")


if __name__ == "__main__":
    run()
