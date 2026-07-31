"""Export the UAE decor-buyer research to a source-less HTML + CSV.

    ./.venv/bin/python scripts/export_uae_decor_buyers.py

Reads docs/research/uae_decor_buyers.json (+ decora_products.json + uae_decor_rfqs.json)
and writes docs/prospects/uae_decor_buyers.{html,csv}:
  - what we offer (Decora families)
  - HIGH-RATE MATCHES (top match-scored buyers: real serveware/gift/decor/hospitality buyers)
  - ACTIVE RFQs (UAE buyers with posted buy-requests, if any were found)
  - all buyers grouped by category, each showing WHAT they'd buy from us + phone/email/website
Provenance is omitted (founder preference); contacts are clickable.
"""
import csv
import html
import json
import os
import re

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RES = os.path.join(BASE, "docs", "research")
OUT_DIR = os.path.join(BASE, "docs", "prospects")


def load(name):
    p = os.path.join(RES, name)
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else {}


def esc(x):
    return html.escape(str(x if x is not None else ""))


def as_url(u):
    u = (u or "").strip()
    return "" if not u else (u if "://" in u else "https://" + u)


def contact_html(phone, email, website):
    bits = []
    if phone:
        bits.append(f'<a href="tel:{esc(re.sub(r"[^0-9+]", "", phone))}">{esc(phone)}</a>')
    if email:
        bits.append(f'<a href="mailto:{esc(email)}">{esc(email)}</a>')
    if website:
        bits.append(f'<a href="{esc(as_url(website))}" target="_blank" rel="noopener">website</a>')
    return " · ".join(bits) or '<span class="enrich">needs enrichment</span>'


def buys_html(b):
    fams = b.get("buys") or b.get("fits") or []
    return "".join(f'<span class="chip">{esc(f)}</span>' for f in fams)


def buyer_card(b, show_score=False):
    name = esc(b.get("company"))
    web = as_url(b.get("website"))
    name_html = f'<a href="{esc(web)}" target="_blank" rel="noopener">{name}</a>' if web else name
    sc = b.get("match_score")
    badge = f'<span class="score">{sc}</span>' if (show_score and sc is not None) else ""
    return (f'<div class="card"><div class="name">{name_html} {badge}</div>'
            f'<div class="loc">{esc(", ".join(b.get("categories", [])) or "UAE")} · {esc(b.get("city") or "UAE")}</div>'
            f'<div class="contact">{contact_html((b.get("phones") or [""])[0], b.get("email"), b.get("website"))}</div>'
            f'<div class="buys"><span class="lab">buys:</span> {buys_html(b)}</div></div>')


def run():
    data = load("uae_decor_buyers.json")
    buyers = data.get("buyers", [])
    prod = load("decora_products.json")
    store = prod.get("store", {})
    families = prod.get("families", [])
    rfqs = load("uae_decor_rfqs.json").get("rfqs", [])

    # ---- product families ----
    fam_cards = "".join(
        f'<div class="fam"><div class="famname">{esc(f.get("name"))} '
        f'<span class="price">${esc(f.get("price_usd"))}</span></div>'
        f'<ul>{"".join(f"<li>{esc(i)}</li>" for i in f.get("items", []))}</ul>'
        f'<div class="fit">Buyers: {esc(" · ".join(f.get("fit_buyers", [])))}</div></div>'
        for f in families)

    # ---- high-rate matches ----
    ranked = sorted([b for b in buyers if b.get("match_score", 0) >= 75],
                    key=lambda x: -x.get("match_score", 0))
    high_html = "".join(buyer_card(b, show_score=True) for b in ranked[:60])
    high_sec = (f'<section><h2>\U0001F525 High-rate matches <span class="count">{len(ranked)}</span></h2>'
                f'<p class="note">Best product-fit (real serveware / gift / decor / hospitality buyers, '
                f'scored above the interior/maintenance firms). Showing top {min(60, len(ranked))}.</p>'
                f'<div class="grid">{high_html}</div></section>')

    # ---- active RFQs ----
    if rfqs:
        rows = "".join(
            f'<tr><td>{esc(q.get("buyer"))}</td><td>{esc(q.get("product"))}</td>'
            f'<td>{esc(q.get("city") or q.get("country"))}</td>'
            f'<td>{contact_html(q.get("phone"), q.get("email"), q.get("website"))}</td>'
            f'<td>{esc(q.get("posted"))}</td></tr>' for q in rfqs)
        rfq_sec = (f'<section><h2>\U0001F4E2 Active buy-requests (RFQs) <span class="count">{len(rfqs)}</span></h2>'
                   f'<div class="scroll"><table><thead><tr><th>Buyer</th><th>Wants</th><th>Where</th>'
                   f'<th>Contact</th><th>Posted</th></tr></thead><tbody>{rows}</tbody></table></div></section>')
    else:
        rfq_sec = ('<section><h2>\U0001F4E2 Active buy-requests (RFQs) <span class="count">0</span></h2>'
                   '<p class="note">No public UAE home-decor RFQs found on the B2B boards scanned '
                   '(they are gated / India-heavy). The high-rate matches above are the actionable demand.</p></section>')

    # ---- all buyers grouped ----
    groups = {}
    for b in buyers:
        groups.setdefault((b.get("categories") or ["Other"])[0], []).append(b)
    order = ["Home-decor retailer", "Tableware / serveware", "Crockery", "Housewares",
             "Handicrafts", "Lighting shop", "Gift / corporate-gift", "Gift shop",
             "Hotel & hospitality supplier"]
    grouped = ""
    for cat in order + [c for c in groups if c not in order]:
        items = sorted(groups.get(cat, []), key=lambda x: -x.get("match_score", 0))
        if not items:
            continue
        grouped += (f'<h3>{esc(cat)} <span class="count">{len(items)}</span></h3>'
                    f'<div class="grid">{"".join(buyer_card(b, show_score=True) for b in items)}</div>')

    css = """
    :root{color-scheme:light}*{box-sizing:border-box}
    body{margin:0;background:#f1f5f9;color:#0f172a;font:14px/1.55 system-ui,-apple-system,Segoe UI,sans-serif}
    .wrap{max-width:1200px;margin:0 auto;padding:26px 20px 60px}
    h1{margin:0 0 4px;font-size:23px}.sub{color:#64748b;margin-bottom:18px;font-size:13px}
    h2{font-size:18px;border-bottom:2px solid #e2e8f0;padding-bottom:6px;margin:26px 0 10px}
    h3{font-size:14px;color:#334155;margin:18px 0 10px}
    .note{color:#64748b;font-size:12px;margin:0 0 10px}
    .count{background:#e2e8f0;color:#475569;border-radius:999px;padding:1px 9px;font-size:12px;margin-left:6px}
    .fams{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:12px;margin-bottom:8px}
    .fam{background:#0f172a;color:#e2e8f0;border-radius:12px;padding:12px 14px}
    .famname{font-weight:700;font-size:14px;margin-bottom:6px}.price{font-size:12px;color:#fbbf24}
    .fam ul{margin:0 0 8px;padding-left:16px;font-size:11.5px;color:#cbd5e1}.fam li{margin:1px 0}
    .fit{font-size:11px;color:#94a3b8;border-top:1px solid #1e293b;padding-top:6px}
    .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:10px}
    .card{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:10px 12px}
    .name{font-weight:700;font-size:13.5px}.name a{color:#0369a1;text-decoration:none}
    .score{float:right;font-size:11px;font-weight:700;background:#dcfce7;color:#15803d;border-radius:6px;padding:0 6px}
    .loc{color:#64748b;font-size:11px;margin:2px 0}
    .contact{font-size:12px;margin-bottom:4px}.contact a{color:#0369a1;text-decoration:none;margin-right:4px}
    .buys{font-size:11px}.buys .lab{color:#94a3b8}
    .chip{display:inline-block;font-size:10px;background:#e0f2fe;color:#0369a1;border:1px solid #bae6fd;border-radius:6px;padding:0 6px;margin:1px}
    .enrich{color:#b45309}
    table{width:100%;border-collapse:collapse;font-size:12.5px;background:#fff;border-radius:8px;overflow:hidden}
    th,td{text-align:left;padding:7px 9px;border-bottom:1px solid #e2e8f0}th{background:#e2e8f0}
    .scroll{overflow-x:auto}
    """
    doc = ("<!doctype html><html lang=en><head><meta charset=utf-8>"
           "<meta name=viewport content='width=device-width,initial-scale=1'>"
           "<title>go4it - UAE buyers (Decora)</title><style>" + css + "</style></head><body><div class=wrap>"
           "<h1>UAE buyers - Decora home-decor range</h1>"
           f"<div class=sub>{esc(store.get('name'))} ({esc(store.get('material'))}, ${esc(store.get('price_band_usd'))}) "
           f"&middot; {len(buyers)} buyers &middot; {data.get('with_phone', 0)} phone &middot; "
           f"{data.get('with_email', 0)} email &middot; {data.get('with_website', 0)} website</div>"
           "<h2>What we offer</h2><div class=fams>" + fam_cards + "</div>"
           + high_sec + rfq_sec
           + "<section><h2>All UAE buyers</h2>" + grouped + "</section>"
           "</div></body></html>")

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "uae_decor_buyers.html"), "w", encoding="utf-8") as f:
        f.write(doc)

    with open(os.path.join(OUT_DIR, "uae_decor_buyers.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Match", "Category", "Company", "City", "Phone", "Email", "Website", "Buys (our products)"])
        for b in sorted(buyers, key=lambda x: -x.get("match_score", 0)):
            w.writerow([b.get("match_score", ""), " / ".join(b.get("categories", [])),
                        b.get("company", ""), b.get("city", "") or b.get("location", ""),
                        (b.get("phones") or [""])[0], b.get("email", ""), as_url(b.get("website")),
                        " / ".join(b.get("buys", []) or b.get("fits", []))])

    print(f"wrote {os.path.join(OUT_DIR, 'uae_decor_buyers.html')}")
    print(f"  buyers={len(buyers)} high-matches={len(ranked)} rfqs={len(rfqs)} "
          f"email={data.get('with_email', 0)} website={data.get('with_website', 0)}")


if __name__ == "__main__":
    run()
