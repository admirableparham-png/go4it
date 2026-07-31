"""Export the UAE decor-buyer research to a source-less HTML + CSV.

    ./.venv/bin/python scripts/export_uae_decor_buyers.py

Reads docs/research/uae_decor_buyers.json + decora_products.json and writes:
  docs/prospects/uae_decor_buyers.csv    (flat, shareable, NO source column)
  docs/prospects/uae_decor_buyers.html   (what we offer + UAE buyers grouped by type)

The header shows the Decora product families we're offering; then UAE buyers grouped by
category (home-decor / tableware / hotel-supplies / gift-shops...) with clickable phones.
Provenance is intentionally omitted (founder preference).
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


def contact_html(phone, website):
    bits = []
    if phone:
        bits.append(f'<a href="tel:{esc(re.sub(r"[^0-9+]", "", phone))}">{esc(phone)}</a>')
    if website:
        u = as_url(website)
        bits.append(f'<a href="{esc(u)}" target="_blank" rel="noopener">website</a>')
    return " · ".join(bits) or '<span class="enrich">phone/enrich</span>'


def run():
    data = load("uae_decor_buyers.json")
    buyers = data.get("buyers", [])
    prod = load("decora_products.json")
    store = prod.get("store", {})
    families = prod.get("families", [])

    # ---- product families header ----
    fam_cards = []
    for f in families:
        items = "".join(f'<li>{esc(i)}</li>' for i in f.get("items", []))
        fits = " · ".join(esc(x) for x in f.get("fit_buyers", []))
        fam_cards.append(
            f'<div class="fam"><div class="famname">{esc(f.get("name"))} '
            f'<span class="price">${esc(f.get("price_usd"))}</span></div>'
            f'<ul>{items}</ul><div class="fit">Buyers: {fits}</div></div>')

    # ---- buyers grouped by primary category ----
    groups = {}
    for b in buyers:
        cat = (b.get("categories") or ["Other"])[0]
        groups.setdefault(cat, []).append(b)
    order = ["Home-decor retailer", "Tableware / serveware", "Crockery", "Housewares",
             "Handicrafts", "Lighting shop", "Gift / corporate-gift", "Gift shop",
             "Hotel & hospitality supplier"]
    sections = []
    for cat in order + [c for c in groups if c not in order]:
        items = groups.get(cat)
        if not items:
            continue
        cards = "".join(
            f'<div class="card"><div class="name">'
            + (f'<a href="{esc(as_url(b.get("website")))}" target="_blank" rel="noopener">{esc(b.get("company"))}</a>'
               if b.get("website") else esc(b.get("company")))
            + '</div>'
            f'<div class="loc">{esc(b.get("city") or b.get("location") or "UAE")}</div>'
            f'<div class="contact">{contact_html((b.get("phones") or [""])[0], b.get("website"))}</div></div>'
            for b in items)
        sections.append(
            f'<section><h3>{esc(cat)} <span class="count">{len(items)}</span></h3>'
            f'<div class="grid">{cards}</div></section>')

    css = """
    :root{color-scheme:light}*{box-sizing:border-box}
    body{margin:0;background:#f1f5f9;color:#0f172a;font:14px/1.55 system-ui,-apple-system,Segoe UI,sans-serif}
    .wrap{max-width:1200px;margin:0 auto;padding:26px 20px 60px}
    h1{margin:0 0 4px;font-size:23px}.sub{color:#64748b;margin-bottom:18px;font-size:13px}
    h2{font-size:18px;border-bottom:2px solid #e2e8f0;padding-bottom:6px;margin:26px 0 12px}
    h3{font-size:14px;color:#334155;margin:18px 0 10px}
    .count{background:#e2e8f0;color:#475569;border-radius:999px;padding:1px 9px;font-size:12px;margin-left:6px}
    .fams{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:12px;margin-bottom:8px}
    .fam{background:#0f172a;color:#e2e8f0;border-radius:12px;padding:12px 14px}
    .famname{font-weight:700;font-size:14px;margin-bottom:6px}.price{font-size:12px;color:#fbbf24}
    .fam ul{margin:0 0 8px;padding-left:16px;font-size:11.5px;color:#cbd5e1}.fam li{margin:1px 0}
    .fit{font-size:11px;color:#94a3b8;border-top:1px solid #1e293b;padding-top:6px}
    .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:10px}
    .card{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:10px 12px}
    .name{font-weight:700;font-size:13.5px}.name a{color:#0369a1;text-decoration:none}
    .loc{color:#64748b;font-size:11.5px;margin:2px 0}
    .contact{font-size:12.5px}.contact a{color:#0369a1;text-decoration:none;margin-right:6px}
    .enrich{color:#b45309;font-size:11.5px}
    """
    doc = ("<!doctype html><html lang=en><head><meta charset=utf-8>"
           "<meta name=viewport content='width=device-width,initial-scale=1'>"
           "<title>go4it - UAE buyers (Decora home-decor)</title><style>" + css + "</style></head><body><div class=wrap>"
           "<h1>UAE buyers - Decora home-decor range</h1>"
           f"<div class=sub>{esc(store.get('name'))} ({esc(store.get('material'))}, ${esc(store.get('price_band_usd'))}) "
           f"&middot; {len(buyers)} UAE buyers &middot; {data.get('with_phone', 0)} with phone &middot; click any contact</div>"
           "<h2>What we offer</h2><div class=fams>" + "".join(fam_cards) + "</div>"
           "<h2>UAE buyers</h2>" + "".join(sections) + "</div></body></html>")

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "uae_decor_buyers.html"), "w", encoding="utf-8") as f:
        f.write(doc)

    with open(os.path.join(OUT_DIR, "uae_decor_buyers.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Category", "Company", "City", "Phone", "Website", "Decora fit"])
        for b in buyers:
            w.writerow([" / ".join(b.get("categories", [])), b.get("company", ""),
                        b.get("city", "") or b.get("location", ""),
                        (b.get("phones") or [""])[0], as_url(b.get("website")),
                        " / ".join(b.get("fits", []))])

    print(f"wrote {os.path.join(OUT_DIR, 'uae_decor_buyers.html')}")
    print(f"wrote {os.path.join(OUT_DIR, 'uae_decor_buyers.csv')} ({len(buyers)} buyers)")


if __name__ == "__main__":
    run()
