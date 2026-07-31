"""Export the Georgia chemical-buyer research to a source-less HTML + CSV.

    ./.venv/bin/python scripts/export_chem_buyers.py

Reads the three harvest datasets and writes:
  docs/prospects/georgia_chem_buyers.csv    (flat, shareable, NO source column)
  docs/prospects/georgia_chem_buyers.html   (two sections + customs, clickable contacts)

Section A = potential Georgian buyers (mines, metallurgy, water utilities, importers);
Section B = live procurement buy-requests (2025+); + customs import records if loaded.
Provenance is intentionally omitted (founder preference); contacts are clickable.
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


def contact_html(email, phone, website):
    bits = []
    if phone:
        bits.append(f'<a href="tel:{esc(re.sub(chr(32),"",phone))}">{esc(phone)}</a>')
    if email:
        bits.append(f'<a href="mailto:{esc(email)}">{esc(email)}</a>')
    if website:
        u = as_url(website)
        bits.append(f'<a href="{esc(u)}" target="_blank" rel="noopener">website</a>')
    return " · ".join(bits) or '<span class="enrich">needs enrichment</span>'


def run():
    buyers = load("ge_chem_buyers.json").get("buyers", [])
    tenders = load("ge_chem_rfqs.json").get("tenders", [])
    customs = load("ge_customs.json").get("records", [])

    anchors = [b for b in buyers if b.get("source_tier") == "anchor"]
    directory = [b for b in buyers if b.get("source_tier") != "anchor"]

    def products_html(b):
        chems = b.get("chemicals") or []
        if chems:
            return ('<div class="prodlabel">Buys / needs:</div><div class="chips">'
                    + "".join(f'<span class="chip">{esc(c)}</span>' for c in chems) + "</div>")
        if b.get("distributor"):
            return '<div class="chips"><span class="chip any">distributor · can source any of the 13</span></div>'
        return f'<div class="spec">{esc(b.get("reagents"))}</div>'

    def buyer_card(b):
        name = esc(b.get("company"))
        web = as_url(b.get("website"))
        name_html = f'<a href="{esc(web)}" target="_blank" rel="noopener">{name}</a>' if web else name
        loc = " · ".join(x for x in [esc(b.get("segment")), esc(b.get("city"))] if x)
        return (f'<div class="card"><div class="name">{name_html}</div>'
                f'<div class="loc">{loc}</div>'
                f'<div class="contact">{contact_html(b.get("email"), b.get("phone"), b.get("website"))}</div>'
                f'{products_html(b)}</div>')

    sections = []
    sections.append(
        f'<section><h2>\U0001F1EC\U0001F1EA Section A · Potential Georgian buyers '
        f'<span class="count">{len(buyers)}</span></h2>'
        f'<h3>Anchor accounts (mines · metallurgy · water utilities · importers) '
        f'<span class="count">{len(anchors)}</span></h3>'
        f'<div class="grid">{"".join(buyer_card(b) for b in anchors)}</div>'
        f'<h3>Chemical importers / distributors <span class="count">{len(directory)}</span></h3>'
        f'<div class="grid">{"".join(buyer_card(b) for b in directory)}</div></section>')

    if tenders:
        rows = "".join(
            f'<tr><td>{esc(t.get("chemical"))}</td><td>{esc(t.get("buyer"))}</td>'
            f'<td>{esc(t.get("announced"))}</td><td class="hot">{esc(t.get("deadline"))}</td>'
            f'<td>{"OPEN" if t.get("open") else "closed"}</td></tr>' for t in tenders)
        sections.append(
            f'<section><h2>\U0001F4E2 Section B · Live procurement buy-requests (2025+) '
            f'<span class="count">{len(tenders)}</span></h2>'
            f'<div class="scroll"><table><thead><tr><th>Chemical</th><th>Buyer (public body)</th>'
            f'<th>Announced</th><th>Bid deadline</th><th>Status</th></tr></thead>'
            f'<tbody>{rows}</tbody></table></div></section>')
    else:
        sections.append(
            '<section><h2>\U0001F4E2 Section B · Live procurement buy-requests (2025+) '
            '<span class="count">0</span></h2><p class="empty">No open chemical tenders on the '
            'Georgian procurement portal in the scanned window. Mining reagents are procured '
            'privately (see Section A); water-treatment tenders appear here periodically.</p></section>')

    if customs:
        rows = "".join(
            f'<tr><td>{esc(c.get("company"))}</td><td>{esc(c.get("chemical"))}</td>'
            f'<td>{esc(c.get("import_date"))}</td><td>{esc(c.get("quantity"))} {esc(c.get("unit"))}</td>'
            f'<td>{contact_html(c.get("email"), c.get("phone"), c.get("website"))}</td></tr>'
            for c in customs)
        sections.append(
            f'<section><h2>\U0001F4E6 Customs import records '
            f'<span class="count">{len(customs)}</span></h2>'
            f'<div class="scroll"><table><thead><tr><th>Importer</th><th>Chemical</th>'
            f'<th>Import date</th><th>Qty</th><th>Contact</th></tr></thead>'
            f'<tbody>{rows}</tbody></table></div></section>')

    css = """
    :root{color-scheme:light}*{box-sizing:border-box}
    body{margin:0;background:#f1f5f9;color:#0f172a;font:14px/1.55 system-ui,-apple-system,Segoe UI,sans-serif}
    .wrap{max-width:1200px;margin:0 auto;padding:26px 20px 60px}
    h1{margin:0 0 4px;font-size:23px}.sub{color:#64748b;margin-bottom:22px;font-size:13px}
    section{margin-bottom:26px}
    h2{font-size:18px;border-bottom:2px solid #e2e8f0;padding-bottom:6px;margin:20px 0 10px}
    h3{font-size:14px;color:#334155;margin:16px 0 8px}
    .count{background:#e2e8f0;color:#475569;border-radius:999px;padding:1px 9px;font-size:12px;margin-left:6px}
    .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px}
    .card{background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:12px 14px}
    .name{font-weight:700;font-size:14px}.name a{color:#0369a1;text-decoration:none}
    .loc{color:#64748b;font-size:12px;margin:2px 0 6px}
    .contact{font-size:12.5px;margin-bottom:6px}.contact a{color:#0369a1;text-decoration:none;margin-right:2px}
    .spec{font-size:11.5px;color:#475569;background:#f8fafc;border-radius:6px;padding:6px 8px}
    .prodlabel{font-size:10px;text-transform:uppercase;letter-spacing:.04em;color:#94a3b8;margin-bottom:4px}
    .chips{display:flex;flex-wrap:wrap;gap:4px}
    .chip{font-size:11px;background:#e0f2fe;color:#0369a1;border:1px solid #bae6fd;border-radius:6px;padding:1px 7px}
    .chip.any{background:#f1f5f9;color:#475569;border-color:#e2e8f0}
    .enrich{color:#b45309}
    table{width:100%;border-collapse:collapse;font-size:12.5px;background:#fff;border-radius:8px;overflow:hidden}
    th,td{text-align:left;padding:7px 9px;border-bottom:1px solid #e2e8f0}th{background:#e2e8f0}
    .hot{color:#b91c1c;font-weight:700}.scroll{overflow-x:auto}.empty{color:#64748b;font-size:13px}
    """
    doc = ("<!doctype html><html lang=en><head><meta charset=utf-8>"
           "<meta name=viewport content='width=device-width,initial-scale=1'>"
           "<title>go4it - Georgia chemical buyers</title><style>" + css + "</style></head><body><div class=wrap>"
           "<h1>Georgia - chemical buyers</h1>"
           f"<div class=sub>Mineral-processing &amp; water-treatment reagents · {len(buyers)} potential buyers · "
           f"{len(tenders)} live procurement RFQs · {len(customs)} customs records · click any contact</div>"
           + "".join(sections) + "</div></body></html>")

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "georgia_chem_buyers.html"), "w", encoding="utf-8") as f:
        f.write(doc)

    with open(os.path.join(OUT_DIR, "georgia_chem_buyers.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Section", "Chemical/Interest", "Company/Buyer", "City", "Segment",
                    "Email", "Phone", "Website", "Detail"])
        for b in buyers:
            w.writerow(["A potential buyer", b.get("reagents", ""), b.get("company", ""), b.get("city", ""),
                        b.get("segment", ""), b.get("email", ""), b.get("phone", ""),
                        as_url(b.get("website")), ""])
        for t in tenders:
            w.writerow(["B procurement RFQ", t.get("chemical", ""), t.get("buyer", ""), "GE", "State/municipal",
                        "", "", "", f"{t.get('number','')} deadline {t.get('deadline','')} "
                        f"{'OPEN' if t.get('open') else 'closed'}"])
        for c in customs:
            w.writerow(["Customs importer", c.get("chemical", ""), c.get("company", ""), "GE", "Importer",
                        c.get("email", ""), c.get("phone", ""), as_url(c.get("website")),
                        f"imported {c.get('import_date','')} qty {c.get('quantity','')}"])

    print(f"wrote {os.path.join(OUT_DIR,'georgia_chem_buyers.html')}")
    print(f"wrote {os.path.join(OUT_DIR,'georgia_chem_buyers.csv')} "
          f"({len(buyers)} buyers + {len(tenders)} RFQs + {len(customs)} customs)")


if __name__ == "__main__":
    run()
