"""Export all go4it leads to a self-contained, clickable HTML file.

    ./.venv/bin/python scripts/export_leads.py
    -> writes docs/prospects/go4it_leads.html  (open it in any browser)

Shows every field per lead — company, contact, email, phone, website, source,
the Clay-enriched procurement contacts (with LinkedIn), and the indicative quote.
All websites / emails / phones / LinkedIn are clickable.
"""
import html
import os
import re
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlmodel import Session, select  # noqa: E402

from app.db import engine  # noqa: E402
from app.models import Activity, Lead, Quote  # noqa: E402


def as_url(u):
    if not u:
        return ""
    return u if "://" in u else "https://" + u


def linkify(text):
    t = html.escape(text or "").replace("\n", "<br>")
    t = re.sub(r'([\w.+-]+@[\w-]+\.[\w.-]+)', r'<a href="mailto:\1">\1</a>', t)
    t = re.sub(r'(https?://[^\s<]+)', r'<a href="\1" target="_blank" rel="noopener">\1</a>', t)
    t = re.sub(r'(?<![/\w])(linkedin\.com/[^\s<]+)', r'<a href="https://\1" target="_blank" rel="noopener">\1</a>', t)
    return t


def card(lead, notes, quote):
    web = as_url(lead.website)
    name = html.escape(lead.buyer_company or "(unknown company)")
    name_html = f'<a href="{html.escape(web)}" target="_blank" rel="noopener">{name}</a>' if web else name
    out = [f'<div class="name">{name_html}</div>']

    badges = []
    if lead.product:
        badges.append(f'<span class="badge b-prod">{html.escape(lead.product)}</span>')
    if lead.dest_country:
        badges.append(f'<span class="badge b-dest">{html.escape(lead.dest_country)}</span>')
    if lead.tracking_code:
        badges.append(f'<span class="badge">{html.escape(lead.tracking_code)}</span>')
    out.append('<div class="badges">' + "".join(badges) + "</div>")

    contact = []
    if lead.contact_name:
        contact.append(f'<b>{html.escape(lead.contact_name)}</b>')
    if lead.email:
        contact.append(f'<a href="mailto:{html.escape(lead.email)}">{html.escape(lead.email)}</a>')
    if lead.phone:
        tel = re.sub(r'[^\d+]', '', lead.phone)
        contact.append(f'<a href="tel:{tel}">{html.escape(lead.phone)}</a>')
    if contact:
        out.append('<div class="contact">' + " &middot; ".join(contact) + "</div>")

    links = []
    if web:
        links.append(f'<a class="lnk" href="{html.escape(web)}" target="_blank" rel="noopener">&#127760; website</a>')
    if lead.source_url:
        links.append(f'<a class="lnk" href="{html.escape(as_url(lead.source_url))}" target="_blank" rel="noopener">&#128270; source</a>')
    if links:
        out.append('<div class="links">' + "".join(links) + "</div>")

    if notes:
        body = "".join(f'<div class="note">{linkify(n)}</div>' for n in notes)
        out.append(f'<div class="notes">{body}</div>')

    if quote:
        out.append('<div class="quote">indicative quote: EXW {:.2f} &rarr; delivered {:.2f} {} ({})</div>'.format(
            quote.exw_unit, quote.delivered_unit, html.escape(quote.quote_currency), html.escape(quote.incoterm)))

    return '<div class="card">' + "".join(out) + "</div>"


def run():
    with Session(engine) as s:
        leads = s.exec(select(Lead).order_by(Lead.id)).all()
        notes_by_lead = {}
        for a in s.exec(select(Activity)).all():
            if a.kind == "note" and a.body:
                notes_by_lead.setdefault(a.lead_id, []).append(a.body)
        quote_by_lead = {}
        for q in s.exec(select(Quote).order_by(Quote.id)).all():
            quote_by_lead.setdefault(q.lead_id, q)

    groups = [
        ("Ceramic &amp; porcelain tiles (HS 6907)", "research-tile"),
        ("Clay bricks &mdash; AJOR (HS 6904)", "research"),
        ("Other / demo / test", None),
    ]
    sections = []
    total = 0
    for title, src in groups:
        if src:
            items = [l for l in leads if l.source == src]
        else:
            items = [l for l in leads if l.source not in ("research", "research-tile")]
        if not items:
            continue
        total += len(items)
        cards = "".join(card(l, notes_by_lead.get(l.id, []), quote_by_lead.get(l.id)) for l in items)
        sections.append(f'<h2>{title} <span class="count">{len(items)}</span></h2><div class="grid">{cards}</div>')

    stamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    css = """
    :root{color-scheme:light}
    *{box-sizing:border-box}
    body{margin:0;background:#f1f5f9;color:#0f172a;font:14px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
    .wrap{max-width:1200px;margin:0 auto;padding:28px 20px 60px}
    h1{margin:0 0 4px;font-size:24px}
    .sub{color:#64748b;margin-bottom:24px}
    h2{margin:32px 0 12px;font-size:18px;border-bottom:2px solid #e2e8f0;padding-bottom:6px}
    .count{background:#e2e8f0;color:#475569;border-radius:999px;padding:1px 9px;font-size:13px;margin-left:6px}
    .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:14px}
    .card{background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:14px 16px;box-shadow:0 1px 2px rgba(0,0,0,.04)}
    .name{font-weight:700;font-size:15px;margin-bottom:6px}
    .name a{color:#0369a1;text-decoration:none}
    .name a:hover{text-decoration:underline}
    .badges{margin-bottom:8px;display:flex;flex-wrap:wrap;gap:5px}
    .badge{background:#f1f5f9;color:#475569;border-radius:6px;padding:2px 7px;font-size:11px}
    .b-prod{background:#ecfdf5;color:#047857}
    .b-dest{background:#eff6ff;color:#1d4ed8}
    .contact{font-size:13px;margin-bottom:6px}
    .contact a{color:#0369a1;text-decoration:none}
    .links{margin-bottom:6px}
    .lnk{display:inline-block;margin-right:10px;font-size:12px;color:#0369a1;text-decoration:none}
    .lnk:hover{text-decoration:underline}
    .notes{background:#f8fafc;border-left:3px solid #cbd5e1;border-radius:4px;padding:8px 10px;margin-top:6px;font-size:12px;color:#334155}
    .note+.note{margin-top:6px;border-top:1px dashed #e2e8f0;padding-top:6px}
    .notes a{color:#0369a1}
    .quote{margin-top:8px;font-size:12px;color:#059669;font-weight:600}
    """
    out = (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>go4it — Georgia buyer prospects</title><style>" + css + "</style></head><body><div class=wrap>"
        "<h1>go4it &mdash; Georgia buyer prospects</h1>"
        f"<div class=sub>{total} leads &middot; exported {stamp} &middot; click any company, website, email, phone or LinkedIn to open it</div>"
        + "".join(sections) +
        "</div></body></html>"
    )
    dest = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "prospects", "go4it_leads.html")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "w", encoding="utf-8") as f:
        f.write(out)
    print(f"exported {total} leads -> {dest}")


if __name__ == "__main__":
    run()
