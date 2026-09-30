"""Render the TRSHARKS anchor buyer hunt into a polished ENGLISH Buyer Research Report.

    ./.venv/bin/python scripts/gen_anchors_buyers_report.py

Reads   docs/research/anchors_buyers_by_country.json
          { product, prepared_for, request_code, products:[{code,name,desc}],
            strategy:[{country,iso2,tier,verdict,why}],
            cells:[{country, iso2, market_verdict, market_why, note,
                    active:[{buyer,city,wants,posted,website,contact,source_url,role,product,verdict}],
                    potential:[{company,city,business,why_fit,rating,website,contact,bulk,product,source_url,verdict}]}],
            totals:{active,potential,confirmed} }
Writes  docs/prospects/trsharks_anchors_buyers.html         (ADMIN — full: sources + verdicts)
        docs/prospects/trsharks_anchors_buyers_client.html  (CLIENT — clean, no sources)
        docs/prospects/trsharks_anchors_buyers_ADMIN.csv     (admin CSV, keeps SOURCE_url)
        docs/prospects/buyers_trsharks.json                  (normalized for scripts/load_managed_buyers.py)

Rules honoured: English chrome · 2 parts per market (Part 1 recent RFQ posters / Part 2 bulk buyers) ·
BULK buyers highlighted in a different colour · website ALWAYS shown · SOURCE admin-only (client=clean) ·
per-buyer P1/P2 product tags. Buyer names + their stated requests stay verbatim for accurate outreach.
"""
import base64
import csv
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RES = os.path.join(ROOT, "docs", "research")
OUT = os.path.join(ROOT, "docs", "prospects")
LOGO = os.path.join(ROOT, "app", "static", "logo-light.png")
os.makedirs(OUT, exist_ok=True)

VERDICT = {"confirmed": ("Confirmed", "#0f766e", "#e6f6f2"),
           "plausible": ("Plausible", "#b45309", "#fdf3e3"),
           "unverified": ("Unverified", "#64748b", "#eef1f5")}
PRODTAG = {"P1": ("P1", "#1d4ed8", "#e6edff"), "P2": ("P2", "#b45309", "#fdf3e3"),
           "P1+P2": ("P1+P2", "#0f766e", "#e6f6f2")}
DEFAULT_PRODUCTS = [
    {"code": "P1", "name": "Self-Drilling Winged Drywall Anchor (Jilet-Tip, 28 & 40 mm)",
     "desc": "No-drill carbon-steel anchor: hammer the spike tip straight in and the wings expand behind the "
             "panel as the screw drives. Light-to-medium loads on drywall, plasterboard, hollow brick/block and "
             "hollow composite/wood panels. Fast and tool-light — curtain rails, shelves, hooks, TV brackets, "
             "mirrors."},
    {"code": "P2", "name": "Spring Toggle / Umbrella Bolt (4x80)",
     "desc": "Drill a hole, fold the spring wings, push through; the wings snap open behind the wall/ceiling and "
             "the machine bolt clamps the load across a wide area. Medium-to-heavy loads including CEILINGS — "
             "chandeliers, hanging fixtures, wall cabinets, cornices, heavy mirrors. For drywall, suspended "
             "ceilings, hollow brick/block, wood/composite panels."},
]


def esc(s):
    return (str(s or "")).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def logo_data_uri():
    try:
        with open(LOGO, "rb") as f:
            return "data:image/png;base64," + base64.b64encode(f.read()).decode()
    except Exception:  # noqa: BLE001
        return ""


def a_ext(url, label, color="#0f766e"):
    if not url:
        return ""
    href = url if "://" in str(url) else "https://" + str(url)
    return (f'<a href="{esc(href)}" target="_blank" rel="noopener" '
            f'style="color:{color};text-decoration:none;word-break:break-word;">{esc(label)}&#8599;</a>')


def prod_chip(code):
    label, fg, bg = PRODTAG.get((code or "").strip(), PRODTAG["P1+P2"])
    return (f'<span style="font-size:9.5px;font-weight:800;color:{fg};background:{bg};border-radius:999px;'
            f'padding:1px 6px;white-space:nowrap;">{label}</span>')


def verdict_chip(v, clean):
    if clean:
        return ""
    label, fg, bg = VERDICT.get((v or "plausible").strip(), VERDICT["plausible"])
    return (f'<span style="font-size:9.5px;font-weight:700;color:{fg};background:{bg};border-radius:999px;'
            f'padding:1px 7px;white-space:nowrap;">{label}</span>')


def stars(r):
    r = max(1, min(5, int(round(float(r or 3)))))
    return f'<span style="color:#b45309;letter-spacing:1px;white-space:nowrap;">{"&#9733;" * r}{"&#9734;" * (5 - r)}</span>'


def web_contact(rec, clean):
    bits = []
    if rec.get("website"):
        dom = str(rec["website"]).replace("https://", "").replace("http://", "").strip("/")
        bits.append(a_ext(rec["website"], dom[:42]))
    if rec.get("contact"):
        bits.append(f'<span style="color:#475569;">{esc(rec["contact"])[:48]}</span>')
    if not clean and rec.get("source_url"):
        bits.append(a_ext(rec["source_url"], "source", "#94a3b8"))
    return "<br>".join(b for b in bits if b) or '<span style="color:#94a3b8;">&mdash;</span>'


def active_table(active, clean):
    if not active:
        return ('<div style="font-size:12.5px;color:#94a3b8;padding:8px 2px;">No public 2024-2026 purchase '
                'request registered for this market yet &mdash; the scored importers/distributors below are the '
                'practical route in.</div>')
    rows = []
    for a in active:
        meta = []
        if a.get("posted"):
            meta.append(f'&#128197; {esc(a["posted"])}')
        if a.get("role"):
            meta.append(f'&#128100; {esc(a["role"])[:36]}')
        metah = f'<div style="font-size:11px;color:#94a3b8;margin-top:3px;">{" &middot; ".join(meta)}</div>' if meta else ""
        rows.append(f'''<tr>
      <td><b style="color:#0f172a;">{esc(a.get("buyer",""))}</b> {prod_chip(a.get("product"))}{metah}</td>
      <td style="color:#475569;">{esc(a.get("city",""))}</td>
      <td style="color:#334155;">{esc(a.get("wants",""))}</td>
      <td>{web_contact(a, clean)}</td>
      <td style="text-align:center;">{verdict_chip(a.get("verdict"), clean) or '&mdash;'}</td>
    </tr>''')
    head = ('<th>Company</th><th>City</th><th>Requested</th><th>Website &amp; contact</th>'
            '<th style="text-align:center;">Conf.</th>')
    return (f'<table class="t"><thead><tr>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table>')


def potential_table(pot, clean):
    if not pot:
        return '<div style="font-size:12.5px;color:#94a3b8;padding:8px 2px;">&mdash;</div>'
    pot = sorted(pot, key=lambda p: (-(1 if p.get("bulk") else 0), -(p.get("rating") or 0)))
    rows = []
    for p in pot:
        cls = ' class="hl"' if p.get("bulk") else ""
        bulk = ('<span style="font-size:9px;font-weight:800;color:#065f46;background:#d1fae5;'
                'border:1px solid #6ee7b7;border-radius:999px;padding:1px 6px;margin-left:4px;">BULK</span>'
                if p.get("bulk") else "")
        rows.append(f'''<tr{cls}>
      <td><b style="color:#0f172a;">{esc(p.get("company",""))}</b> {prod_chip(p.get("product"))}{bulk}</td>
      <td style="color:#475569;">{esc(p.get("city",""))}</td>
      <td style="color:#334155;">{esc(p.get("business",""))}{(" &mdash; " + esc(p.get("why_fit",""))) if p.get("why_fit") else ""}</td>
      <td>{web_contact(p, clean)}</td>
      <td style="text-align:center;white-space:nowrap;">{stars(p.get("rating"))}{("<br>" + verdict_chip(p.get("verdict"), clean)) if not clean else ""}</td>
    </tr>''')
    head = ('<th>Company</th><th>City</th><th>Business &amp; why they fit</th>'
            '<th>Website &amp; contact</th><th style="text-align:center;">Rating</th>')
    return (f'<table class="t"><thead><tr>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table>')


TIER_LABELS = {1: "Tier 1 — National distributors / chains / major importers",
               2: "Tier 2 — Regional distributors & wholesalers",
               3: "Tier 3 — Specialist / smaller / plausible"}


def _tier_of(p):
    t = p.get("tier")
    if t in (1, 2, 3):
        return int(t)
    r = float(p.get("rating") or 3)      # fall back to rating if tier missing
    return 1 if r >= 5 else (2 if r >= 4 else 3)


def potential_tiered(pot, clean):
    """Part 2, banded into Tier 1/2/3 so the list is explicitly tier-separated."""
    if not pot:
        return '<div style="font-size:12.5px;color:#94a3b8;padding:8px 2px;">&mdash;</div>'
    bands = {1: [], 2: [], 3: []}
    for p in pot:
        bands[_tier_of(p)].append(p)
    out = []
    for t in (1, 2, 3):
        rows = bands[t]
        if not rows:
            continue
        out.append(f'<div style="font-size:11px;font-weight:800;color:#0f766e;margin:12px 0 4px;'
                   f'text-transform:uppercase;letter-spacing:.04em;">{TIER_LABELS[t]} '
                   f'<span style="color:#94a3b8;">({len(rows)})</span></div>')
        out.append(potential_table(rows, clean))
    return "".join(out)


def country_section(cell, clean):
    active, pot = cell.get("active", []), cell.get("potential", [])
    conf = sum(1 for a in active if a.get("verdict") == "confirmed")
    conf_txt = "" if clean else f' &middot; {conf} confirmed'
    verd = "" if clean else (f'<div style="font-size:12.5px;color:#475569;margin:2px 0 12px;">'
                             f'<b style="color:#0f766e;">Verdict:</b> {esc(cell.get("market_verdict",""))} '
                             f'&mdash; {esc(cell.get("market_why",""))}</div>')
    return f'''
  <section style="margin-top:34px;page-break-inside:auto;">
    <h2 style="margin:0 0 2px;font-size:20px;font-weight:800;color:#0f172a;border-bottom:2px solid #cfeae4;padding-bottom:6px;">
      {esc(cell.get("country",""))} <span style="font-size:12px;font-weight:600;color:#94a3b8;">({len(active)} active-request{conf_txt} &middot; {len(pot)} importers/distributors)</span></h2>
    {verd}
    <div class="band b1">&#128226; Part 1 &mdash; Active buy-requests (posted 2024-2026)</div>
    {active_table(active, clean)}
    <div class="band b2">&#11088; Part 2 &mdash; Importers / distributors / wholesalers (tier-banded)</div>
    {potential_tiered(pot, clean)}
  </section>'''


def exec_summary(strategy, cells):
    by_iso = {c.get("iso2"): c for c in cells}
    rows = []
    order = {"primary": 0, "eu": 1, "extra": 2}
    strat = sorted(strategy, key=lambda s: order.get(s.get("tier", "eu"), 3))
    for s in strat:
        c = by_iso.get(s.get("iso2"))
        cnt = (len(c.get("active", [])) + len(c.get("potential", []))) if c else 0
        tierpill = ""
        if s.get("tier") == "primary":
            tierpill = '<span style="font-size:9px;font-weight:800;color:#0f766e;background:#e6f6f2;border-radius:999px;padding:1px 7px;margin-left:6px;">PRIMARY</span>'
        elif s.get("tier") == "extra":
            tierpill = '<span style="font-size:9px;font-weight:800;color:#b45309;background:#fdf3e3;border-radius:999px;padding:1px 7px;margin-left:6px;">SUGGESTED</span>'
        rows.append(f'''<tr>
      <td><b style="color:#0f172a;">{esc(s.get("country",""))}</b>{tierpill}</td>
      <td style="color:#0f766e;font-weight:600;">{esc(s.get("verdict",""))}</td>
      <td style="color:#334155;">{esc(s.get("why",""))}</td>
      <td style="text-align:center;color:#475569;">{cnt or "&mdash;"}</td>
    </tr>''')
    return (f'<table class="t"><thead><tr><th>Market</th><th>Verdict</th><th>Why</th>'
            f'<th style="text-align:center;">Buyers</th></tr></thead><tbody>{"".join(rows)}</tbody></table>')


def products_strip(products):
    cards = []
    for p in products:
        label, fg, bg = PRODTAG.get(p.get("code", "P1"), PRODTAG["P1+P2"])
        cards.append(f'''<div style="flex:1;min-width:240px;border:1px solid #e2e8f0;border-left:4px solid {fg};
        border-radius:10px;padding:12px 14px;background:#fff;">
        <div style="font-size:10px;font-weight:800;color:{fg};background:{bg};display:inline-block;border-radius:999px;padding:1px 8px;">{label}</div>
        <div style="font-size:14px;font-weight:800;color:#0f172a;margin-top:6px;">{esc(p.get("name",""))}</div>
        <div style="font-size:12px;color:#475569;margin-top:4px;line-height:1.55;">{esc(p.get("desc",""))}</div>
      </div>''')
    return f'<div style="display:flex;flex-wrap:wrap;gap:12px;margin-top:14px;">{"".join(cards)}</div>'


def build(data, clean):
    cells = data.get("cells", [])
    strategy = data.get("strategy", [])
    products = data.get("products") or DEFAULT_PRODUCTS
    totals = data.get("totals", {})
    prepared = data.get("prepared_for", "TRSHARKS")
    code = data.get("request_code", "")
    n_markets = len(cells)
    n_buyers = sum(len(c.get("active", [])) + len(c.get("potential", [])) for c in cells)
    subtitle = ("Real demand-side buyers for TRSHARKS's metal cavity/hollow-wall anchors "
                "&mdash; Canada (primary), Europe, and recommended additional markets.")
    tline = (f'{n_buyers} buyers across {n_markets} markets '
             f'&middot; Part 1 = recent buy-requests, Part 2 = scored importers/distributors'
             + ("" if clean else f' &middot; {totals.get("confirmed",0)} confirmed &middot; research-grade, validate before contact"'))
    body = "".join(country_section(c, clean) for c in cells)
    logo = logo_data_uri()
    logo_html = f'<img src="{logo}" alt="Go4it" style="height:40px;width:auto;">' if logo else '<b style="font-size:22px;">Go4it</b>'
    copytag = "" if not clean else '<span style="font-size:10px;color:#94a3b8;"> &middot; client copy</span>'
    return f'''<div class="doc">
  <div class="head">
    <div style="display:flex;align-items:center;gap:12px;">{logo_html}
      <div><div style="font-size:20px;font-weight:800;color:#0f172a;line-height:1;">Go4it</div>
        <div style="font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:#0f766e;font-weight:700;margin-top:3px;">Buyer Research Report{copytag}</div></div>
    </div>
    <div style="text-align:right;font-size:11px;color:#64748b;">
      {f'<div class="mono">{esc(code)}</div>' if code else ''}
      <div>Prepared for: <b style="color:#0f172a;">{esc(prepared)}</b></div>
    </div>
  </div>
  <h1 style="margin:18px 0 4px;font-size:26px;font-weight:800;color:#0f172a;">Metal Drywall / Cavity Anchors &mdash; Target Buyers</h1>
  <p style="margin:0;font-size:13px;color:#475569;">{subtitle}</p>
  <p style="margin:6px 0 0;font-size:11.5px;color:#94a3b8;">{tline}</p>
  {products_strip(products)}
  <h2 style="margin:26px 0 8px;font-size:16px;font-weight:800;color:#0f172a;">Executive summary</h2>
  {exec_summary(strategy, cells)}
  {body}
  <div class="foot">Go4it &middot; sourcing &amp; delivered trade &nbsp;|&nbsp; buyer names + requests kept verbatim for outreach accuracy</div>
</div>'''


CSS = '''<style>
 *{box-sizing:border-box}
 body{margin:0;background:#eef2f4;}
 .doc{max-width:1120px;margin:0 auto;padding:34px 30px 60px;background:#fff;color:#0f172a;
   font-family:'Inter','Segoe UI',system-ui,-apple-system,Roboto,Arial,sans-serif;line-height:1.6;
   box-shadow:0 20px 60px -30px rgba(2,20,40,.35);border-radius:14px;}
 .head{display:flex;justify-content:space-between;align-items:flex-start;border-bottom:2px solid #cfeae4;padding-bottom:14px;}
 .mono{font-family:'JetBrains Mono',ui-monospace,monospace;font-size:11px;color:#0f766e;font-weight:600;}
 .band{font-size:12px;font-weight:800;margin:16px 0 8px;padding:5px 10px;border-radius:7px;display:inline-block;}
 .band.b1{color:#065f46;background:#e6f6f2;}
 .band.b2{color:#7c2d12;background:#fdf3e3;}
 table.t{width:100%;border-collapse:collapse;font-size:12.5px;margin-top:2px;}
 table.t thead th{text-align:left;font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:#64748b;
   font-weight:700;border-bottom:1.5px solid #e2e8f0;padding:6px 9px;background:#f8fafc;}
 table.t td{padding:8px 9px;vertical-align:top;border-bottom:1px solid #eef2f6;}
 table.t tbody tr:hover{background:#f8fafc;}
 tr.hl td{background:#effcf8;}
 tr.hl td:first-child{box-shadow:inset 3px 0 0 #0f766e;-webkit-print-color-adjust:exact;print-color-adjust:exact;}
 .foot{margin-top:30px;padding-top:14px;border-top:1px solid #e2e8f0;font-size:10.5px;color:#94a3b8;text-align:center;}
 a{word-break:break-word;}
 @media print{ body{background:#fff;} .doc{box-shadow:none;border-radius:0;max-width:100%;} table.t td,table.t th{padding:5px 7px;} tr.hl td{background:#effcf8 !important;} }
 @media(max-width:720px){ .doc{padding:20px 14px;} table.t{font-size:11.5px;} }
</style>'''


def html_doc(inner, title):
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)}</title>'
            f'{CSS}</head><body>{inner}</body></html>')


def write_csv(data, path):
    cols = ["part", "country", "iso2", "company", "city", "segment", "product", "bulk", "rating",
            "verdict", "website", "contact", "wants", "posted", "why_fit", "SOURCE_url"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for c in data.get("cells", []):
            for a in c.get("active", []):
                w.writerow({"part": "1-active", "country": c.get("country"), "iso2": c.get("iso2"),
                            "company": a.get("buyer"), "city": a.get("city"), "segment": a.get("role"),
                            "product": a.get("product"), "bulk": "", "rating": "", "verdict": a.get("verdict"),
                            "website": a.get("website"), "contact": a.get("contact"), "wants": a.get("wants"),
                            "posted": a.get("posted"), "why_fit": "", "SOURCE_url": a.get("source_url")})
            for p in c.get("potential", []):
                w.writerow({"part": "2-importer", "country": c.get("country"), "iso2": c.get("iso2"),
                            "company": p.get("company"), "city": p.get("city"), "segment": p.get("business"),
                            "product": p.get("product"), "bulk": "yes" if p.get("bulk") else "",
                            "rating": p.get("rating"), "verdict": p.get("verdict"), "website": p.get("website"),
                            "contact": p.get("contact"), "wants": "", "posted": "", "why_fit": p.get("why_fit"),
                            "SOURCE_url": p.get("source_url")})


def write_loader_json(data, path):
    """Normalized to scripts/load_managed_buyers.py's expected keys (dest_iso/buys/phones/source_url)."""
    out = []
    for c in data.get("cells", []):
        iso = c.get("iso2", "")
        for a in c.get("active", []):
            out.append({"company": a.get("buyer"), "dest_iso": iso, "city": a.get("city", ""),
                        "website": a.get("website", ""), "email": a.get("contact", ""), "phones": [],
                        "buys": [a.get("wants", "")] if a.get("wants") else ["drywall/cavity anchors"],
                        "source_url": a.get("source_url", ""), "match_score": 90,
                        "tier": "active", "product": a.get("product", "")})
        for p in c.get("potential", []):
            out.append({"company": p.get("company"), "dest_iso": iso, "city": p.get("city", ""),
                        "website": p.get("website", ""), "email": p.get("contact", ""), "phones": [],
                        "buys": [p.get("business", "")] if p.get("business") else ["drywall/cavity anchors"],
                        "source_url": p.get("source_url", ""),
                        "match_score": int(55 + 7 * (p.get("rating") or 3)),
                        "tier": "potential", "product": p.get("product", "")})
    json.dump({"buyers": out}, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return len(out)


def main():
    src = os.path.join(RES, "anchors_buyers_by_country.json")
    data = json.load(open(src, encoding="utf-8"))

    admin = html_doc(build(data, clean=False), "TRSHARKS - Buyer Research Report (admin)")
    open(os.path.join(OUT, "trsharks_anchors_buyers.html"), "w", encoding="utf-8").write(admin)

    client = html_doc(build(data, clean=True), "Go4it - Buyer Research Report")
    open(os.path.join(OUT, "trsharks_anchors_buyers_client.html"), "w", encoding="utf-8").write(client)

    write_csv(data, os.path.join(OUT, "trsharks_anchors_buyers_ADMIN.csv"))
    n = write_loader_json(data, os.path.join(OUT, "buyers_trsharks.json"))

    tb = sum(len(c.get("active", [])) + len(c.get("potential", [])) for c in data.get("cells", []))
    print(f"markets: {len(data.get('cells', []))} | buyers: {tb} | loader rows: {n}")
    print("wrote docs/prospects/: trsharks_anchors_buyers.html (admin), _client.html, _ADMIN.csv, buyers_trsharks.json")


if __name__ == "__main__":
    main()
