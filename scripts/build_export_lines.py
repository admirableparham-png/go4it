"""Turn the verified Iran-export buyer hunt into 11 go4it product-lines (one category per product).

    ./.venv/bin/python scripts/build_export_lines.py     # writes specs + buyers files
    for s in <slugs>: ./.venv/bin/python scripts/load_line.py $s   # loads them as leads

Reads docs/research/iran_export_buyers.json -> writes, per product:
  * docs/research/lines/<slug>.json               (a line spec — shows at /lines/<slug>)
  * docs/research/<slug>_export_buyers.json       (buyers grouped by BUYER COUNTRY)
Each verified buy-request becomes a buyer, grouped by their country, tagged confirmed/plausible, with
the posted date + source link preserved. Multi-country (dest per buyer), so /uae stays UAE-only.
"""
import json
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.line_spec import LINES_DIR, RESEARCH_DIR  # noqa: E402

SRC = os.path.join(RESEARCH_DIR, "iran_export_buyers.json")

ISO = {"india": "IN", "united kingdom": "GB", "uk": "GB", "united states": "US", "usa": "US",
       "poland": "PL", "france": "FR", "spain": "ES", "south korea": "KR", "korea": "KR",
       "argentina": "AR", "afghanistan": "AF", "malaysia": "MY", "oman": "OM",
       "united arab emirates": "AE", "uae": "AE", "germany": "DE", "south africa": "ZA",
       "czech republic": "CZ", "czechia": "CZ", "russia": "RU", "indonesia": "ID", "qatar": "QA",
       "turkey": "TR", "colombia": "CO", "mexico": "MX", "chile": "CL"}
MARKETPLACE = ("tradekey", "go4worldbusiness", "exportersindia", "espaceagro", "find-tender",
               "b2b-center", "etenders", "sba.gov", "europa.eu", "freshdi", "eoitashkent", "gov.za")
LABELS = {"zinc-sulfate": "Zinc sulphate", "copper-sulfate": "Copper sulphate", "saffron": "Saffron",
          "pistachio": "Pistachios", "honey-royaljelly": "Honey & royal jelly",
          "plastic-pe-packing": "Plastic & PE packaging", "turquoise-gems": "Turquoise & gems",
          "herbs-essential-oils": "Herbs & essential oils", "carpets-kilim": "Carpets & kilims",
          "dates-ardeh": "Dates & ardeh", "tiles-sanitaryware": "Tiles & sanitaryware"}


def iso_of(country):
    c = (country or "").lower()
    for k, v in ISO.items():
        if k in c:
            return v
    return ""


def clean_country(country):
    return re.sub(r"\s*\(.*?\)", "", country or "").strip()


def email_of(s):
    m = [e for e in re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", s or "") if "*" not in e]
    return m[0] if m else ""


def phone_of(s):
    for m in re.findall(r"\+?\d[\d\s().\-]{6,}\d", s or ""):
        if "*" not in m and sum(ch.isdigit() for ch in m) >= 7:
            return re.sub(r"\s+", " ", m).strip()
    return ""


def website_of(contact, src):
    for d in re.findall(r"\b([a-z0-9-]+\.[a-z]{2,}(?:\.[a-z]{2,})?)\b", (contact or "").lower()):
        if "@" not in d and d not in ("e.g", "i.e") and not any(mp in d for mp in MARKETPLACE):
            return d
    host = re.sub(r"^https?://(www\.)?", "", src or "").split("/")[0]
    return host if host and not any(mp in host for mp in MARKETPLACE) else ""


def run():
    res = json.load(open(SRC, encoding="utf-8"))
    slugs = []
    for p in res["products"]:
        slug = p["key"]
        label = LABELS.get(slug, p["product"])
        buyers = []
        for x in p["verified"]:
            country = clean_country(x.get("country")) or "Other"
            contact = x.get("contact", "")
            em, ph, web = email_of(contact), phone_of(contact), website_of(contact, x.get("source_url"))
            buyers.append({
                "company": x.get("buyer", ""), "categories": [country], "city": country,
                "dest_iso": iso_of(x.get("country")),
                "phones": [ph] if ph else [], "email": em, "website": web,
                "match_score": 92 if x.get("verdict") == "confirmed" else 80,
                "verdict": x.get("verdict", ""),
                "buys": [(x.get("wants", "") or "")[:60]], "wants": x.get("wants", ""),
                "posted": x.get("posted", ""), "source_url": x.get("source_url", ""),
                "needs_enrichment": not (em or ph or web), "bulk_likely": False,
            })
        order = [c for c, _ in Counter(b["categories"][0] for b in buyers).most_common()]
        bf = f"{slug}_export_buyers.json"
        json.dump({"source": "verified-hunt-2025+", "total": len(buyers),
                   "with_phone": sum(1 for b in buyers if b["phones"]),
                   "with_email": sum(1 for b in buyers if b["email"]),
                   "with_website": sum(1 for b in buyers if b["website"]), "buyers": buyers},
                  open(os.path.join(RESEARCH_DIR, bf), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        spec = {
            "slug": slug, "label": label,
            "product": f"{label} (Iran-sourced) - verified 2025+ foreign buy-requests",
            "category": f"export-{slug}",
            "dest": {"iso": "", "m49": 0, "name": "Export (multi-country)", "directory": "none"},
            "source": f"iran-export-{slug}", "rfq_source": f"iran-export-{slug}-rfq",
            "buyers_file": bf, "rfqs_file": "",
            "meta": {"name": label,
                     "positioning": "Real 2025+ foreign buy-side demand for this Iran-sourced product (verified + de-noised).",
                     "brands": [], "note": p.get("product_read", "")},
            "families": [],
            "scoring": {"default_base": 70, "cat_base": {}, "families_for": {}, "boost": [], "penalty": []},
            "display_order": order,
        }
        json.dump(spec, open(os.path.join(LINES_DIR, f"{slug}.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        slugs.append(slug)
        print(f"  {slug:20} {len(buyers):>2} buyers across {len(order)} countries -> spec + {bf}")
    print("\nnext: load them ->", " ".join(slugs))


if __name__ == "__main__":
    run()
