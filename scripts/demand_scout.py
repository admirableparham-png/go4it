"""Generic demand / RFQ scout for a product line — the re-runnable step that assembles a line's
active buy-requests into docs/research/<slug>_rfqs.json (what the hub, loader, and export read).

    ./.venv/bin/python scripts/demand_scout.py cd-dvd

It MERGES demand candidates from three sources, dedups them, keeps only recent (>= MIN_YEAR) or
undated ones, and writes the standard RFQ file. Re-running is additive — curated RFQs are never lost:

  1. the existing <slug> rfqs_file            (preserve what's already curated)
  2. a structured source for the dest country (STRUCTURED_SOURCES registry; Georgia procurement wired
                                               as the working example — real, keyless, automated)
  3. docs/research/lines/<slug>_rfq_candidates.json  (the OPEN-WEB search step's output)

Honest scope: fully-keyless open-web search from a plain script is unreliable (search engines block
bots), so the open-web half is fed via the candidates file — produced by a web-search pass (an agent
run, or manual research) in the SAME shape as an RFQ row. This script is the deterministic
normalize/dedup/filter/write half; structured procurement portals are fully automated.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.line_spec import LINES_DIR, load_spec, research_path  # noqa: E402

MIN_YEAR = 2025
RFQ_KEYS = ("buyer", "product", "city", "country", "posted", "phone", "email", "website",
            "note", "contact_gated")


def _year(posted):
    """Largest 4-digit year in a free-text `posted` field (e.g. 'active 2026', 'Jan 2025'), or None."""
    yrs = [int(y) for y in re.findall(r"(20\d{2})", str(posted or ""))]
    return max(yrs) if yrs else None


def _normalize(row, dest_iso):
    out = {k: row.get(k, "") for k in RFQ_KEYS}
    out["buyer"] = (out["buyer"] or "").strip()
    if not out["country"]:
        out["country"] = dest_iso
    out["contact_gated"] = bool(row.get("contact_gated")) or not (out["phone"] or out["email"] or out["website"])
    out["verified"] = bool(row.get("verified"))
    return out


def _domain(url):
    u = (url or "").strip().lower()
    u = re.sub(r"^https?://", "", u)
    u = u[4:] if u.startswith("www.") else u
    return u.split("/")[0].split("?")[0]


def _norm_buyer(name):
    name = re.sub(r"\(.*?\)", "", name or "")            # drop parentheticals like "(altimus.ae)"
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


def _key(row):
    """Same real company = same RFQ. Key on the website domain when present (robust to name
    variants like 'Altimus Office Supplies LLC' vs 'Altimus Office (altimus.ae)'), else the
    normalized buyer name."""
    dom = _domain(row.get("website"))
    if dom:
        return ("dom", dom)
    return ("buyer", _norm_buyer(row.get("buyer"))[:24])


# ---- structured, keyless procurement sources per destination country (auto). Return candidate rows.
def _ge_procurement_candidates(spec):
    """Georgia e-procurement tenders already harvested into ge_chem_rfqs.json / ge_tenders.json —
    the working example that a dest country's public tender portal plugs in here (real + keyless)."""
    rows = []
    for fname in ("ge_chem_rfqs.json", "ge_tenders.json"):
        p = research_path(fname)
        if not os.path.exists(p):
            continue
        for t in json.load(open(p, encoding="utf-8")).get("tenders", []):
            rows.append({"buyer": t.get("buyer") or t.get("org") or "", "product": t.get("title") or "",
                         "city": t.get("city") or "", "country": "GE",
                         "posted": t.get("posted") or t.get("deadline") or "",
                         "website": t.get("url") or "", "note": "public tender", "verified": True})
    return rows


STRUCTURED_SOURCES = {"GE": _ge_procurement_candidates}


def run(slug):
    spec = load_spec(slug)
    dest_iso = spec.get("dest", {}).get("iso", "")
    rfqs_path = research_path(spec["rfqs_file"])

    candidates = []
    # 1. preserve existing curated rfqs
    if os.path.exists(rfqs_path):
        candidates += json.load(open(rfqs_path, encoding="utf-8")).get("rfqs", [])
    # 2. structured source for the destination country
    src = STRUCTURED_SOURCES.get(dest_iso)
    if src:
        got = src(spec)
        print(f"structured source [{dest_iso}]: {len(got)} candidate(s)")
        candidates += got
    # 3. open-web candidates file (web-search step output)
    cand_path = os.path.join(LINES_DIR, f"{slug}_rfq_candidates.json")
    if os.path.exists(cand_path):
        blob = json.load(open(cand_path, encoding="utf-8"))
        got = blob.get("candidates") or blob.get("rfqs") or []
        print(f"web-search candidates file: {len(got)} candidate(s)")
        candidates += got

    seen, kept, dropped_old = {}, [], 0
    for row in candidates:
        norm = _normalize(row, dest_iso)
        if not norm["buyer"]:
            continue
        yr = _year(norm["posted"])
        if yr is not None and yr < MIN_YEAR:
            dropped_old += 1
            continue
        k = _key(norm)
        if k in seen:                       # dedup; prefer the row that has more contact info
            prev = seen[k]
            if sum(bool(norm[c]) for c in ("phone", "email", "website")) > \
               sum(bool(prev[c]) for c in ("phone", "email", "website")):
                kept[kept.index(prev)] = norm
                seen[k] = norm
            continue
        seen[k] = norm
        kept.append(norm)

    kept.sort(key=lambda r: (_year(r["posted"]) or 0), reverse=True)
    out = {
        "source": "demand-scout (structured portals + web-search candidates)",
        "note": f"Active buy-requests for '{spec.get('label', slug)}', deduped, {MIN_YEAR}+ or undated.",
        "total_named": len(kept), "rfqs": kept,
    }
    with open(rfqs_path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(f"[{slug}] demand scout -> {len(kept)} RFQs "
          f"({sum(1 for r in kept if r['verified'])} verified, {dropped_old} dropped as pre-{MIN_YEAR}) "
          f"-> {rfqs_path}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: demand_scout.py <line-slug>   (e.g. cd-dvd, decoration)")
        sys.exit(1)
    run(sys.argv[1])
