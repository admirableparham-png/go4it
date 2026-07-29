"""Export the new-products market research to CSV + a clickable HTML file.

    ./.venv/bin/python scripts/export_newproducts.py

Reads   docs/research/new_products_2026-07.json   (the researched dataset)
Writes  docs/prospects/new_products.csv           (clean, shareable)
        docs/prospects/new_products.html          (grouped, clickable, with opportunity cards)

Covers ONLY the three new products (cold asphalt, rubber tiles, pour-in-place rubber),
each split into Georgia buyers / Iran sellers / UAE sellers, plus a price-opportunity
summary per product. Provenance ("source") is intentionally NOT shown (founder preference);
company websites, emails, phones and LinkedIn are clickable.
"""
import csv
import html
import json
import os
import re

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(BASE, "docs", "research", "new_products_2026-07.json")
OUT_DIR = os.path.join(BASE, "docs", "prospects")

# short display names + emoji per product key
PRODUCT_DISPLAY = {
    "cold-asphalt": ("Cold asphalt (bagged cold-mix)", "\U0001F6E3️"),
    "rubber-tiles": ("Rubber tiles (gym / outdoor)", "\U0001F7E9"),
    "pour-in-place-rubber": ("Pour-in-place rubber flooring", "\U0001F3C3"),
}
ROLE_ORDER = ["GE-buyer", "IR-seller", "UAE-seller"]
ROLE_TITLE = {
    "GE-buyer": "\U0001F1EC\U0001F1EA Georgia buyers (demand)",
    "IR-seller": "\U0001F1EE\U0001F1F7 Iran sellers (supply)",
    "UAE-seller": "\U0001F1E6\U0001F1EA UAE sellers (supply)",
}


def as_url(u):
    u = (u or "").strip()
    if not u:
        return ""
    return u if "://" in u else "https://" + u


def linkify(text):
    t = html.escape(text or "").replace("\n", "<br>")
    t = re.sub(r'([\w.+-]+@[\w-]+\.[\w.-]+)', r'<a href="mailto:\1">\1</a>', t)
    t = re.sub(r'(https?://[^\s<]+)', r'<a href="\1" target="_blank" rel="noopener">\1</a>', t)
    t = re.sub(r'(?<![/\w])(linkedin\.com/[^\s<]+)', r'<a href="https://\1" target="_blank" rel="noopener">\1</a>', t)
    return t


def card(rec):
    web = as_url(rec.get("website"))
    name = html.escape(rec.get("company") or "(unknown company)")
    name_html = f'<a href="{html.escape(web)}" target="_blank" rel="noopener">{name}</a>' if web else name
    conf = (rec.get("confidence") or "").lower()
    badge = f'<span class="conf c-{html.escape(conf)}">{html.escape(conf)}</span>' if conf else ""
    out = [f'<div class="name">{name_html} {badge}</div>']

    loc = " · ".join(x for x in [html.escape(rec.get("city") or ""), html.escape(rec.get("country") or "")] if x)
    if loc:
        out.append(f'<div class="loc">{loc}</div>')

    contact = []
    if rec.get("contact_name"):
        contact.append(f'<b>{html.escape(rec["contact_name"])}</b>')
    if rec.get("email"):
        contact.append(f'<a href="mailto:{html.escape(rec["email"])}">{html.escape(rec["email"])}</a>')
    if rec.get("phone"):
        tel = re.sub(r"[^\d+]", "", rec["phone"])
        contact.append(f'<a href="tel:{tel}">{html.escape(rec["phone"])}</a>')
    if contact:
        out.append('<div class="contact">' + " · ".join(contact) + "</div>")

    if rec.get("product_spec"):
        out.append(f'<div class="spec">{html.escape(rec["product_spec"])}</div>')
    if rec.get("indicative_price"):
        out.append(f'<div class="price">\U0001F4B0 {html.escape(rec["indicative_price"])}</div>')
    if web:
        out.append('<div class="links"><a class="lnk" href="{}" target="_blank" rel="noopener">\U0001F310 website</a></div>'.format(html.escape(web)))
    if rec.get("notes"):
        out.append(f'<div class="note">{linkify(rec["notes"])}</div>')
    return '<div class="card">' + "".join(out) + "</div>"


def opp_card(o):
    rows = [
        ("HS code", o.get("hs_code")),
        ("Priced per", o.get("unit")),
        ("Iran EXW", o.get("iran_exw")),
        ("UAE EXW", o.get("uae_exw")),
        ("Georgia duty", o.get("georgia_duty_pct")),
        ("Georgia VAT", o.get("georgia_vat_pct")),
        ("Delivered to Georgia (est.)", o.get("delivered_georgia_estimate")),
        ("Cheapest source", o.get("cheapest_source")),
    ]
    bench = (o.get("benchmark_product") or "")
    if o.get("benchmark_price"):
        bench = (bench + " — " + o["benchmark_price"]).strip(" — ")
    if bench:
        rows.append(("Benchmark", bench))
    body = "".join(
        f'<div class="orow"><span class="ok">{html.escape(k)}</span><span class="ov">{html.escape(v or "—")}</span></div>'
        for k, v in rows
    )
    verdict = html.escape(o.get("verdict") or "")
    rationale = html.escape(o.get("rationale") or "")
    return (
        '<div class="opp"><div class="verdict">\U0001F4CA Opportunity: ' + verdict + "</div>"
        + f'<div class="orows">{body}</div>'
        + (f'<div class="rationale">{rationale}</div>' if rationale else "")
        + "</div>"
    )


def run():
    with open(SRC, encoding="utf-8") as f:
        data = json.load(f)
    records = data.get("records", [])
    opps = {o.get("product_key"): o for o in data.get("opportunities", [])}
    keys = [p["key"] for p in data.get("products", [])] or list(PRODUCT_DISPLAY)

    # ---- HTML ----
    sections = []
    for pkey in keys:
        disp, emoji = PRODUCT_DISPLAY.get(pkey, (pkey, ""))
        prod_recs = [r for r in records if r.get("product_key") == pkey]
        blocks = [f'<h2>{emoji} {html.escape(disp)} <span class="count">{len(prod_recs)}</span></h2>']
        if pkey in opps:
            blocks.append(opp_card(opps[pkey]))
        for role in ROLE_ORDER:
            items = [r for r in prod_recs if r.get("role") == role]
            if not items:
                continue
            cards = "".join(card(r) for r in items)
            blocks.append(f'<h3>{ROLE_TITLE.get(role, role)} <span class="count">{len(items)}</span></h3>'
                          f'<div class="grid">{cards}</div>')
        sections.append('<section>' + "".join(blocks) + "</section>")

    css = """
    :root{color-scheme:light}
    *{box-sizing:border-box}
    body{margin:0;background:#f1f5f9;color:#0f172a;font:14px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
    .wrap{max-width:1200px;margin:0 auto;padding:28px 20px 60px}
    h1{margin:0 0 4px;font-size:24px}
    .sub{color:#64748b;margin-bottom:24px}
    h2{margin:36px 0 6px;font-size:20px;border-bottom:2px solid #e2e8f0;padding-bottom:6px}
    h3{margin:20px 0 10px;font-size:15px;color:#334155}
    .count{background:#e2e8f0;color:#475569;border-radius:999px;padding:1px 9px;font-size:13px;margin-left:6px}
    .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px}
    .card{background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:14px 16px;box-shadow:0 1px 2px rgba(0,0,0,.04)}
    .name{font-weight:700;font-size:15px;margin-bottom:4px}
    .name a{color:#0369a1;text-decoration:none}
    .name a:hover{text-decoration:underline}
    .conf{font-size:10px;font-weight:700;text-transform:uppercase;border-radius:5px;padding:1px 6px;vertical-align:middle}
    .c-verified{background:#dcfce7;color:#15803d}.c-likely{background:#fef9c3;color:#a16207}.c-uncertain{background:#f1f5f9;color:#64748b}
    .loc{color:#64748b;font-size:12px;margin-bottom:6px}
    .contact{font-size:13px;margin-bottom:6px}
    .contact a{color:#0369a1;text-decoration:none}
    .spec{font-size:12px;color:#334155;margin-bottom:6px}
    .price{font-size:12px;color:#059669;font-weight:600;margin-bottom:6px}
    .links{margin-bottom:4px}
    .lnk{font-size:12px;color:#0369a1;text-decoration:none;margin-right:10px}
    .lnk:hover{text-decoration:underline}
    .note{background:#f8fafc;border-left:3px solid #cbd5e1;border-radius:4px;padding:7px 9px;margin-top:6px;font-size:12px;color:#334155}
    .note a{color:#0369a1}
    .opp{background:#0f172a;color:#e2e8f0;border-radius:12px;padding:16px 18px;margin:10px 0 4px}
    .verdict{font-weight:700;font-size:15px;margin-bottom:10px;color:#fbbf24}
    .orows{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:4px 18px;margin-bottom:10px}
    .orow{display:flex;justify-content:space-between;gap:10px;border-bottom:1px solid #1e293b;padding:3px 0;font-size:12px}
    .ok{color:#94a3b8}.ov{color:#e2e8f0;font-weight:600;text-align:right}
    .rationale{font-size:12.5px;color:#cbd5e1;line-height:1.55}
    """
    total = len(records)
    out = (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>go4it — New products: buyers, sellers & opportunity</title><style>" + css + "</style></head><body><div class=wrap>"
        "<h1>go4it — New product research</h1>"
        f"<div class=sub>{total} companies across 3 products · Georgia buyers + Iran/UAE sellers + price opportunity · click any company, website, email or phone</div>"
        + "".join(sections) +
        "</div></body></html>"
    )

    os.makedirs(OUT_DIR, exist_ok=True)
    html_dest = os.path.join(OUT_DIR, "new_products.html")
    with open(html_dest, "w", encoding="utf-8") as f:
        f.write(out)

    # ---- CSV (no source/provenance column) ----
    csv_dest = os.path.join(OUT_DIR, "new_products.csv")
    with open(csv_dest, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Product", "Role", "Company", "Country", "City", "Contact Person",
                    "Email", "Phone", "Website", "HS Code", "Indicative Price", "Notes"])
        for pkey in keys:
            disp = PRODUCT_DISPLAY.get(pkey, (pkey, ""))[0]
            for role in ROLE_ORDER:
                for r in [x for x in records if x.get("product_key") == pkey and x.get("role") == role]:
                    w.writerow([disp, r.get("role_label", role), r.get("company", ""), r.get("country", ""),
                                r.get("city", ""), r.get("contact_name", ""), r.get("email", ""),
                                r.get("phone", ""), as_url(r.get("website")), r.get("hs_code", ""),
                                r.get("indicative_price", ""), r.get("notes", "")])

    print(f"exported {total} companies -> {html_dest}")
    print(f"exported {total} rows        -> {csv_dest}")


if __name__ == "__main__":
    run()
