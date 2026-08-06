"""Merge the country-by-country deep-hunt buyers into the 11 export product-lines (English dashboard).

    ./.venv/bin/python scripts/load_country_hunt.py     # merges into <slug>_export_buyers.json
    then: for s in <slugs>: ./.venv/bin/python scripts/load_line.py $s

Reads docs/research/iran_buyers_by_country.json (52 country x product cells) -> for each product, adds
its Tier-1 active buyers + Tier-2 rated potential buyers into docs/research/<slug>_export_buyers.json,
grouped by buyer COUNTRY, tagged with tier (active/potential/pending) + rating + role + linkedin.
Dedup by company name. Keeps the earlier finds. English (the dashboard/CRM is English).
"""
import json
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.line_spec import RESEARCH_DIR  # noqa: E402

SRC = os.path.join(RESEARCH_DIR, "iran_buyers_by_country.json")
ISO = {"Iraq": "IQ", "United Arab Emirates": "AE", "Russia": "RU", "Afghanistan": "AF",
       "China": "CN", "India": "IN", "Pakistan": "PK", "Turkey": "TR"}
MARKET = ("tradekey", "go4worldbusiness", "exportersindia", "espaceagro", "find-tender", "b2b-center",
          "etenders", "sba.gov", "europa.eu", "freshdi", "linkedin", "volza", "eximpedia", "importyeti")


def email_of(s):
    m = [e for e in re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", s or "") if "*" not in e]
    return m[0] if m else ""


def phone_of(s):
    for m in re.findall(r"\+?\d[\d\s().\-]{6,}\d", s or ""):
        if "*" not in m and sum(c.isdigit() for c in m) >= 7:
            return re.sub(r"\s+", " ", m).strip()
    return ""


def website_of(url, contact):
    for u in (url, contact):
        for d in re.findall(r"\b([a-z0-9-]+\.[a-z]{2,}(?:\.[a-z]{2,})?)\b", (u or "").lower()):
            if "@" not in d and d not in ("e.g", "i.e") and not any(mp in d for mp in MARKET):
                return d
    return ""


def run():
    data = json.load(open(SRC, encoding="utf-8"))
    per_product = {}
    for c in data["cells"]:
        slug, country = c["product"], c["country"]
        iso = ISO.get(country, "")
        recs = per_product.setdefault(slug, [])
        for a in c.get("active", []):
            recs.append({
                "company": a.get("buyer", ""), "categories": [country], "city": a.get("city") or country,
                "dest_iso": iso, "phones": [phone_of(a.get("contact"))] if phone_of(a.get("contact")) else [],
                "email": email_of(a.get("contact")), "website": website_of(a.get("source_url"), a.get("contact")),
                "match_score": {"confirmed": 92, "plausible": 82, "pending": 75}.get(a.get("verdict"), 82),
                "verdict": a.get("verdict") or "plausible", "tier": "active",
                "buys": [(a.get("wants", "") or "")[:55]], "wants": a.get("wants", ""),
                "posted": a.get("posted", ""), "source_url": a.get("source_url", ""),
                "role": a.get("role", ""), "linkedin": a.get("linkedin", ""),
                "needs_enrichment": not (email_of(a.get("contact")) or phone_of(a.get("contact"))),
                "bulk_likely": False,
            })
        for p in c.get("potential", []):
            r = max(1, min(5, int(p.get("rating") or 3)))
            recs.append({
                "company": p.get("company", ""), "categories": [country], "city": p.get("city") or country,
                "dest_iso": iso, "phones": [phone_of(p.get("contact"))] if phone_of(p.get("contact")) else [],
                "email": email_of(p.get("contact")), "website": p.get("website") or website_of("", p.get("contact")),
                "match_score": 55 + r * 7, "tier": "potential", "rating": r,
                "buys": [(p.get("business", "") or "")[:55]], "business": p.get("business", ""),
                "why_fit": p.get("why_fit", ""), "linkedin": p.get("linkedin", ""),
                "needs_enrichment": not (email_of(p.get("contact")) or phone_of(p.get("contact")) or p.get("website")),
                "bulk_likely": False,
            })

    for slug, recs in per_product.items():
        path = os.path.join(RESEARCH_DIR, f"{slug}_export_buyers.json")
        blob = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {"buyers": []}
        buyers = blob.get("buyers", [])
        seen = {(b.get("company") or "").strip().lower() for b in buyers}
        added = 0
        for r in recs:
            k = (r["company"] or "").strip().lower()
            if k and k not in seen:
                seen.add(k)
                buyers.append(r)
                added += 1
        blob["buyers"] = buyers
        blob["with_phone"] = sum(1 for b in buyers if b.get("phones"))
        blob["with_email"] = sum(1 for b in buyers if b.get("email"))
        blob["with_website"] = sum(1 for b in buyers if b.get("website"))
        blob["total"] = len(buyers)
        json.dump(blob, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        act = sum(1 for r in recs if r.get("tier") == "active")
        print(f"  {slug:22} +{added} new (of {len(recs)}: {act} active / {len(recs)-act} potential) -> {len(buyers)} total")


if __name__ == "__main__":
    run()
