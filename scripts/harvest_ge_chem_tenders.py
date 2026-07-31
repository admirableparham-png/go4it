"""go4it Intelligence - Georgian public-procurement harvester for CHEMICALS (Section B).

    ./.venv/bin/python scripts/harvest_ge_chem_tenders.py [max_pages]

Section B of the "Georgia chemical buyers" job: real, dated, contactable BUY-REQUESTS
from Georgia's official e-procurement portal (tenders.procurement.gov.ge) for the
mineral-processing + water-treatment chemicals on the founder's list.

Reality (verified): the international B2B buy-lead boards carry ~zero fresh Georgian
chemical RFQs, and the mining flotation/leaching reagents are never publicly tendered
(private mines). What DOES appear here is the WATER-TREATMENT subset (PAC, aluminium
sulphate, flocculants, caustic, acid, chlorine) bought by state water utilities and
municipalities - plus the occasional acid/caustic tender from industry.

Design notes:
  * The portal's public AJAX search ignores date/keyword filter params (only paginates
    newest-first), so we scan the newest pages and filter client-side.
  * Only OPEN tenders are actionable (you can't bid on an expired one), and open tenders
    are recent by definition -> a bounded recent scan is both feasible and correct.
  * FRESHNESS: keep only announced >= 2025-01-01 (the founder's rule); also flag `open`
    (bid deadline >= today). Stop early if we page back past 2025.

Writes docs/research/ge_chem_rfqs.json. Loaded into go4it by scripts/load_chem.py.
"""
import html
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from geo_en import cpv_en, geo_to_en  # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "docs", "research", "ge_chem_rfqs.json")

ROOT = "https://tenders.procurement.gov.ge/public/"
SEARCH = "https://tenders.procurement.gov.ge/public/library/qep/qep_controller.php"

MAX_PAGES = int(sys.argv[1]) if len(sys.argv) > 1 else 300   # 5 tenders/page (~1500 newest)
GAP = 0.5
CUTOFF = date(2025, 1, 1)                                     # keep announced >= this
TODAY = date.today()

# Georgian subject-text signal -> the chemical it means (precise; keywords beat CPV here).
KW_CHEM = {
    "გოგირდმჟავა": "Sulfuric acid",
    "კაუსტიკური სოდა": "Caustic soda (NaOH)",
    "ნატრიუმის ჰიდროქსიდ": "Sodium hydroxide",
    "ალუმინის სულფატ": "Aluminium sulphate",
    "პოლიალუმინის ქლორიდ": "Polyaluminium chloride (PAC)",
    "ფლოკულანტ": "Flocculant",
    "კოაგულანტ": "Coagulant",
    "პოლიაკრილამიდ": "Polyacrylamide",
    "ჰიპოქლორიტ": "Hypochlorite",
    "სპილენძის სულფატ": "Copper sulphate",
    "თუთიის სულფატ": "Zinc sulphate",
    "ნატრიუმის სულფიტ": "Sodium sulfite",
    "ნატრიუმის სულფიდ": "Sodium sulfide",
    "ციანიდ": "Sodium cyanide",
    "ქსანტოგენატ": "Xanthate",
    "ქსანტატ": "Xanthate",
    "ფლოტაცი": "Flotation reagent",
    "წყლის დამუშავებ": "Water-treatment chemicals",
    "წყლის გამწმენდ": "Water-treatment chemicals",
    "ქიმიური რეაგენტ": "Chemical reagent",
}
# CPV prefixes for chemical products. The portal uses the ROUNDED division codes, so match
# broadly: 243* = basic inorganic/organic chemicals (acids, caustic, sulfates, cyanide),
# 249* = fine/various chemical products (incl. 24962 water-treatment chemicals).
CPV_CHEM = ["243", "249"]
# Drop obvious non-purchases even if a token hits.
EXCLUDE_TOKENS = ["ნარჩენ", "ჯართ"]   # waste / scrap


def opener():
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    op.addheaders = [("User-Agent", "Mozilla/5.0 go4it-intel"),
                     ("X-Requested-With", "XMLHttpRequest")]
    op.open(ROOT, timeout=30).read()
    return op


def fetch_page(op, page):
    data = urllib.parse.urlencode({"action": "search_qep", "page": page, "search": ""}).encode()
    for attempt in range(3):
        try:
            return op.open(SEARCH, data=data, timeout=30).read().decode("utf-8", "ignore")
        except Exception as e:  # noqa: BLE001
            if attempt == 2:
                print(f"   ! page {page} failed: {e}")
                return ""
            time.sleep(1.5)
    return ""


def flat(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)).strip()


def grab(pat, text, g=1):
    m = re.search(pat, text)
    return m.group(g).strip() if m else ""


def parse_date(s):
    m = re.match(r"(\d{2})\.(\d{2})\.(\d{4})", s or "")
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


def parse_rows(html_text):
    rows = re.findall(r'<tr id="A\d+".*?</tr>', html_text, re.S)
    out = []
    for raw in rows:
        t = flat(raw)
        cpv = grab(r"შესყიდვის კატეგორია:\s*([0-9]{6,8})", t)
        out.append({
            "number": grab(r"ნომერი:\s*([A-Z]{2,5}\d+)", t),
            "status": grab(r"(გამოცხადებულია|შეფასება|დასრულებული|მიმდინარე)", t),
            "buyer": grab(r"შემსყიდველი:\s*(.+?)\s*შესყიდვის კატეგორია:", t),
            "cpv": cpv,
            "cpv_desc": grab(r"შესყიდვის კატეგორია:\s*[0-9]{6,8}\s*-\s*(.+?)\s*(?:შესყიდვის კატეგორია:|შესყიდვის სავარაუდო|$)", t),
            "announced": grab(r"გამოცხადების თარიღი:\s*([\d.]+)", t),
            "deadline": grab(r"მიღების ვადა:\s*([\d.]+(?:\s+[\d:]+)?)", t),
            "_text": t,
        })
    return out


def classify(row):
    """Return (chemical_label, matched_by) or (None, None)."""
    txt = row["_text"]
    if any(x in txt for x in EXCLUDE_TOKENS):
        return None, None
    for kw, chem in KW_CHEM.items():
        if kw in txt:
            return chem, "keyword"
    cpv = row["cpv"]
    if cpv and any(cpv.startswith(p) for p in CPV_CHEM):
        return "Chemical (unspecified)", "cpv"
    return None, None


def run():
    op = opener()
    seen, matched = set(), []
    scanned, old_streak = 0, 0
    for page in range(1, MAX_PAGES + 1):
        rows = parse_rows(fetch_page(op, page))
        if not rows:
            print(f"   page {page}: empty -> stop")
            break
        page_dates = [d for d in (parse_date(r["announced"]) for r in rows) if d]
        for r in rows:
            if r["number"] in seen:
                continue
            seen.add(r["number"])
            scanned += 1
            chem, how = classify(r)
            d = parse_date(r["announced"])
            if not chem or not d or d < CUTOFF:      # on-target AND fresh (>=2025)
                continue
            dl = parse_date(r["deadline"])
            r.pop("_text", None)
            r["chemical"] = chem
            r["matched_by"] = how
            r["buyer"] = geo_to_en(html.unescape(r["buyer"]))
            r["cpv_desc"] = cpv_en(r["cpv"]) or geo_to_en(html.unescape(r["cpv_desc"]))
            r["announced_iso"] = d.isoformat()
            r["open"] = bool(dl and dl >= TODAY)
            r["dest_country"] = "GE"
            matched.append(r)
        # stop once we've paged back before 2025 (newest date on page < cutoff)
        newest = max(page_dates) if page_dates else None
        old_streak = old_streak + 1 if (newest and newest < CUTOFF) else 0
        if old_streak >= 2:
            print(f"   page {page}: paged past {CUTOFF} -> stop")
            break
        if page % 20 == 0:
            print(f"   ...page {page}: scanned {scanned}, matched {len(matched)}")
        time.sleep(GAP)

    for m in matched:
        m.pop("_text", None)
    open_n = sum(1 for m in matched if m["open"])
    result = {
        "source": "tenders.procurement.gov.ge (public e-procurement)",
        "scanned": scanned, "matched": len(matched), "open_now": open_n,
        "freshness": f">= {CUTOFF.isoformat()}", "tenders": matched,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    print(f"\nScanned {scanned} tenders; {len(matched)} chemical RFQs since {CUTOFF} "
          f"({open_n} still open):")
    for m in matched:
        flag = "OPEN " if m["open"] else "closed"
        print(f"  [{flag}] {m['announced']} {m['chemical'][:26]:<26} "
              f"deadline {m['deadline']:<17} {m['number']}")
        print(f"          buyer: {m['buyer'][:66]}")
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    run()
