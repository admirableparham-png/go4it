"""Export the go4it real-source intelligence layer to CSV + a clickable HTML dashboard.

    ./.venv/bin/python scripts/export_intel.py

Reads the committed harvest datasets under docs/research/ and builds:
  docs/prospects/intel.csv    - flat, shareable table of every demand + supply record
  docs/prospects/intel.html   - a dashboard: (1) real market reality from customs data,
                                (2) live Georgian demand (tenders + venues + regional RFQs),
                                (3) UAE supply. Clickable phones/websites; sources omitted.

This is the human-facing output of the harvester system - the part that shows the data an
LLM alone can't produce: real import volumes/prices, live tenders, and named companies.
"""
import csv
import html
import json
import os
import re

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RES = os.path.join(BASE, "docs", "research")
OUT_DIR = os.path.join(BASE, "docs", "prospects")

PRODUCTS = {
    "cold-asphalt": ("Cold asphalt (bagged cold-mix)", "\U0001F6E3️", "2715"),
    "rubber-tiles": ("Rubber tiles (gym / outdoor)", "\U0001F7E9", "4016"),
    "pour-in-place-rubber": ("Pour-in-place rubber flooring", "\U0001F3C3", "4004"),
}
HS_TO_PKEY = {"2715": "cold-asphalt", "4016": "rubber-tiles", "4004": "pour-in-place-rubber"}


def load(name):
    p = os.path.join(RES, name)
    if not os.path.exists(p):
        return {}
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def esc(x):
    return html.escape(str(x or ""))


def as_url(u):
    u = (u or "").strip()
    return "" if not u else (u if "://" in u else "https://" + u)


def phone_link(p):
    if not p:
        return ""
    tel = re.sub(r"[^\d+]", "", p)
    return f'<a href="tel:{tel}">{esc(p)}</a>'


def web_link(w):
    u = as_url(w)
    return f'<a href="{esc(u)}" target="_blank" rel="noopener">website</a>' if u else ""


# ---------------------------------------------------------------- market cards
def market_cards(stats):
    by_hs = {}
    for c in stats.get("commodities", []):
        by_hs.setdefault(c["hs_code"], c)
    cards = []
    for hs, pkey in HS_TO_PKEY.items():
        c = by_hs.get(hs)
        if not c:
            continue
        disp, emoji, _ = PRODUCTS[pkey]
        yrs = c.get("years", {})
        last_yr = max(yrs) if yrs else None
        latest = yrs.get(last_yr, {}) if last_yr else {}
        cheapest = c.get("cheapest_source") or {}
        iran = c.get("iran_present")
        uae = c.get("uae_present")
        trend = c.get("trend_pct_first_to_last")
        rows = []
        if latest:
            rows.append(("Georgia imports " + str(last_yr),
                         f"{latest.get('tonnes','?')} t  /  ${latest.get('cif_usd',0):,}"))
            rows.append(("Landed price", f"{latest.get('unit_price_usd_kg','?')} $/kg CIF"))
        if trend is not None:
            rows.append(("Trend (multi-year)", f"{'+' if trend>=0 else ''}{trend}%"))
        if cheapest:
            rows.append(("Cheapest source now",
                         f"{cheapest.get('country','?')} @ {cheapest.get('unit_price_usd_kg','?')} $/kg"))
        verdict, cls = "", "warn"
        if iran:
            rows.append(("Iran in this market",
                         f"YES - {iran.get('unit_price_usd_kg','?')} $/kg, {iran.get('tonnes_total','?')} t"))
            if cheapest and cheapest.get("country") == "Iran":
                verdict = "Iran is already the CHEAPEST supplier to Georgia - proven live lane."
                cls = "good"
            else:
                verdict = "Iran already present - competitive lane."
                cls = "good"
        else:
            rows.append(("Iran in this market", "no"))
            verdict = "Iran absent; cheap regional bulk (Turkmenistan/Russia/Turkey) owns it - marginal for us."
            cls = "bad"
        if uae:
            rows.append(("UAE in this market", "yes"))
        body = "".join(f'<div class="orow"><span class="ok">{esc(k)}</span>'
                       f'<span class="ov">{esc(v)}</span></div>' for k, v in rows)
        cards.append(
            f'<div class="opp {cls}"><div class="otitle">{emoji} {esc(disp)} '
            f'<span class="hs">HS {esc(hs)}</span></div>{body}'
            f'<div class="verdict">{esc(verdict)}</div></div>')
    return "".join(cards)


def demand_section(tenders, businesses):
    parts = []
    # live tenders
    tl = tenders.get("tenders", [])
    if tl:
        rows = "".join(
            f'<tr><td>{esc(t.get("buyer"))}</td><td>{esc(t.get("cpv_desc"))[:60]}</td>'
            f'<td>{esc(t.get("number"))}</td><td class="hot">{esc(t.get("deadline"))}</td></tr>'
            for t in tl)
        parts.append(f'<h3>\U0001F3DB️ Live Georgian tenders <span class="count">{len(tl)}</span></h3>'
                     f'<div class="scroll"><table><thead><tr><th>Buyer (public body)</th><th>Subject</th>'
                     f'<th>Tender no.</th><th>Bid deadline</th></tr></thead><tbody>{rows}</tbody></table></div>'
                     '<p class="fine">Scanned '
                     f'{tenders.get("scanned_tenders","?")} live tenders; these matched road / '
                     'playground / sports-surfacing. Re-run daily to catch new ones.</p>')
    # OSM venues
    bl = [b for b in businesses.get("businesses", []) if b.get("product_key") == "rubber-tiles"]
    withc = [b for b in bl if not b.get("needs_enrichment")]
    if bl:
        cards = "".join(
            f'<div class="card"><div class="name">{esc(b.get("company"))}</div>'
            f'<div class="loc">{esc(b.get("role"))}{" · "+esc(b.get("city")) if b.get("city") else ""}</div>'
            f'<div class="contact">{phone_link(b.get("phone"))} {web_link(b.get("website"))}</div></div>'
            for b in (withc[:60]))
        parts.append(
            f'<h3>\U0001F3CB️ Georgian gyms & sports venues (rubber-tile buyers) '
            f'<span class="count">{len(bl)}</span></h3>'
            f'<p class="fine">{len(withc)} have a phone/website (shown below); '
            f'{len(bl)-len(withc)} more are named venues awaiting contact enrichment.</p>'
            f'<div class="grid">{cards}</div>')
    return "".join(parts)


def supply_section(sup):
    uae = [s for s in sup.get("suppliers", []) if s.get("country") == "AE"]
    by_pk = {}
    for s in uae:
        by_pk.setdefault(s.get("product_key", "rubber-tiles"), []).append(s)
    parts = [f'<h3>\U0001F1E6\U0001F1EA UAE suppliers (quotable) <span class="count">{len(uae)}</span></h3>']
    for pkey, items in by_pk.items():
        disp = PRODUCTS.get(pkey, (pkey, "", ""))[0]
        cards = "".join(
            f'<div class="card"><div class="name">{esc(s.get("company"))}</div>'
            f'<div class="loc">{esc(s.get("city") or "UAE")}'
            f'{" · est. "+esc(s.get("established")) if s.get("established") else ""}</div>'
            f'<div class="contact">{phone_link((s.get("phones") or [""])[0])}</div></div>'
            for s in items)
        parts.append(f'<h4>{esc(disp)} <span class="count">{len(items)}</span></h4>'
                     f'<div class="grid">{cards}</div>')
    return "".join(parts)


def run():
    stats = load("trade_stats_georgia.json")
    tenders = load("ge_tenders.json")
    businesses = load("ge_businesses.json")
    sup = load("suppliers_uae_iran.json")

    css = """
    :root{color-scheme:light}*{box-sizing:border-box}
    body{margin:0;background:#0b1220;color:#0f172a;font:14px/1.55 system-ui,-apple-system,Segoe UI,sans-serif}
    .wrap{max-width:1200px;margin:0 auto;padding:26px 18px 70px}
    header{background:linear-gradient(135deg,#0f172a,#1e3a8a);color:#fff;border-radius:16px;padding:24px 26px;margin-bottom:22px}
    header h1{margin:0 0 4px;font-size:23px}header .sub{color:#c7d2fe;font-size:13.5px}
    section{background:#f1f5f9;border-radius:16px;padding:18px 20px;margin-bottom:18px}
    h2{margin:2px 0 12px;font-size:19px;color:#0f172a}
    h3{margin:20px 0 8px;font-size:15px;color:#1e293b}h4{margin:12px 0 6px;font-size:13px;color:#475569}
    .count{background:#dbeafe;color:#1d4ed8;border-radius:999px;padding:1px 9px;font-size:12px;margin-left:6px}
    .opps{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px}
    .opp{border-radius:12px;padding:14px 16px;color:#e2e8f0;background:#0f172a}
    .opp.good{background:linear-gradient(160deg,#064e3b,#0f172a);border:1px solid #10b981}
    .opp.bad{background:linear-gradient(160deg,#4c1d1d,#0f172a);border:1px solid #ef4444}
    .opp.warn{background:linear-gradient(160deg,#4a3908,#0f172a);border:1px solid #f59e0b}
    .otitle{font-weight:700;font-size:15px;margin-bottom:8px}.hs{font-size:11px;color:#94a3b8;font-weight:500}
    .orow{display:flex;justify-content:space-between;gap:10px;border-bottom:1px solid rgba(148,163,184,.2);padding:3px 0;font-size:12px}
    .ok{color:#94a3b8}.ov{color:#f1f5f9;font-weight:600;text-align:right}
    .verdict{margin-top:9px;font-size:12.5px;font-weight:600;color:#fbbf24}
    .opp.good .verdict{color:#6ee7b7}.opp.bad .verdict{color:#fca5a5}
    .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:10px}
    .card{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:10px 12px}
    .name{font-weight:700;font-size:13.5px}.loc{color:#64748b;font-size:11.5px;margin:2px 0}
    .contact{font-size:12.5px}.contact a{color:#0369a1;text-decoration:none;margin-right:8px}
    table{width:100%;border-collapse:collapse;font-size:12.5px;background:#fff;border-radius:8px;overflow:hidden}
    th,td{text-align:left;padding:7px 9px;border-bottom:1px solid #e2e8f0}th{background:#e2e8f0}
    .hot{color:#b91c1c;font-weight:700}.scroll{overflow-x:auto}
    .rfq{font-size:12.5px;color:#334155}.fine{font-size:11.5px;color:#64748b;margin:4px 0 8px}
    """
    n_venues = len([b for b in businesses.get("businesses", []) if b.get("product_key") == "rubber-tiles"])
    n_uae = len([s for s in sup.get("suppliers", []) if s.get("country") == "AE"])
    n_tenders = len(tenders.get("tenders", []))
    doc = (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>go4it - Market Intelligence</title><style>" + css + "</style></head><body><div class=wrap>"
        "<header><h1>go4it - Market Intelligence</h1>"
        f"<div class=sub>Real-source harvest &middot; {n_tenders} live tenders &middot; {n_venues} Georgian venues "
        f"&middot; {n_uae} UAE suppliers &middot; customs-verified market data. Not LLM guesses - pulled from "
        "customs records, the state procurement portal, OpenStreetMap and B2B directories.</div></header>"
        "<section><h2>1 &middot; Market reality (Georgia customs records)</h2>"
        "<div class=opps>" + market_cards(stats) + "</div></section>"
        "<section><h2>2 &middot; Live Georgian demand</h2>" + demand_section(tenders, businesses) + "</section>"
        "<section><h2>3 &middot; Supply we can quote</h2>" + supply_section(sup) + "</section>"
        "</div></body></html>"
    )
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "intel.html"), "w", encoding="utf-8") as f:
        f.write(doc)

    # ---- flat CSV ----
    with open(os.path.join(OUT_DIR, "intel.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Type", "Product", "Company/Buyer", "Country", "City", "Phone", "Website", "Detail"])
        for t in tenders.get("tenders", []):
            w.writerow(["GE tender", PRODUCTS.get(t.get("product_key"), ("",))[0], t.get("buyer"), "GE",
                        "", "", "", f"{t.get('number')} | deadline {t.get('deadline')} | CPV {t.get('cpv')}"])
        for b in businesses.get("businesses", []):
            if b.get("product_key") != "rubber-tiles":
                continue
            w.writerow(["GE venue", "Rubber tiles (buyer)", b.get("company"), "GE", b.get("city"),
                        b.get("phone"), as_url(b.get("website")), b.get("role")])
        for s in sup.get("suppliers", []):
            if s.get("country") != "AE":
                continue
            w.writerow(["UAE supplier", PRODUCTS.get(s.get("product_key"), ("",))[0], s.get("company"), "AE",
                        s.get("city"), (s.get("phones") or [""])[0], "", s.get("location")])

    print(f"wrote {os.path.join(OUT_DIR,'intel.html')}")
    print(f"wrote {os.path.join(OUT_DIR,'intel.csv')}")


if __name__ == "__main__":
    run()
