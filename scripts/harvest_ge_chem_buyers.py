"""go4it Intelligence - Georgian chemical BUYER universe (Section A).

    ./.venv/bin/python scripts/harvest_ge_chem_buyers.py

Section A of the "Georgia chemical buyers" job: every plausible Georgian buyer of the
mineral-processing + water-treatment reagents on the list. Two inputs merged:

  (a) CURATED ANCHOR ACCOUNTS - the real end-buyers, verified: mines / ore-beneficiation,
      ferroalloy & metallurgy, water utilities, and known importers. This is where the
      mining-reagent demand actually lives (it is never publicly tendered).
  (b) DIRECTORY LONG-TAIL - Georgian chemical importers/distributors from yell.ge
      (chemical-reagents rubric 3767), with email/phone/site scraped from each profile.

Contacts are captured where public; the rest get a needs_enrichment flag (Clay later).
Writes docs/research/ge_chem_buyers.json. Loaded into go4it by scripts/load_chem.py.
NOTE: sodium cyanide + acids/caustic are regulated hazmat (permits, DG shipping); NaCN
demand may be met domestically by Rustavi Azot (a producer). Captured as buyer notes.
"""
import html
import json
import os
import re
import time
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "docs", "research", "ge_chem_buyers.json")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) go4it-intel"

MINING = "Sulfuric acid, caustic soda, flocculants/coagulants, and flotation/leaching reagents"

# (a) curated, verified anchor accounts -----------------------------------------
ANCHORS = [
    {"company": "RMG - Rich Metals Group", "segment": "Mining - Cu/Au (flotation + leaching)",
     "city": "Kazreti / Bolnisi", "website": "richmetalsgroup.com",
     "chemicals": ["Sodium Isopropyl Xanthate (Z11)", "MIBC", "Potassium Amyl Xanthate (Z6)",
                   "Zinc Sulfate", "Copper Sulfate", "Sodium Sulfite", "Sulfuric Acid",
                   "Sodium Hydroxide", "Sodium Cyanide", "Anionic Polyacrylamide",
                   "Cationic Polyacrylamide", "Sodium Sulfide"]},
    {"company": "JSC RMG Copper", "segment": "Mining - copper flotation", "city": "Madneuli / Kazreti",
     "website": "madneuli.ge",
     "chemicals": ["Sodium Isopropyl Xanthate (Z11)", "MIBC", "Potassium Amyl Xanthate (Z6)",
                   "Zinc Sulfate", "Copper Sulfate", "Sodium Sulfite", "Sodium Sulfide",
                   "Sulfuric Acid", "Sodium Hydroxide", "Anionic Polyacrylamide", "Cationic Polyacrylamide"]},
    {"company": "RMG Gold LLC", "segment": "Mining - gold (CIL / heap leach)", "city": "Sakdrisi / Kvartsiti",
     "website": "richmetalsgroup.com",
     "chemicals": ["Sodium Cyanide", "Sodium Hydroxide", "Sulfuric Acid",
                   "Anionic Polyacrylamide", "Cationic Polyacrylamide"]},
    {"company": "Georgian Manganese LLC", "segment": "Mining/metallurgy - Mn beneficiation + ferroalloy",
     "city": "Chiatura / Zestafoni", "website": "gm.ge",
     "chemicals": ["Sulfuric Acid", "Sodium Hydroxide", "Anionic Polyacrylamide",
                   "Cationic Polyacrylamide", "Polyaluminum Chloride"]},
    {"company": "JSC Zestafoni Ferroalloy Plant", "segment": "Metallurgy - ferroalloys", "city": "Zestafoni",
     "website": "gaalloys.ge", "chemicals": ["Sulfuric Acid", "Sodium Hydroxide"]},
    {"company": "JSC Rustavi Azot", "segment": "Heavy chemical plant (buys acid/caustic; MAKES cyanide)",
     "city": "Rustavi", "website": "rustaviazot.ge", "chemicals": ["Sulfuric Acid", "Sodium Hydroxide"]},
    {"company": "Rustavi Steel", "segment": "Metallurgy - steel", "city": "Rustavi", "website": "",
     "chemicals": ["Sulfuric Acid", "Sodium Hydroxide"]},
    {"company": "Georgian Water & Power (GWP)", "segment": "Water utility", "city": "Tbilisi",
     "website": "gwp.ge",
     "chemicals": ["Polyaluminum Chloride", "Sulfuric Acid", "Sodium Hydroxide",
                   "Anionic Polyacrylamide", "Cationic Polyacrylamide"]},
    {"company": "United Water Supply Company of Georgia (UWSCG)", "segment": "Water utility - STATE (tenders)",
     "city": "Tbilisi (nationwide)", "website": "water.gov.ge",
     "chemicals": ["Polyaluminum Chloride", "Sulfuric Acid", "Sodium Hydroxide",
                   "Anionic Polyacrylamide", "Cationic Polyacrylamide"]},
    {"company": "Batumi Water LLC (Adjara)", "segment": "Water utility", "city": "Batumi", "website": "",
     "chemicals": ["Polyaluminum Chloride", "Sodium Hydroxide",
                   "Anionic Polyacrylamide", "Cationic Polyacrylamide"]},
    {"company": "Chemkraft", "segment": "Chemical importer/distributor (Iran-sourced)", "city": "Tbilisi",
     "website": "chemkraft.ir", "distributor": True, "chemicals": ["Sodium Hydroxide"]},
    {"company": "AFG Finance LLC (mining holding)", "segment": "Mining holding / investor",
     "city": "Tbilisi", "website": "afg.ge",
     "chemicals": ["Sodium Isopropyl Xanthate (Z11)", "MIBC", "Potassium Amyl Xanthate (Z6)",
                   "Zinc Sulfate", "Copper Sulfate", "Sodium Sulfite", "Sulfuric Acid",
                   "Sodium Hydroxide", "Sodium Cyanide", "Anionic Polyacrylamide",
                   "Cationic Polyacrylamide", "Sodium Sulfide"]},
]

# (b) directory long-tail --------------------------------------------------------
YELL_RUBRICS = [3767, 3630, 3766]   # chemical reagents · disinfectants · lab supplies
JUNK_HOST = ("yell.ge", "w3.org", "clarity.ms", "cdnjs", "cloudflare", "jquery",
             "bootstrap", "googleapis", "gstatic", "google.", "facebook", "twitter",
             "instagram", "youtube", "schema.org", "gmail.com", "maps.", "popper")
PHONE_RE = re.compile(r"(?:\+?995[\s\-]?)?(?:\(?0?32\)?|5\d{2})[\s\-]?\d{2}[\s\-]?\d{2}[\s\-]?\d{2}")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def get(url):
    for i in range(3):
        try:
            return urllib.request.urlopen(
                urllib.request.Request(url, headers={"User-Agent": UA}), timeout=25
            ).read().decode("utf-8", "ignore")
        except Exception as e:  # noqa: BLE001
            if i == 2:
                print(f"   ! {url} -> {e}")
                return ""
            time.sleep(1.5)
    return ""


def profile_contact(cid):
    h = get(f"https://www.yell.ge/company.php?lan=eng&id={cid}")
    if not h:
        return "", "", ""
    emails = [e for e in EMAIL_RE.findall(h) if not e.endswith((".png", ".jpg", ".css"))]
    email = next((e for e in emails if "sentry" not in e and "example" not in e), "")
    phones = PHONE_RE.findall(h)
    phone = phones[0].strip() if phones else ""
    sites = [u for u in re.findall(r'https?://[^\"\s<>]+', h)
             if not any(j in u.lower() for j in JUNK_HOST)]
    website = sites[0] if sites else ""
    return email, phone, website


def harvest_directory():
    out, seen = [], set()
    for rub in YELL_RUBRICS:
        h = get(f"https://www.yell.ge/companies.php?lan=eng&rub={rub}")
        pairs = re.findall(r'company\.php\?lan=eng&id=(\d+)"[^>]*>\s*([^<]{2,70})</a>', h)
        uniq = []
        for cid, name in pairs:
            if cid in seen:
                continue
            seen.add(cid)
            uniq.append((cid, html.unescape(name).strip()))
        print(f"   yell.ge rub={rub}: {len(uniq)} companies")
        for cid, name in uniq:
            email, phone, website = profile_contact(cid)
            out.append({
                "company": name, "segment": "Chemical importer/distributor (directory)",
                "city": "", "website": website, "email": email, "phone": phone,
                "distributor": True, "chemicals": [],   # a trader can source any of the 13
                "reagents": "distributor - can source any of the 13 (vet for industrial tonnage)",
                "needs_enrichment": not (email or phone),
            })
            time.sleep(0.4)
    return out


def run():
    buyers = []
    for a in ANCHORS:
        a = dict(a)
        a.setdefault("email", "")
        a.setdefault("phone", "")
        a.setdefault("distributor", False)
        a["reagents"] = ", ".join(a.get("chemicals", []))   # display string from the 13
        a["needs_enrichment"] = not (a.get("email") or a.get("phone"))
        a["source_tier"] = "anchor"
        buyers.append(a)

    print("=== directory long-tail (yell.ge) ===")
    for d in harvest_directory():
        d["source_tier"] = "directory"
        buyers.append(d)

    with_contact = [b for b in buyers if not b["needs_enrichment"]]
    result = {
        "section": "A - potential Georgian buyers",
        "total": len(buyers), "anchors": len(ANCHORS),
        "with_contact": len(with_contact),
        "buyers": buyers,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    print(f"\n{len(buyers)} Georgian buyers ({len(ANCHORS)} anchor accounts + "
          f"{len(buyers)-len(ANCHORS)} directory); {len(with_contact)} have a contact.")
    print("\nAnchor accounts:")
    for b in buyers[:len(ANCHORS)]:
        print(f"  {b['company'][:40]:<40} {b['segment'][:34]:<34} {b['website']}")
    print("\nDirectory sample:")
    for b in with_contact:
        if b["source_tier"] == "directory":
            print(f"  {b['company'][:34]:<34} {b['email'] or b['phone']}")
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    run()
