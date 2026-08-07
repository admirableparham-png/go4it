"""Render the honey buyer hunt (Task 2+3) into a polished Persian RTL report (full + source-free).

    ./.venv/bin/python scripts/gen_honey_buyers_report.py

Reads  docs/research/honey_buyers_by_country.json  {cells:[{country, active:[...], potential:[...], note}]}
Writes docs/prospects/honey_buyers_fa.html        (Persian RTL, full: sources + verdicts + contacts)
       docs/prospects/honey_buyers_clean_fa.html   (Persian RTL, source-free client version)
Also writes an artifact fragment to $ARTIFACT_OUT (or docs/prospects/_honey_buyers_fragment.html).
Buyer names + their stated requests stay in original language for accurate outreach; chrome is Persian.
"""
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RES = os.path.join(ROOT, "docs", "research")
OUT = os.path.join(ROOT, "docs", "prospects")
os.makedirs(OUT, exist_ok=True)

FA = {"Iraq": "عراق", "United Arab Emirates": "امارات متحده عربی", "Afghanistan": "افغانستان",
      "Georgia": "گرجستان", "Armenia": "ارمنستان", "Pakistan": "پاکستان", "Qatar": "قطر",
      "Kazakhstan": "قزاقستان"}
VERDICT = {"confirmed": ("تأییدشده", "#0f7b4f", "#e8f6ef"),
           "plausible": ("محتمل", "#b7791f", "#fbf3e2"),
           "unverified": ("تأییدنشده", "#6b7280", "#f1f2f4")}


def esc(s):
    return (str(s or "")).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def link(url, label, color="#1f6feb"):
    if not url:
        return ""
    href = url if "://" in url else "https://" + url
    return f'<a href="{esc(href)}" target="_blank" rel="noopener" style="color:{color};text-decoration:none;">{label}&#8599;</a>'


def contact_bits(rec):
    bits = []
    if rec.get("contact"):
        bits.append(f'<span style="color:#3f4753;">{esc(rec["contact"])[:60]}</span>')
    if rec.get("linkedin"):
        bits.append(link(rec["linkedin"], "LinkedIn", "#0a66c2"))
    if rec.get("website"):
        bits.append(link(rec["website"], "سایت", "#1f6feb"))
    return " · ".join(b for b in bits if b)


def active_card(a, clean):
    vlabel, vfg, vbg = VERDICT.get(a.get("verdict", "plausible"), VERDICT["plausible"])
    src = "" if clean else link(a.get("source_url"), "منبع", "#1f6feb")
    badge = "" if clean else (
        f'<span style="font-size:10px;font-weight:700;color:{vfg};background:{vbg};border-radius:999px;'
        f'padding:2px 9px;">{vlabel}</span>')
    role = f' · <span style="color:#6b5d43;">&#128100; {esc(a["role"])[:44]}</span>' if a.get("role") else ""
    posted = f'<span style="color:#8a9099;">&#128197; {esc(a["posted"])}</span>' if a.get("posted") else ""
    contact = contact_bits(a)
    meta = " · ".join(x for x in [posted, src] if x)
    return f'''
      <div style="border:1px solid #e6d9bd;border-right:3px solid #0f7b4f;border-radius:10px;padding:12px 14px;background:#fffdf8;">
        <div style="display:flex;justify-content:space-between;align-items:baseline;gap:8px;">
          <b style="font-size:14.5px;color:#2a2113;">{esc(a.get("buyer",""))}</b>{badge}
        </div>
        <div style="font-size:12px;color:#6b5d43;margin-top:2px;">&#128205; {esc(a.get("city",""))}{role}</div>
        <div style="font-size:13px;color:#3f4753;margin-top:6px;"><b style="color:#0f7b4f;">درخواست:</b> {esc(a.get("wants",""))}</div>
        {f'<div style="font-size:12px;margin-top:6px;">{contact}</div>' if contact else ''}
        {f'<div style="font-size:11.5px;margin-top:6px;color:#8a9099;">{meta}</div>' if meta else ''}
      </div>'''


def potential_card(p, clean):
    r = max(1, min(5, int(p.get("rating") or 3)))
    stars = f'<span style="color:#b7791f;letter-spacing:1px;">{"★"*r}{"☆"*(5-r)}</span>'
    contact = contact_bits(p)
    return f'''
      <div style="border:1px solid #e6e2d6;border-radius:10px;padding:12px 14px;background:#fff;">
        <div style="display:flex;justify-content:space-between;align-items:baseline;gap:8px;">
          <b style="font-size:14px;color:#2a2113;">{esc(p.get("company",""))}</b>{stars}
        </div>
        <div style="font-size:12px;color:#6b5d43;margin-top:2px;">&#128205; {esc(p.get("city",""))} · {esc(p.get("business",""))[:70]}</div>
        <div style="font-size:12.5px;color:#3f4753;margin-top:6px;"><b style="color:#b7791f;">چرا مناسب است:</b> {esc(p.get("why_fit",""))}</div>
        {f'<div style="font-size:12px;margin-top:6px;">{contact}</div>' if contact else ''}
      </div>'''


def country_section(cell, clean):
    country = cell["country"]
    fa = FA.get(country, country)
    active = cell.get("active", [])
    pot = sorted(cell.get("potential", []), key=lambda p: -(p.get("rating") or 0))
    conf = sum(1 for a in active if a.get("verdict") == "confirmed")
    a_html = "".join(active_card(a, clean) for a in active) or \
        '<div style="font-size:12.5px;color:#8a9099;padding:6px 2px;">درخواست خرید عمومی و ثبت‌شده‌ای (۲۰۲۵ به بعد) برای این کشور یافت نشد — تقاضای واقعی این بازار بیشتر از مسیر غیررسمی/زمینی است؛ خریداران بالقوه زیر مسیر عملی هستند.</div>'
    p_html = "".join(potential_card(p, clean) for p in pot)
    conf_txt = "" if clean else f' · <span style="color:#0f7b4f;">{conf} تأییدشده</span>'
    return f'''
  <section style="margin-top:30px;">
    <h2 style="margin:0 0 4px;font-size:21px;font-weight:800;color:#2a2113;border-bottom:2px solid #e6d9bd;padding-bottom:6px;">
      {esc(fa)} <span style="font-size:12px;font-weight:600;color:#8a9099;">({len(active)} فعال{conf_txt} · {len(pot)} بالقوه)</span></h2>
    <div style="font-size:12px;font-weight:700;color:#0f7b4f;margin:14px 0 8px;">&#128226; ردیف ۱ — خریدارانی که درخواست خرید ثبت کرده‌اند (۲۰۲۵ به بعد)</div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;">{a_html}</div>
    <div style="border-top:1px dashed #d9cba8;margin:18px 0 8px;"></div>
    <div style="font-size:12px;font-weight:700;color:#b7791f;margin:0 0 8px;">&#11088; ردیف ۲ — خریداران بالقوه (واردکننده/توزیع‌کننده مناسب، امتیازدهی‌شده)</div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;">{p_html}</div>
  </section>'''


def build(cells, clean, totals):
    body = "".join(country_section(c, clean) for c in cells)
    sub = ("خریداران واقعی عسل ایرانی در ۸ کشور هدف — ردیف ۱: درخواست‌های خرید ثبت‌شده؛ ردیف ۲: خریداران بالقوهٔ امتیازدهی‌شده"
           if not clean else "خریداران هدف عسل ایرانی در ۸ کشور — به تفکیک کشور و اولویت")
    tline = "" if clean else (
        f'<p style="margin:6px 0 0;font-size:12px;color:#6b5d43;">مجموع: {totals["active"]} خریدار فعال '
        f'({totals["confirmed"]} تأییدشده) و {totals["potential"]} خریدار بالقوه · درجه پژوهشی، پیش از تماس اعتبارسنجی شود</p>')
    return f'''<style>
 *{{box-sizing:border-box}}
 .hb{{max-width:1080px;margin:0 auto;padding:34px 20px 64px;background:#fbf6ec;color:#2a2113;
   font-family:Vazirmatn,Tahoma,'Segoe UI',system-ui,sans-serif;line-height:1.65;direction:rtl;border-radius:16px;}}
 @media(max-width:760px){{.hb section > div[style*="grid-template-columns:1fr 1fr"]{{grid-template-columns:1fr !important}}}}
</style>
<div class="hb" dir="rtl">
  <div style="font-size:12px;font-weight:800;letter-spacing:.05em;color:#b7791f;">&#127855; عسل ایران — خریداران هدف</div>
  <h1 style="margin:6px 0 4px;font-size:28px;font-weight:800;">خریداران عسل ایرانی به تفکیک کشور</h1>
  <p style="margin:0;font-size:13.5px;color:#6b5d43;">{sub}</p>
  {tline}
  {body}
  <p style="margin-top:26px;font-size:11px;color:#a99b7d;text-align:center;">go4it · هوش صادراتی ایران</p>
</div>'''


def main():
    data = json.load(open(os.path.join(RES, "honey_buyers_by_country.json"), encoding="utf-8"))
    cells = data["cells"]
    totals = data.get("totals", {"active": 0, "confirmed": 0, "potential": 0})

    full = build(cells, clean=False, totals=totals)
    open(os.path.join(OUT, "honey_buyers_fa.html"), "w", encoding="utf-8").write(
        f'<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>خریداران عسل ایرانی</title>'
        f'<style>@import url("https://fonts.googleapis.com/css2?family=Vazirmatn:wght@400;600;800&display=swap");'
        f'body{{margin:0;background:#f2ede1}}</style></head><body>{full}</body></html>')

    cleanhtml = build(cells, clean=True, totals=totals)
    open(os.path.join(OUT, "honey_buyers_clean_fa.html"), "w", encoding="utf-8").write(
        f'<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>خریداران هدف عسل ایرانی</title>'
        f'<style>@import url("https://fonts.googleapis.com/css2?family=Vazirmatn:wght@400;600;800&display=swap");'
        f'body{{margin:0;background:#f2ede1}}</style></head><body>{cleanhtml}</body></html>')

    frag_path = os.environ.get("ARTIFACT_OUT", os.path.join(OUT, "_honey_buyers_fragment.html"))
    open(frag_path, "w", encoding="utf-8").write(full)

    print("wrote:")
    for f in ("honey_buyers_fa.html", "honey_buyers_clean_fa.html"):
        print("  docs/prospects/" + f)
    print("  artifact fragment -> " + frag_path)


if __name__ == "__main__":
    main()
