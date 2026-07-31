"""go4it Intelligence - paid customs-import ingest (Georgian chemical buyers).

    ./.venv/bin/python scripts/load_customs.py

The paid channel (founder approved). Subscribe to Volza / Trademo / ImportGenius / Seair,
export "buyers/importers in Georgia" per HS code as CSV, drop the file(s) into
docs/customs_inbox/, and run this. It maps each provider's columns tolerantly into buyer
records with importer + shipment date + HS + chemical, and writes docs/research/ge_customs.json
(loaded into go4it by scripts/load_chem.py, which sets Lead.posted_at from the shipment date).

Every Georgian company that has actually imported one of these reagents is a real buyer -
this is the highest-ROI way to complete the buyer universe. No API needed; it reads the
CSVs the founder exports. (Contact-level detail is often a higher provider tier - missing
phone/email is flagged needs_enrichment for a Clay pass.)

HS codes to query at the provider (per chemical):
  2807 sulfuric acid · 2815 sodium hydroxide · 2837.11 sodium cyanide ·
  2833.25 copper sulphate · 2833.29 zinc sulphate · 2832.10 sodium sulfite ·
  2830.10 sodium sulfide · 2827.32 aluminium chloride/PAC · 3906.90 polyacrylamide ·
  2905.19 MIBC · 2930.90 / 3824.99 xanthates & prepared flotation reagents.
"""
import csv
import glob
import io
import json
import os
import re
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INBOX = os.path.join(BASE, "docs", "customs_inbox")
OUT = os.path.join(BASE, "docs", "research", "ge_customs.json")

HS_MAP = [
    ("283711", "Sodium cyanide"), ("2837", "Cyanides"),
    ("283325", "Copper sulphate"), ("283329", "Zinc sulphate"), ("2833", "Metal sulphates"),
    ("283210", "Sodium sulfite"), ("283010", "Sodium sulfide"),
    ("282732", "Aluminium chloride / PAC"), ("2807", "Sulfuric acid"),
    ("281511", "Sodium hydroxide"), ("281512", "Sodium hydroxide"), ("2815", "Sodium hydroxide"),
    ("390690", "Polyacrylamide"), ("3906", "Acrylic polymer (PAM)"),
    ("290519", "MIBC (methyl isobutyl carbinol)"),
    ("293090", "Xanthate (organo-sulfur)"), ("382499", "Prepared flotation reagents"), ("3824", "Prepared reagents"),
]
ALIASES = {
    "buyer": ["importer", "importer name", "consignee", "consignee name", "buyer", "buyer name", "company"],
    "date": ["date", "shipment date", "bl date", "b/l date", "bill of lading date", "import date", "arrival date"],
    "hs": ["hs code", "hs", "hscode", "hs_code", "commodity code", "hts", "hts code", "tariff"],
    "product": ["product", "product description", "description", "goods description", "commodity", "goods", "item"],
    "quantity": ["quantity", "qty", "net weight", "weight", "volume", "gross weight"],
    "unit": ["unit", "uom"],
    "supplier": ["supplier", "exporter", "shipper", "seller", "exporter name"],
    "origin": ["origin", "country of origin", "origin country"],
    "value": ["value", "cif value", "fob value", "usd value", "amount", "total value"],
    "email": ["email", "e-mail", "importer email"],
    "phone": ["phone", "contact", "tel", "telephone", "mobile", "importer phone"],
    "website": ["website", "url", "web"],
    "address": ["address", "importer address"],
}


def hs_to_chem(hs):
    h = re.sub(r"\D", "", hs or "")
    for prefix, name in HS_MAP:
        if h.startswith(prefix):
            return name
    return "Chemical (HS " + (hs or "?") + ")"


def parse_date(s):
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y", "%d.%m.%Y", "%d-%b-%Y", "%d %b %Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s[:len(datetime.now().strftime(fmt)) + 4], fmt).date().isoformat()
        except ValueError:
            continue
    m = re.search(r"(20\d{2})", s)
    return f"{m.group(1)}-01-01" if m else ""


def header_map(fieldnames):
    present = {name: (name or "").strip().lower() for name in fieldnames}
    out = {}
    for field, al in ALIASES.items():
        for actual, low in present.items():
            if low in al:
                out[field] = actual
                break
    return out


def parse_text(text):
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return []
    hm = header_map(reader.fieldnames)
    rows = []
    for row in reader:
        g = lambda f: (row.get(hm[f], "") or "").strip() if f in hm else ""
        buyer = g("buyer")
        if not buyer:
            continue
        hs = g("hs")
        email, phone = g("email"), g("phone")
        rows.append({
            "company": buyer, "country": "GE",
            "hs": hs, "chemical": hs_to_chem(hs),
            "product": g("product"), "import_date": parse_date(g("date")),
            "quantity": g("quantity"), "unit": g("unit"),
            "supplier": g("supplier"), "origin": g("origin"), "value": g("value"),
            "email": email, "phone": phone, "website": g("website"),
            "needs_enrichment": not (email or phone),
        })
    return rows


def run():
    os.makedirs(INBOX, exist_ok=True)
    files = sorted(glob.glob(os.path.join(INBOX, "*.csv")))
    records, seen = [], set()
    for path in files:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            for r in parse_text(f.read()):
                key = (r["company"].lower(), r["hs"], r["import_date"])
                if key in seen:
                    continue
                seen.add(key)
                records.append(r)
        print(f"   parsed {os.path.basename(path)}")

    recent = [r for r in records if r["import_date"] >= "2025-01-01"]
    result = {"section": "customs (paid import records)", "files": len(files),
              "total": len(records), "since_2025": len(recent), "records": records}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    if not files:
        print(f"No CSVs in {INBOX}.")
        print("  -> Subscribe to Volza/Trademo/ImportGenius, export 'importers in Georgia'")
        print("     per HS code (see the list in this script's header), drop the CSV here,")
        print("     and re-run. Columns are matched tolerantly.")
    else:
        print(f"\n{len(records)} Georgian importer records ({len(recent)} since 2025) -> {OUT}")


if __name__ == "__main__":
    run()
