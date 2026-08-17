"""Review-before-send list: every ZINC SULPHATE buyer we have an EMAIL for, read from the LIVE DB (the
source of truth the app sends from), grouped by country (regional priority order). Founder reviews +
approves, then we send.

    ./.venv/bin/python scripts/gen_zinc_sendlist.py

Writes docs/prospects/zinc_sendlist.html (+ artifact fragment to $ARTIFACT_OUT). Prints counts.
"""
import os
import sys

from sqlmodel import Session, func, select

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.db import engine  # noqa: E402
from app.models import Lead  # noqa: E402

SRC = "iran-export-zinc-sulfate"
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "prospects")
os.makedirs(OUT, exist_ok=True)
# Regional-first: tier-A neighbor/landlocked cells (Iran's freight edge), then tier-B big markets.
RANK = {"AF": 0, "IQ": 1, "UZ": 2, "KZ": 3, "TM": 4, "TJ": 5, "KG": 6, "AZ": 7, "AM": 8, "GE": 9,
        "PK": 10, "TR": 11, "AE": 12, "SA": 13, "OM": 14, "QA": 15, "KW": 16, "BH": 17}
NAME = {"AF": "Afghanistan", "IQ": "Iraq", "UZ": "Uzbekistan", "KZ": "Kazakhstan", "TM": "Turkmenistan",
        "TJ": "Tajikistan", "KG": "Kyrgyzstan", "AZ": "Azerbaijan", "AM": "Armenia", "GE": "Georgia",
        "PK": "Pakistan", "TR": "Turkey", "AE": "United Arab Emirates", "SA": "Saudi Arabia",
        "OM": "Oman", "QA": "Qatar", "KW": "Kuwait", "BH": "Bahrain", "": "Other"}
FLAG = {"AF": "🇦🇫", "IQ": "🇮🇶", "UZ": "🇺🇿", "KZ": "🇰🇿", "TM": "🇹🇲", "TJ": "🇹🇯", "KG": "🇰🇬",
        "AZ": "🇦🇿", "AM": "🇦🇲", "GE": "🇬🇪", "PK": "🇵🇰", "TR": "🇹🇷", "AE": "🇦🇪", "SA": "🇸🇦",
        "OM": "🇴🇲", "QA": "🇶🇦", "KW": "🇰🇼", "BH": "🇧🇭", "": "🌐"}


def esc(x):
    return str(x or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def type_badge(notes):
    n = (notes or "").lower()
    if "feed" in n:
        fg, bg, lbl = "#8a4b0f", "#fbeede", "feed / premix"
    elif "industrial" in n:
        fg, bg, lbl = "#374151", "#eef0f3", "industrial"
    elif "agrochem" in n:
        fg, bg, lbl = "#0f5f7b", "#e4f2f6", "agrochem"
    elif "importer" in n or "trader" in n:
        fg, bg, lbl = "#6b7280", "#f1f2f4", "importer / trader"
    else:
        fg, bg, lbl = "#0f7b4f", "#e8f6ef", "agri-input"
    return f'<span style="font-size:10px;font-weight:700;color:{fg};background:{bg};border-radius:999px;padding:2px 8px;white-space:nowrap;">{lbl}</span>'


def _score_first(lead):
    # highest match_score first within a country (score is stored in notes: "match NN | ...")
    try:
        return -int((lead.notes or "").split("match", 1)[1].split("|", 1)[0].strip())
    except (IndexError, ValueError):
        return 0


def main():
    with Session(engine) as s:
        emailable = s.exec(select(Lead).where(Lead.source == SRC, Lead.email != "")).all()
        n_phone = s.exec(select(func.count()).where(Lead.source == SRC, Lead.email == "", Lead.phone != "")).one()
        n_total = s.exec(select(func.count()).where(Lead.source == SRC)).one()

    by_c = {}
    for lead in emailable:
        by_c.setdefault(lead.dest_country or "", []).append(lead)
    order = sorted(by_c, key=lambda k: RANK.get(k, 99))
    for iso in order:
        by_c[iso].sort(key=_score_first)

    rows_html, n = [], 0
    for iso in order:
        items = by_c[iso]
        rows_html.append(
            f'<tr><td colspan="5" style="padding:14px 12px 6px;font-weight:800;font-size:14px;color:#12233a;">'
            f'{FLAG.get(iso,"🌐")} {esc(NAME.get(iso, iso))} <span style="color:#7089a6;font-weight:600;">'
            f'{len(items)}</span></td></tr>')
        for lead in items:
            n += 1
            rows_html.append(
                f'<tr style="border-top:1px solid #e7edf3;">'
                f'<td style="padding:8px 12px;color:#9aa8b6;font-variant-numeric:tabular-nums;">{n}</td>'
                f'<td style="padding:8px 12px;font-weight:600;">{esc(lead.buyer_company)}'
                f'<div style="font-size:11px;color:#7089a6;font-weight:400;">{esc(lead.dest_city or "")}</div></td>'
                f'<td style="padding:8px 12px;"><a href="mailto:{esc(lead.email)}" style="color:#0f5f7b;text-decoration:none;">{esc(lead.email)}</a></td>'
                f'<td style="padding:8px 12px;">{type_badge(lead.notes)}</td>'
                f'<td style="padding:8px 12px;font-size:12px;color:#425a72;">{esc((lead.spec or "")[:70])}</td></tr>')

    inner = f'''<style>*{{box-sizing:border-box}}
 .sl{{max-width:960px;margin:0 auto;padding:28px 20px 56px;background:#f2f6fa;color:#12233a;
   font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;line-height:1.5;border-radius:16px;}}
 .sl table{{width:100%;border-collapse:collapse;background:#ffffff;border:1px solid #d3e0ec;border-radius:12px;overflow:hidden;font-size:13px;}}
 .sl thead th{{background:#0f2033;color:#dfe8f2;text-align:left;padding:9px 12px;font-size:11px;text-transform:uppercase;letter-spacing:.5px;}}
 .sl a{{word-break:break-all;}}</style>
<div class="sl">
  <div style="font-size:12px;font-weight:800;letter-spacing:.05em;color:#0f5f7b;">⚗️ KIMIEL — ZINC SULPHATE EMAIL SEND LIST</div>
  <h1 style="font-size:25px;font-weight:800;margin:6px 0 3px;">{len(emailable)} buyers ready to email — please review &amp; approve</h1>
  <p style="margin:0 0 4px;font-size:13px;color:#425a72;">Everyone here has an email on file (live database). Grouped by market, regional priority order, highest-fit first. Each gets the KIMIEL zinc first-touch (Dear Sir/Madam, lab-tested 33% Zn, delivered CPT quoted to their own city) + your signature. Nothing sends until you approve.</p>
  <p style="margin:0 0 16px;font-size:12px;color:#7089a6;">Also on file: <b>{n_phone}</b> phone-only buyers (WhatsApp / call, separate). Total zinc buyers: {n_total}.</p>
  <table><thead><tr><th>#</th><th>Company</th><th>Email</th><th>Type</th><th>Buys / business</th></tr></thead>
  <tbody>{''.join(rows_html)}</tbody></table>
  <p style="margin-top:16px;font-size:11px;color:#8ba0b5;">Reply "send all" to email everyone, or tell me which markets / types to start with (e.g. only Afghanistan + Iraq + Central Asia).</p>
</div>'''
    open(os.path.join(OUT, "zinc_sendlist.html"), "w", encoding="utf-8").write(
        f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" '
        f'content="width=device-width,initial-scale=1"><title>Zinc sulphate send list</title>'
        f'<style>body{{margin:0;background:#e8eef4;padding:14px}}</style></head><body>{inner}</body></html>')
    frag = os.environ.get("ARTIFACT_OUT", os.path.join(OUT, "_zinc_sendlist_fragment.html"))
    open(frag, "w", encoding="utf-8").write(inner)

    print(f"emailable: {len(emailable)} | phone-only: {n_phone} | total: {n_total}")
    for iso in order:
        print(f"  {FLAG.get(iso,'')} {NAME.get(iso, iso):22} {len(by_c[iso])}")


if __name__ == "__main__":
    main()
