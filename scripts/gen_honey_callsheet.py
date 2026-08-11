"""Wave-1 HONEY call sheet: the prioritized list of reachable buyers to contact first, each with a
ready-to-send email + WhatsApp draft (offer + delivered price to their market). The founder works top-down.

    ./.venv/bin/python scripts/gen_honey_callsheet.py           # top 30
    HONEY_CALLSHEET_N=60 ./.venv/bin/python scripts/gen_honey_callsheet.py

Reuses honey_message + signature_text so each draft matches exactly what go4it sends.
Writes docs/prospects/honey_callsheet.html (+ artifact fragment to $ARTIFACT_OUT).
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.outreach import honey_message, signature_text  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "prospects")
RES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "research")
RANK = {"IQ": 0, "AE": 1, "AF": 2, "GE": 3, "AM": 4, "PK": 5, "QA": 6, "KZ": 7}
NAME = {"IQ": "Iraq", "AE": "the UAE", "QA": "Qatar", "PK": "Pakistan", "GE": "Georgia",
        "AM": "Armenia", "AF": "Afghanistan", "KZ": "Kazakhstan", "RU": "Russia"}
FLAG = {"IQ": "🇮🇶", "AE": "🇦🇪", "QA": "🇶🇦", "PK": "🇵🇰", "GE": "🇬🇪", "AM": "🇦🇲",
        "AF": "🇦🇫", "KZ": "🇰🇿", "RU": "🇷🇺"}


def esc(x):
    return str(x or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def wa_text():
    return ("Hello, this is KIMIEL (Dubai) - we supply laboratory-tested Iranian honey, bulk & "
            "private-label, in 25kg food-grade drums with Certificate of Origin. Raw 40-flower / "
            "Astragalus / Coriander from USD 10.25/kg and Mountain Javashir USD 13.25/kg (CPT, "
            "budgetary). Could we explore a wholesale or private-label supply?")


def card(i, b):
    iso = b.get("dest_iso", "")
    dn = NAME.get(iso, "")
    lead = SimpleNamespace(contact_name="", buyer_company=b.get("company", ""), dest_country=iso,
                           dest_city=b.get("city", ""))
    subj, body = honey_message(lead)
    body = body + "\n\n" + signature_text()
    email = b.get("email", "")
    phone = (b.get("phones") or [""])[0]
    site = b.get("website", "")
    tier = b.get("verdict") or b.get("tier") or ""
    contact = " · ".join(x for x in [
        f'<a href="mailto:{esc(email)}" style="color:#0f7b4f;">{esc(email)}</a>' if email else "",
        f'<a href="tel:{esc(phone)}" style="color:#1f6feb;">{esc(phone)}</a>' if phone else "",
        f'<a href="{esc(site if "://" in site else "https://"+site)}" style="color:#8a6d1f;">site</a>' if site else "",
    ] if x)
    wa = ""
    if phone:
        digits = "".join(c for c in phone if c.isdigit())
        wa = (f'<a href="https://wa.me/{digits}?text={esc(wa_text()).replace(chr(34),"")}" '
              f'style="display:inline-block;margin-top:6px;background:#25d366;color:#fff;border-radius:8px;'
              f'padding:5px 12px;font-size:12px;font-weight:700;text-decoration:none;">WhatsApp &#8599;</a>')
    price_badge = '<span style="color:#0f7b4f;font-weight:700;">$10.25&ndash;13.25/kg CPT</span>'
    return f'''
  <div style="border:1px solid #e6d9bd;border-radius:12px;padding:14px 16px;background:#fffdf8;">
    <div style="display:flex;justify-content:space-between;gap:8px;align-items:baseline;">
      <div style="font-weight:750;font-size:15px;">{i}. {esc(b.get("company",""))}
        <span style="font-size:12px;color:#8a8069;font-weight:400;">{FLAG.get(iso,"")} {esc(dn or iso)}{(" · "+esc(b.get("city",""))) if b.get("city") and b.get("city")!=dn else ""}</span></div>
      <div style="font-size:11px;white-space:nowrap;">{price_badge}{(" · "+esc(tier)) if tier else ""}</div>
    </div>
    <div style="font-size:12.5px;margin-top:4px;">{contact or '<span style="color:#c0392b;">no direct contact — enrich</span>'}</div>
    {f'<div style="font-size:11.5px;color:#6b5d43;margin-top:3px;">wants: {esc((b.get("wants") or b.get("business") or ", ".join(b.get("buys",[])))[:110])}</div>' if (b.get("wants") or b.get("business") or b.get("buys")) else ''}
    <details style="margin-top:8px;"><summary style="cursor:pointer;font-size:12px;color:#0f7b4f;font-weight:600;">Email draft — {esc(subj)}</summary>
      <pre style="white-space:pre-wrap;background:#faf6ec;border:1px solid #eadfc8;border-radius:8px;padding:10px;font-size:12px;margin:6px 0 0;font-family:ui-monospace,Menlo,monospace;">{esc(body)}</pre></details>
    {wa}
  </div>'''


def main():
    n = int(os.environ.get("HONEY_CALLSHEET_N", "30"))
    import json
    buyers = json.load(open(os.path.join(RES, "honey-royaljelly_export_buyers.json"), encoding="utf-8"))["buyers"]
    reachable = [b for b in buyers if b.get("email") or (b.get("phones") or [""])[0]]
    reachable.sort(key=lambda b: (RANK.get(b.get("dest_iso", ""), 9), -(b.get("match_score") or 0)))
    top = reachable[:n]
    cards = "\n".join(card(i + 1, b) for i, b in enumerate(top))
    by_c = {}
    for b in top:
        by_c[b.get("dest_iso", "?")] = by_c.get(b.get("dest_iso", "?"), 0) + 1
    dist = " · ".join(f"{FLAG.get(k,'')}{NAME.get(k,k)} {v}" for k, v in
                      sorted(by_c.items(), key=lambda kv: RANK.get(kv[0], 9)))
    inner = f'''<style>*{{box-sizing:border-box}}
 .cs{{max-width:900px;margin:0 auto;padding:30px 22px 60px;background:#fbf6ec;color:#2a2113;
   font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;line-height:1.55;border-radius:16px;}}
 .cs a{{text-decoration:none}} .cs summary::-webkit-details-marker{{display:none}}</style>
<div class="cs">
  <div style="font-size:12px;font-weight:800;letter-spacing:.05em;color:#b7791f;">&#127855; HONEY — WAVE 1 CALL SHEET</div>
  <h1 style="font-size:26px;font-weight:800;margin:6px 0 2px;">Your first {len(top)} honey buyers to contact</h1>
  <p style="margin:0 0 4px;font-size:13px;color:#6b5d43;">Reachable now (email/phone on file), priority order. Each has a ready email (click to expand) + WhatsApp. Prices are budgetary delivered CPT (subject to freight confirmation).</p>
  <p style="margin:0 0 18px;font-size:12px;color:#8a8069;">{dist}</p>
  <div style="display:grid;gap:12px;">{cards}</div>
  <p style="margin-top:22px;font-size:11px;color:#a99b7d;text-align:center;">go4it · work top-down · log each send in the lead page (owner + follow-up)</p>
</div>'''
    open(os.path.join(OUT, "honey_callsheet.html"), "w", encoding="utf-8").write(
        f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" '
        f'content="width=device-width,initial-scale=1"><title>Honey call sheet</title>'
        f'<style>body{{margin:0;background:#f2ede1;padding:14px}}</style></head><body>{inner}</body></html>')
    frag = os.environ.get("ARTIFACT_OUT", os.path.join(OUT, "_honey_callsheet_fragment.html"))
    open(frag, "w", encoding="utf-8").write(inner)
    print(f"wrote docs/prospects/honey_callsheet.html — {len(top)} buyers ({dist})")


if __name__ == "__main__":
    main()
