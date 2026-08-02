"""Enrich + score the UAE blank-optical-media buyers.

    ./.venv/bin/python scripts/enrich_uae_optical_buyers.py

Two jobs, writing back into docs/research/uae_optical_buyers.json:
  1. MATCH SCORE (0-100) + `buys` (which disc families fit each buyer) - so the report ranks the
     real disc buyers (duplication / media / IT / computer sellers) above generic listings, with
     the founder's SPECIALISTS scored highest.
  2. WEBSITE + EMAIL + extra phones - scraped from each buyer's yellowpages-uae detail page, only
     for score >= MIN_ENRICH (the buyers worth contacting), to stay fast + polite.
"""
import html
import json
import os
import re
import time
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(BASE, "docs", "research", "uae_optical_buyers.json")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) go4it-intel"
MIN_ENRICH = 55        # only fetch detail pages for buyers scoring >= this

# base score per buyer type - bulk resellers / distributors highest (VOLUME), then medical/imaging
# end-users (genuine printable-patient-disc demand), then photo. Keys MUST match harvest SLUGS labels
# + products fit_buyers.
CAT_BASE = {
    # bulk resellers / distributors (highest volume)
    "Medical equipment supplier": 90, "IT distributor": 88, "Distributor": 86, "Computer supplies": 80,
    # medical / imaging end-users (real recurring want)
    "Radiology / imaging": 86, "Medical laboratory": 82, "Hospital": 80, "Medical centre": 76,
    "Healthcare": 74, "Laboratory": 74, "Clinic": 70, "Dental clinic": 68, "Polyclinic": 66,
    # other genuine (lower volume)
    "Photography / photo studio": 60, "Recording studio": 56,
}
# buyer type -> which printable families fit (family names MUST match optical_media_products.json).
_ALL = ["Printable CD-R", "Printable DVD-R", "Printable Blu-ray (BD-R)"]
_CDDVD = ["Printable CD-R", "Printable DVD-R"]
FAMILIES = {
    "Medical equipment supplier": _ALL, "IT distributor": _ALL, "Distributor": _ALL, "Computer supplies": _ALL,
    "Radiology / imaging": _ALL, "Medical laboratory": _CDDVD, "Hospital": _CDDVD, "Medical centre": _CDDVD,
    "Healthcare": _CDDVD, "Laboratory": _CDDVD, "Clinic": _CDDVD, "Dental clinic": _CDDVD, "Polyclinic": _CDDVD,
    "Photography / photo studio": _CDDVD, "Recording studio": _CDDVD,
}
BOOST = ["medical", "mri", "radiology", "diagnostic", "imaging", "scan", "laborator", "lab ",
         "clinic", "hospital", "dental", "health", "pharma", "printable", "disc", "media",
         "cd", "dvd", "blu-ray", "bluray", "recordable", "blank", "optical", "data", "archive",
         "photo", "distribut", "wholesale", "supplies", "supplier", "trading", "equipment", "computer"]
PENALTY = ["printing", "printers", "print shop", "press", "signage", "advertising", "typography",
           "banner", "flex", "sticker", "repair", "maintenance", "rental", "real estate", "cleaning",
           "restaurant", "cafe", "garment", "textile", "furniture", "cargo", "logistics", "travel",
           "construction", "cosmetic", "perfume", "jewel"]
JUNK = ["yellowpages-uae", "google", "facebook", "instagram", "twitter", "x.com", "threads",
        "youtube", "whatsapp", "pinterest", "w3.org", "gstatic", "cloudflare", "cdnjs",
        "jquery", "schema.org", "dmca", "undefined", "linkedin", "tiktok", "clarity.ms", "clarity",
        "bing.com", "microsoft", "gtag", "googletag", "hotjar", "sentry", "fontawesome",
        "bootstrapcdn", "unpkg", "jsdelivr", "gravatar", "gmpg.org", "doubleclick", "amazonaws",
        "cloudfront", "apple.com", "play.google", "itunes", "wp.com", "cdn.", "static.", "assets.", "ajax."]


def real_site(u):
    u = (u or "").strip().strip('\\"\'')
    if not u or "\\" in u or " " in u:
        return ""
    lo = u.lower()
    if any(j in lo for j in JUNK):
        return ""
    if any(x in lo for x in ("/tag/", "/badge", "/pixel", ".js", ".css", ".min.", "/ajax/", "/embed")):
        return ""
    return u
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
    website = ""
    for u in re.findall(r'https?://[^"\s<>\\]+', h):
        cleaned = real_site(u)
        if cleaned:
            website = cleaned
            break
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
        print(f"  {b['match_score']:>3}  {b['company'][:42]:<42} {b.get('email') or (b.get('phones') or [''])[0]}")


if __name__ == "__main__":
    run()
