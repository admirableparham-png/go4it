"""Render the HONEY target-country ranking (Task 1) into polished HTML reports.

    ./.venv/bin/python scripts/gen_honey_countries_report.py

Reads  docs/research/honey_target_countries.json   (workflow result: ranked + avoid + headline)
       docs/research/honey_target_countries_fa.json (optional Persian translation, keyed by country)
Writes docs/prospects/honey_target_countries.html        (English, full, with scores)
       docs/prospects/honey_target_countries_fa.html      (Persian RTL, full)
       docs/prospects/honey_target_countries_clean_fa.html (Persian client, no internal scoring jargon)
"""
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RES = os.path.join(ROOT, "docs", "research")
OUT = os.path.join(ROOT, "docs", "prospects")
os.makedirs(OUT, exist_ok=True)

TIER_COLOR = {
    "Prime": ("#0f7b4f", "#e8f6ef", "#0f7b4f"),
    "Strong": ("#1f6feb", "#e9f1fe", "#1f6feb"),
    "Opportunistic": ("#b7791f", "#fbf3e2", "#b7791f"),
    "Niche": ("#6b7280", "#f1f2f4", "#6b7280"),
}
AXES = [("demand", "Demand"), ("competition", "Openness"), ("iran_edge", "Iran edge"),
        ("shipping", "Shipping"), ("access", "Access")]
AXES_FA = [("demand", "تقاضا"), ("competition", "فضای باز رقابت"), ("iran_edge", "برتری ایران"),
           ("shipping", "حمل و نقل"), ("access", "دسترسی بازار")]


def bar(val, accent, rtl=False):
    pct = int(round((val or 0) / 10 * 100))
    align = "right" if rtl else "left"
    return (f'<div style="background:#e6e8ec;border-radius:6px;height:7px;overflow:hidden;">'
            f'<div style="width:{pct}%;height:7px;background:{accent};float:{align};"></div></div>')


def esc(s):
    return (str(s or "")).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def rank_cards(ranked, tr=None, rtl=False, clean=False):
    axes = AXES_FA if rtl else AXES
    dir_attr = 'dir="rtl"' if rtl else ""
    out = []
    for i, r in enumerate(ranked, 1):
        tier = r.get("tier", "Niche")
        fg, bg, accent = TIER_COLOR.get(tier, TIER_COLOR["Niche"])
        t = (tr or {}).get(r["country"], {}) if tr else {}
        why = t.get("why_iran") or r.get("why_iran", "")
        watch = t.get("watch_outs") or r.get("watch_outs", "")
        signal = t.get("signal") or r.get("signal", "")
        cname = (t.get("name_fa") if rtl else None) or r["country"]
        cregion = (t.get("region_fa") if rtl else None) or r.get("region", "")
        tier_label = {"Prime": "درجه‌یک", "Strong": "قوی", "Opportunistic": "فرصت‌محور",
                      "Niche": "نیچ/محدود"}.get(tier, tier) if rtl else tier
        why_lbl = "چرا عسل ایران اینجا برنده است" if rtl else "Why Iranian honey wins here"
        watch_lbl = "نکات مراقبت" if rtl else "Watch-outs"
        signal_lbl = "سیگنال ۲۰۲۵" if rtl else "2025 signal"
        score_html = "" if clean else (
            f'<span style="font-size:22px;font-weight:800;color:{accent};font-variant-numeric:tabular-nums;">'
            f'{r.get("composite","")}</span><span style="font-size:11px;color:#9aa0a6;">/100</span>')
        axis_html = ""
        if not clean:
            cells = []
            for key, lbl in axes:
                v = (r.get("scores") or {}).get(key)
                cells.append(
                    f'<div style="min-width:0;"><div style="font-size:10px;color:#9aa0a6;margin-bottom:3px;'
                    f'display:flex;justify-content:space-between;"><span>{lbl}</span>'
                    f'<span style="font-variant-numeric:tabular-nums;color:#6b7280;">{v if v is not None else "-"}</span></div>'
                    f'{bar(v, accent, rtl)}</div>')
            axis_html = (f'<div style="display:grid;grid-template-columns:repeat(5,1fr);gap:10px;'
                         f'margin:12px 0 4px;">{"".join(cells)}</div>')
        out.append(f'''
    <article {dir_attr} style="border:1px solid #e6e8ec;border-{'right' if rtl else 'left'}:4px solid {accent};
        border-radius:12px;padding:16px 18px;background:#fff;box-shadow:0 1px 2px rgba(16,24,40,.04);">
      <div style="display:flex;align-items:baseline;gap:10px;justify-content:space-between;">
        <div style="display:flex;align-items:baseline;gap:10px;min-width:0;">
          <span style="font-size:13px;font-weight:700;color:#9aa0a6;font-variant-numeric:tabular-nums;">#{i}</span>
          <h3 style="margin:0;font-size:18px;font-weight:750;color:#111827;">{esc(cname)}</h3>
          <span style="font-size:11px;color:#9aa0a6;">{esc(cregion)}</span>
        </div>
        <div style="display:flex;align-items:center;gap:10px;white-space:nowrap;">
          {score_html}
          <span style="font-size:11px;font-weight:700;color:{fg};background:{bg};border:1px solid {accent}33;
             border-radius:999px;padding:3px 10px;">{tier_label}</span>
        </div>
      </div>
      {axis_html}
      <p style="margin:10px 0 0;font-size:13.5px;line-height:1.6;color:#1f2937;">
        <b style="color:{accent};">{why_lbl}:</b> {esc(why)}</p>
      {f'<p style="margin:7px 0 0;font-size:12.5px;line-height:1.55;color:#6b5a2e;background:#fbf7ec;border-radius:8px;padding:7px 10px;"><b>{watch_lbl}:</b> {esc(watch)}</p>' if watch else ''}
      {f'<p style="margin:7px 0 0;font-size:11.5px;color:#8a9099;"><b>{signal_lbl}:</b> {esc(signal)}</p>' if signal and signal.lower() not in ("none known","none") else ''}
    </article>''')
    return "\n".join(out)


def avoid_block(avoid, rtl=False):
    if not avoid:
        return ""
    title = "بازارهای بسته / پرهیز (به‌خاطر تحریم، بانک، مقررات مبدأ ایران)" if rtl else \
            "Avoid / effectively closed to Iranian honey (sanctions, banking, origin rules)"
    dir_attr = 'dir="rtl"' if rtl else ""
    items = "".join(
        f'<li style="margin:0 0 8px;"><b style="color:#b42318;">{esc(a["country"])}</b> '
        f'<span style="color:#6b7280;">— {esc(a["reason"])}</span></li>' for a in avoid)
    return f'''
  <section {dir_attr} style="margin-top:34px;border:1px solid #f1c7c0;background:#fdf3f1;border-radius:14px;padding:18px 22px;">
    <h2 style="margin:0 0 12px;font-size:16px;font-weight:750;color:#b42318;">&#9888;&#65039; {title}</h2>
    <ul style="margin:0;padding-{'right' if rtl else 'left'}:20px;font-size:13.5px;line-height:1.5;list-style:disc;">{items}</ul>
  </section>'''


def page(title, subtitle, headline, tiers_note, ranked, avoid, tr=None, rtl=False, clean=False):
    font = ("'Vazirmatn','Segoe UI',Tahoma,sans-serif" if rtl else
            "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif")
    dir_attr = 'dir="rtl"' if rtl else 'dir="ltr"'
    lang = "fa" if rtl else "en"
    legend = ""
    if not clean:
        bands = ("درجه‌یک ≥۷۸ · قوی ۶۶ تا ۷۷ · فرصت‌محور ۵۴ تا ۶۵ · نیچ کمتر از ۵۴" if rtl
                 else "Prime ≥78 · Strong 66-77 · Opportunistic 54-65 · Niche <54")
        legend = f'<p style="margin:6px 0 0;font-size:11.5px;color:#9aa0a6;">{esc(tiers_note)}<br>{bands}</p>'
    src_note = "" if rtl else (
        '<p style="margin:14px 0 0;font-size:11px;color:#b0b4ba;">Demand grounded in real UN Comtrade honey '
        'imports (HS 0409); scoring on 6 axes (demand, competition-openness, Iran price/quality edge, shipping '
        'from Iran, market access) via a 30-country analyst fleet. Research-grade — verify before outreach.</p>')
    return f'''<!doctype html><html lang="{lang}" {dir_attr}><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title>
<style>
 @import url('https://fonts.googleapis.com/css2?family=Vazirmatn:wght@400;600;800&display=swap');
 *{{box-sizing:border-box}} body{{margin:0;background:#f6f7f9;color:#111827;font-family:{font};
   line-height:1.5;-webkit-font-smoothing:antialiased;}}
 .wrap{{max-width:1040px;margin:0 auto;padding:40px 22px 70px;}}
 .grid{{display:grid;grid-template-columns:1fr 1fr;gap:14px;}}
 @media(max-width:760px){{.grid{{grid-template-columns:1fr}}}}
 a{{color:#1f6feb}}
</style></head><body><div class="wrap">
  <header style="margin-bottom:26px;">
    <div style="font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:#0f7b4f;">
      &#127855; {"عسل ایران — استراتژی صادرات" if rtl else "Iranian Honey — Export Strategy"}</div>
    <h1 style="margin:6px 0 4px;font-size:30px;font-weight:820;line-height:1.15;letter-spacing:-.02em;">{esc(title)}</h1>
    <p style="margin:0;font-size:14px;color:#6b7280;">{esc(subtitle)}</p>
    <div style="margin-top:16px;padding:16px 18px;background:#0f7b4f;color:#eafaf2;border-radius:14px;">
      <div style="font-size:12px;font-weight:700;opacity:.8;text-transform:uppercase;letter-spacing:.06em;margin-bottom:5px;">
        {"جمع‌بندی مدیریتی" if rtl else "Executive summary"}</div>
      <p style="margin:0;font-size:15px;line-height:1.65;">{esc(headline)}</p>
    </div>
    {legend}
  </header>
  <div class="grid">
{rank_cards(ranked, tr=tr, rtl=rtl, clean=clean)}
  </div>
{avoid_block(avoid, rtl=rtl)}
  {src_note}
  <footer style="margin-top:30px;font-size:11px;color:#b0b4ba;text-align:center;">go4it · Iran-export intelligence</footer>
</div></body></html>'''


def artifact_fragment(headline, tiers_note, ranked, avoid, tr):
    """Content-only HTML (no doctype/html/head/body) for the Artifact tool; self-contained, system fonts,
    RTL Persian, warm-honey single-theme (a deliberate committed look)."""
    bands = "درجه‌یک ≥۷۸ · قوی ۶۶ تا ۷۷ · فرصت‌محور ۵۴ تا ۶۵ · نیچ کمتر از ۵۴"
    return f'''<style>
 :root{{--ink:#2a2113;--ink2:#6b5d43;--paper:#fbf6ec;--card:#fffdf8;--line:#eadfc8;--honey:#b7791f;--green:#0f7b4f;}}
 *{{box-sizing:border-box}}
 .honeywrap{{max-width:1060px;margin:0 auto;padding:34px 20px 64px;background:var(--paper);
   color:var(--ink);font-family:Vazirmatn,Tahoma,'Segoe UI',system-ui,sans-serif;line-height:1.6;
   direction:rtl;border-radius:16px;}}
 .honeywrap h1{{font-size:29px;font-weight:800;letter-spacing:-.01em;margin:6px 0 4px;}}
 .honeywrap h3{{margin:0;}}
 .hgrid{{display:grid;grid-template-columns:1fr 1fr;gap:14px;}}
 @media(max-width:760px){{.hgrid{{grid-template-columns:1fr}}}}
</style>
<div class="honeywrap">
  <div style="font-size:12px;font-weight:800;letter-spacing:.05em;color:var(--honey);">&#127855; عسل ایران — استراتژی صادرات</div>
  <h1>کجا عسل ایرانی بفروشیم</h1>
  <p style="margin:0;font-size:14px;color:var(--ink2);">{len(ranked)} کشور مقصد رتبه‌بندی‌شده · از پرتقاضا تا کم‌رقابت · بر پایه داده واقعی واردات (UN Comtrade، کد ۰۴۰۹) و تحلیل ۶ محوری</p>
  <div style="margin-top:16px;padding:16px 18px;background:var(--green);color:#eafaf2;border-radius:14px;">
    <div style="font-size:12px;font-weight:800;opacity:.85;letter-spacing:.04em;margin-bottom:5px;">جمع‌بندی مدیریتی</div>
    <p style="margin:0;font-size:15px;line-height:1.7;">{esc(headline)}</p>
  </div>
  <p style="margin:8px 0 20px;font-size:11.5px;color:var(--ink2);">{esc(tiers_note)}<br>{bands}</p>
  <div class="hgrid">
{rank_cards(ranked, tr=tr, rtl=True, clean=False)}
  </div>
{avoid_block(avoid, rtl=True)}
  <p style="margin-top:22px;font-size:11px;color:#a99b7d;text-align:center;">go4it · هوش صادراتی ایران · درجه پژوهشی — پیش از تماس، اعتبارسنجی شود</p>
</div>'''


def main():
    data = json.load(open(os.path.join(RES, "honey_target_countries.json"), encoding="utf-8"))
    ranked = data["ranked"]
    avoid = data.get("avoid", [])
    headline = data.get("headline", "")
    tiers_note = data.get("tiers_note", "")
    fa_path = os.path.join(RES, "honey_target_countries_fa.json")
    fa = json.load(open(fa_path, encoding="utf-8")) if os.path.exists(fa_path) else {}
    tr = fa.get("by_country", {})
    fa_headline = fa.get("headline", headline)
    fa_tiers = fa.get("tiers_note", tiers_note)
    fa_avoid = fa.get("avoid", avoid)

    # English full
    html = page("Where to sell Iranian honey", f"{len(ranked)} destination countries ranked · demand → low-competition niches",
                headline, tiers_note, ranked, avoid, rtl=False)
    open(os.path.join(OUT, "honey_target_countries.html"), "w", encoding="utf-8").write(html)

    # Persian full
    html_fa = page("کجا عسل ایرانی بفروشیم", f"{len(ranked)} کشور مقصد رتبه‌بندی‌شده · از پرتقاضا تا کم‌رقابت",
                   fa_headline, fa_tiers, ranked, fa_avoid, tr=tr, rtl=True)
    open(os.path.join(OUT, "honey_target_countries_fa.html"), "w", encoding="utf-8").write(html_fa)

    # Persian clean (client) — no internal scores/bars
    html_clean = page("بازارهای هدف عسل ایرانی", "کشورهای پیشنهادی برای صادرات، به‌ترتیب اولویت",
                      fa_headline, fa_tiers, ranked, fa_avoid, tr=tr, rtl=True, clean=True)
    open(os.path.join(OUT, "honey_target_countries_clean_fa.html"), "w", encoding="utf-8").write(html_clean)

    # Artifact fragment (self-contained, for the Artifact tool) -> scratchpad path via env or default
    frag = artifact_fragment(fa_headline, fa_tiers, ranked, fa_avoid, tr)
    frag_path = os.environ.get("ARTIFACT_OUT", os.path.join(OUT, "_honey_artifact_fragment.html"))
    open(frag_path, "w", encoding="utf-8").write(frag)

    print("wrote:")
    for f in ("honey_target_countries.html", "honey_target_countries_fa.html", "honey_target_countries_clean_fa.html"):
        print("  docs/prospects/" + f)
    print("  artifact fragment -> " + frag_path)


if __name__ == "__main__":
    main()
