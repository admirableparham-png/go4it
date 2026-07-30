"""go4it Intelligence - Georgian public-procurement harvester (real, live tenders).

    ./.venv/bin/python scripts/harvest_ge_tenders.py [max_pages]

Pulls CURRENT tenders straight from Georgia's official e-procurement system
(tenders.procurement.gov.ge) via its public, no-login search app. This is real
demand you can't get from an LLM: named Georgian buyers (municipalities, schools,
sports/road agencies) actively purchasing, with budget + bid deadline.

How it works (reverse-engineered from the public app):
  GET  /public/                                  -> seed a PHP session cookie
  POST /public/library/qep/qep_controller.php    action=search_qep&page=N
       -> an HTML table of 5 announced tenders per page; each row already carries
          announcement no, buyer org, CPV category, estimated value (GEL), announce
          date and bid deadline. (No per-tender detail request needed.)

We paginate recent announcements and keep the ones whose CPV code (language-neutral)
or Georgian subject text matches our product areas:
  * road / cold-asphalt / pothole repair
  * playground / sports / rubber safety surfacing  (our winning product = rubber)

Writes docs/research/ge_tenders.json (committed backup) and prints a summary.
Matched buyers load into go4it as Leads via scripts/load_intel.py.
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import http.cookiejar

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from geo_en import cpv_en, geo_to_en  # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "docs", "research", "ge_tenders.json")

ROOT = "https://tenders.procurement.gov.ge/public/"
SEARCH = "https://tenders.procurement.gov.ge/public/library/qep/qep_controller.php"

MAX_PAGES = int(sys.argv[1]) if len(sys.argv) > 1 else 80   # 5 tenders/page
GAP = 0.5

# --- what we care about -------------------------------------------------------
# CPV (Common Procurement Vocabulary) prefixes - language-neutral, most reliable.
# Balanced: playground/sports-surfacing + municipal sports-agency goods (real buyers of
# rubber gym/playground tiles), while excluding pure-materials/waste buckets below.
CPV_TARGETS = {
    "cold-asphalt": ["44113", "45233"],                       # bitumen/asphalt materials, road works
    "rubber-tiles": ["37535", "45236", "45112720", "44112200",  # playground/surfacing works
                     "37400", "37410", "37450", "37415"],       # sports goods/equipment (municipal demand)
}
# Georgian subject-text signals (substring on the flattened row). Require a rubber-
# SURFACE / playground-construction phrase, not the bare word "rubber" (which also tags
# "rubber & plastic WASTE" tenders) or bare "sports".
KW_TARGETS = {
    "cold-asphalt": ["ასფალტ", "ბიტუმ", "ორმულ", "საგზაო", "გზის სარეაბ", "გზის მოწყ"],
    "rubber-tiles": ["რეზინის საფარ", "რეზინის ფილ", "რეზინის მოედ", "სათამაშო მოედ",
                     "საბავშვო მოედ", "მინი მოედ", "ხელოვნური საფარ", "ხელოვნური ბალახ",
                     "ტარტან", "სტადიონის მოწყ", "მოედნის მოწყ"],
}
# Hard excludes - drop rows about raw materials / scrap / waste even if a keyword hits.
EXCLUDE_TOKENS = ["ნარჩენ", "ჯართ", "მეორადი ნედლ"]   # waste / scrap / secondary raw


def opener():
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    op.addheaders = [("User-Agent", "Mozilla/5.0 go4it-intel"),
                     ("X-Requested-With", "XMLHttpRequest")]
    op.open(ROOT, timeout=30).read()        # seed session cookie
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


def parse_rows(html_text):
    rows = re.findall(r'<tr id="A\d+".*?</tr>', html_text, re.S)
    out = []
    for raw in rows:
        qid = grab(r'ShowQep\((\d+)\)', raw)
        t = flat(raw)
        cpv = grab(r'შესყიდვის კატეგორია:\s*([0-9]{6,8})', t)
        cpv_desc = grab(r'შესყიდვის კატეგორია:\s*[0-9]{6,8}\s*-\s*(.+?)\s*(?:შესყიდვის კატეგორია:|შესყიდვის სავარაუდო|$)', t)
        out.append({
            "qep_id": qid,
            "number": grab(r'ნომერი:\s*([A-Z]{2,5}\d+)', t),
            "status": grab(r'^\s*(\S[^|]*?)\s*(?:ელექტრონული|გამარტივ|კონსოლიდ)', t) or grab(r'(გამოცხადებულია|შეფასება|დასრულებული|მიმდინარე)', t),
            "buyer": grab(r'შემსყიდველი:\s*(.+?)\s*შესყიდვის კატეგორია:', t),
            "cpv": cpv,
            "cpv_desc": cpv_desc,
            "value_gel": grab(r'ღირებულება:\s*([\d.,]+)', t),
            "announced": grab(r'გამოცხადების თარიღი:\s*([\d.]+)', t),
            "deadline": grab(r'მიღების ვადა:\s*([\d.]+(?:\s+[\d:]+)?)', t),
            "_text": t,
        })
    return out


def classify(row):
    cpv = row["cpv"]
    txt = row["_text"]
    if any(x in txt for x in EXCLUDE_TOKENS):
        return None, None
    for pkey in ("cold-asphalt", "rubber-tiles"):
        if cpv and any(cpv.startswith(p) for p in CPV_TARGETS[pkey]):
            return pkey, "cpv"
        if any(kw in txt for kw in KW_TARGETS[pkey]):
            return pkey, "keyword"
    return None, None


def run():
    op = opener()
    seen, matched = set(), []
    scanned = 0
    for page in range(1, MAX_PAGES + 1):
        html_text = fetch_page(op, page)
        rows = parse_rows(html_text)
        if not rows:
            print(f"   page {page}: empty -> stop")
            break
        for r in rows:
            if r["number"] in seen:
                continue
            seen.add(r["number"])
            scanned += 1
            pkey, how = classify(r)
            if pkey:
                r.pop("_text", None)
                r["buyer"] = geo_to_en(r["buyer"])
                r["cpv_desc"] = cpv_en(r["cpv"]) or geo_to_en(r["cpv_desc"])
                r["status"] = geo_to_en(r.get("status", ""))
                r["product_key"] = pkey
                r["matched_by"] = how
                r["dest_country"] = "GE"
                matched.append(r)
        if page % 10 == 0:
            print(f"   ...page {page}: scanned {scanned}, matched {len(matched)}")
        time.sleep(GAP)

    for m in matched:
        m.pop("_text", None)
    result = {
        "source": "tenders.procurement.gov.ge (public e-procurement search)",
        "scanned_tenders": scanned, "matched": len(matched),
        "tenders": matched,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    print(f"\nScanned {scanned} live tenders, matched {len(matched)} on-target:")
    for m in matched:
        print(f"  [{m['product_key']:>12}] {m['number']:<14} CPV {m['cpv']:<8} "
              f"{m['value_gel'] or '?':>10} GEL  deadline {m['deadline']}")
        print(f"                 buyer: {m['buyer'][:70]}")
        print(f"                 subj:  {m['cpv_desc'][:70]}")
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    run()
