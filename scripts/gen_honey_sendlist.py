"""Review-before-send list: every honey buyer we have an EMAIL for, read from the LIVE DB (the source of
truth the app sends from), grouped by country (priority order). Founder reviews + approves, then we send.

    ./.venv/bin/python scripts/gen_honey_sendlist.py

Writes docs/prospects/honey_sendlist.html (+ artifact fragment to $ARTIFACT_OUT). Prints counts.
"""
import os
import sys

from sqlmodel import Session, func, select

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.db import engine  # noqa: E402
from app.models import Lead  # noqa: E402

SRC = "iran-export-honey-royaljelly"
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "prospects")
os.makedirs(OUT, exist_ok=True)
RANK = {"IQ": 0, "AE": 1, "AF": 2, "GE": 3, "AM": 4, "PK": 5, "QA": 6, "KZ": 7, "RU": 8}
NAME = {"IQ": "Iraq", "AE": "United Arab Emirates", "QA": "Qatar", "PK": "Pakistan", "GE": "Georgia",
        "AM": "Armenia", "AF": "Afghanistan", "KZ": "Kazakhstan", "RU": "Russia", "": "Other"}
FLAG = {"IQ": "🇮🇶", "AE": "🇦🇪", "QA": "🇶🇦", "PK": "🇵🇰", "GE": "🇬🇪", "AM": "🇦🇲", "AF": "🇦🇫",
        "KZ": "🇰🇿", "RU": "🇷🇺", "": "🌐"}


def esc(x):
    return str(x or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def tier_badge(notes):
    n = (notes or "").lower()
    if "confirmed" in n:
        fg, bg, lbl = "#0f7b4f", "#e8f6ef", "confirmed RFQ"
    elif "plausible" in n:
        fg, bg, lbl = "#b7791f", "#fbf3e2", "likely RFQ"
    elif "unverified" in n:
        fg, bg, lbl = "#6b7280", "#f1f2f4", "unverified"
    else:
        fg, bg, lbl = "#6b7280", "#f1f2f4", "importer"
    return f'<span style="font-size:10px;font-weight:700;color:{fg};background:{bg};border-radius:999px;padding:2px 8px;white-space:nowrap;">{lbl}</span>'


def _confirmed_first(lead):
    return 0 if "confirmed" in (lead.notes or "").lower() else 1


def main():
    with Session(engine) as s:
        emailable = s.exec(select(Lead).where(Lead.source == SRC, Lead.email != "")).all()
        n_phone = s.exec(select(func.count()).where(Lead.source == SRC, Lead.email == "", Lead.phone != "")).one()
        n_total = s.exec(select(func.count()).where(Lead.source == SRC)).one()

    by_c = {}
    for lead in emailable:
        by_c.setdefault(lead.dest_country or "", []).append(lead)
    order = sorted(by_c, key=lambda k: RANK.get(k, 9))
    for iso in order:
        by_c[iso].sort(key=_confirmed_first)

    rows_html, n = [], 0
    for iso in order:
        items = by_c[iso]
        rows_html.append(
            f'<tr><td colspan="5" style="padding:14px 12px 6px;font-weight:800;font-size:14px;color:#2a2113;">'
            f'{FLAG.get(iso,"🌐")} {esc(NAME.get(iso, iso))} <span style="color:#8a8069;font-weight:600;">'
            f'{len(items)}</span></td></tr>')
        for lead in items:
            n += 1
            rows_html.append(
                f'<tr style="border-top:1px solid #eee;">'
                f'<td style="padding:8px 12px;color:#9aa0a6;font-variant-numeric:tabular-nums;">{n}</td>'
                f'<td style="padding:8px 12px;font-weight:600;">{esc(lead.buyer_company)}'
                f'<div style="font-size:11px;color:#8a8069;font-weight:400;">{esc(lead.dest_city or "")}</div></td>'
                f'<td style="padding:8px 12px;"><a href="mailto:{esc(lead.email)}" style="color:#0f7b4f;text-decoration:none;">{esc(lead.email)}</a></td>'
                f'<td style="padding:8px 12px;">{tier_badge(lead.notes)}</td>'
                f'<td style="padding:8px 12px;font-size:12px;color:#6b5d43;">{esc((lead.spec or "")[:70])}</td></tr>')

    inner = f'''<style>*{{box-sizing:border-box}}
 .sl{{max-width:960px;margin:0 auto;padding:28px 20px 56px;background:#fbf6ec;color:#2a2113;
   font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;line-height:1.5;border-radius:16px;}}
 .sl table{{width:100%;border-collapse:collapse;background:#fffdf8;border:1px solid #e6d9bd;border-radius:12px;overflow:hidden;font-size:13px;}}
 .sl thead th{{background:#15130f;color:#f0e9db;text-align:left;padding:9px 12px;font-size:11px;text-transform:uppercase;letter-spacing:.5px;}}
 .sl a{{word-break:break-all;}}</style>
<div class="sl">
  <div style="font-size:12px;font-weight:800;letter-spacing:.05em;color:#b7791f;">🍯 KIMIEL — HONEY EMAIL SEND LIST</div>
  <h1 style="font-size:25px;font-weight:800;margin:6px 0 3px;">{len(emailable)} buyers ready to email — please review &amp; approve</h1>
  <p style="margin:0 0 4px;font-size:13px;color:#6b5d43;">Everyone here has an email on file (live database). Grouped by market, priority order, confirmed-RFQ buyers first. Each gets the KIMIEL first-touch (Dear Sir/Madam, delivered CPT to their own city, LC/SWIFT/crypto) + your signature. Nothing sends until you approve.</p>
  <p style="margin:0 0 16px;font-size:12px;color:#8a8069;">Also on file: <b>{n_phone}</b> phone-only buyers (WhatsApp / call, separate). Total honey buyers: {n_total}.</p>
  <table><thead><tr><th>#</th><th>Company</th><th>Email</th><th>Type</th><th>Buys / business</th></tr></thead>
  <tbody>{''.join(rows_html)}</tbody></table>
  <p style="margin-top:16px;font-size:11px;color:#a99b7d;">Reply "send all" to email everyone, or tell me which markets / types to start with (e.g. only confirmed RFQ + UAE/Iraq).</p>
</div>'''
    open(os.path.join(OUT, "honey_sendlist.html"), "w", encoding="utf-8").write(
        f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" '
        f'content="width=device-width,initial-scale=1"><title>Honey send list</title>'
        f'<style>body{{margin:0;background:#f2ede1;padding:14px}}</style></head><body>{inner}</body></html>')
    frag = os.environ.get("ARTIFACT_OUT", os.path.join(OUT, "_honey_sendlist_fragment.html"))
    open(frag, "w", encoding="utf-8").write(inner)

    print(f"emailable: {len(emailable)} | phone-only: {n_phone} | total: {n_total}")
    for iso in order:
        print(f"  {FLAG.get(iso,'')} {NAME.get(iso, iso):22} {len(by_c[iso])}")


if __name__ == "__main__":
    main()
