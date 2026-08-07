"""Render the HONEY export offer sheet / price list (EN + Persian) from the seeded product + corridors.

    ./.venv/bin/python scripts/seed_honey_offer.py     # first, so the product/corridors exist
    ./.venv/bin/python scripts/gen_honey_offer_sheet.py

Reuses build_params + compute_quote so the delivered prices EXACTLY match what go4it quotes.
Writes docs/prospects/honey_offer_sheet_en.html + _fa.html (+ artifact fragment to $ARTIFACT_OUT).
The one-pager the founder attaches to every first-touch message. Delivered = INDICATIVE (CPT), 500 kg ref.
"""
import os
import sys

from sqlmodel import Session, select

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.db import engine  # noqa: E402
from app.models import Product  # noqa: E402
from app.quote_service import build_params  # noqa: E402
from app.quoting import compute_quote  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "prospects")
os.makedirs(OUT, exist_ok=True)
MARKETS = [("IQ", "Iraq", "عراق"), ("AE", "United Arab Emirates", "امارات"),
           ("QA", "Qatar", "قطر"), ("PK", "Pakistan", "پاکستان")]
GRADES_EN = ["Multifloral", "Thyme", "Sidr", "Honeydew", "Honey with royal jelly"]
GRADES_FA = ["چندگل (بهاره)", "آویشن", "کنار (سِدر)", "گِزانگبین (هانی‌دیو)", "عسل با ژل رویال"]


def prices(product):
    """delivered $/kg per market at 500 kg and 1000 kg, via the real engine."""
    rows = []
    with Session(engine) as s:
        for iso, en, fa in MARKETS:
            pr = build_params(s, dest_country=iso)
            d500 = compute_quote(exw_price=product.exw_price, quantity=500, weight_kg_per_unit=1,
                                 incoterm="CPT", params=pr, fx=1)["delivered_unit"]
            d1000 = compute_quote(exw_price=product.exw_price, quantity=1000, weight_kg_per_unit=1,
                                  incoterm="CPT", params=pr, fx=1)["delivered_unit"]
            rows.append((iso, en, fa, d500, d1000))
    return rows


def esc(x):
    return str(x or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build(product, rows, fa=False):
    exw = product.exw_price
    grades = GRADES_FA if fa else GRADES_EN
    d = 'dir="rtl"' if fa else 'dir="ltr"'
    L = (lambda en, fa_: fa_) if fa else (lambda en, fa_: en)
    grade_chips = "".join(
        f'<span style="display:inline-block;background:#fbf3e2;color:#8a5a12;border:1px solid #e6d3a8;'
        f'border-radius:999px;padding:4px 12px;font-size:13px;margin:3px;">{esc(g)}</span>' for g in grades)
    price_rows = "".join(
        f'<tr><td style="padding:9px 12px;font-weight:600;">{esc(fa_ if fa else en)}</td>'
        f'<td style="padding:9px 12px;text-align:center;font-variant-numeric:tabular-nums;">${d500:.2f}</td>'
        f'<td style="padding:9px 12px;text-align:center;font-variant-numeric:tabular-nums;color:#0f7b4f;font-weight:700;">${d1000:.2f}</td></tr>'
        for iso, en, fa_, d500, d1000 in rows)
    term = lambda k: {
        "title": L("Iranian Natural Honey", "عسل طبیعی ایران"),
        "sub": L("Single-origin bulk honey - export offer", "عسل تک‌منشأ فله - پیشنهاد صادراتی"),
        "grades": L("Grades available", "انواع موجود"),
        "pricing": L("Pricing (USD)", "قیمت (دلار)"),
        "market": L("Delivered market", "بازار مقصد"),
        "exwh": L("EXW Iran", "درب کارخانه (EXW)"),
        "d500": L("Delivered /kg (500 kg)", "تحویل/کیلو (۵۰۰ کیلو)"),
        "d1000": L("Delivered /kg (1 tonne)", "تحویل/کیلو (۱ تُن)"),
        "exwline": L(f"EXW ex-works Iran: <b>${exw:.2f} / kg</b> (all grades)",
                     f"قیمت درب کارخانه ایران: <b>${exw:.2f} / کیلوگرم</b> (همه انواع)"),
        "note": L("Delivered prices are indicative CPT (carriage paid to destination), for the order size "
                  "shown. Bigger orders lower the per-kg logistics share. Freight to be confirmed per shipment.",
                  "قیمت‌های تحویل، تقریبی و بر مبنای CPT (کرایه تا مقصد) برای حجم سفارش نشان‌داده‌شده است. "
                  "سفارش بزرگ‌تر، سهم لجستیک هر کیلو را کاهش می‌دهد. کرایه در هر محموله نهایی می‌شود."),
        "terms": L("Terms", "شرایط"),
        "pack": L("Packaging", "بسته‌بندی"),
        "packv": L("25 kg food-grade bulk drums", "بشکه‌های ۲۵ کیلویی درجه غذایی"),
        "moq": L("Minimum / trial order", "حداقل / سفارش آزمایشی"),
        "moqv": L("25 kg trial (1 drum); regular orders 250 kg+", "۲۵ کیلو آزمایشی (۱ بشکه)؛ سفارش عادی از ۲۵۰ کیلو"),
        "vol": L("Monthly availability", "ظرفیت ماهانه"),
        "volv": L("Up to 1 metric tonne", "تا ۱ تُن متریک"),
        "docs": L("Documents", "مدارک"),
        "docsv": L("Certificate of Origin included; lab specs (moisture / HMF / antibiotic-free) on request",
                   "گواهی مبدأ ارائه می‌شود؛ آنالیز آزمایشگاهی (رطوبت/HMF/بدون آنتی‌بیوتیک) در صورت درخواست"),
        "pay": L("Payment", "پرداخت"),
        "payv": L("Letter of Credit, SWIFT bank transfer, or cryptocurrency",
                  "اعتبار اسنادی (LC)، حواله بانکی SWIFT، یا ارز دیجیتال"),
        "sample": L("Samples", "نمونه"),
        "samplev": L("Paid 25 kg trial order; packaging + delivery payable by buyer",
                     "سفارش آزمایشی ۲۵ کیلویی؛ هزینه بسته‌بندی و ارسال بر عهده خریدار"),
        "pitch": L("Genuine single-origin Iranian natural honey with Certificate of Origin - a real, "
                   "lab-verifiable natural product, not commodity blended honey.",
                   "عسل طبیعی تک‌منشأ اصیل ایرانی همراه با گواهی مبدأ - یک محصول طبیعی واقعی و قابل آزمایش، "
                   "نه عسل فله‌ی مخلوط."),
    }[k]
    return f'''<style>
 *{{box-sizing:border-box}}
 .os{{max-width:820px;margin:0 auto;padding:34px 26px 54px;background:#fffdf8;color:#2a2113;
   font-family:{'Vazirmatn,Tahoma,' if fa else ''}-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
   line-height:1.6;border:1px solid #eadfc8;border-radius:16px;}}
 .os h1{{font-size:30px;font-weight:800;margin:6px 0 2px;letter-spacing:-.01em;}}
 .os table{{width:100%;border-collapse:collapse;margin-top:8px;font-size:14px;}}
 .os thead th{{background:#0f7b4f;color:#eafaf2;padding:9px 12px;text-align:{'right' if fa else 'left'};font-size:12px;text-transform:uppercase;letter-spacing:.04em;}}
 .os tbody tr:nth-child(even){{background:#faf6ec;}}
 .os .grid2{{display:grid;grid-template-columns:150px 1fr;gap:8px 14px;font-size:14px;margin-top:6px;}}
 @media(max-width:620px){{.os .grid2{{grid-template-columns:1fr}}}}
</style>
<div class="os" {d}>
  <div style="font-size:12px;font-weight:800;letter-spacing:.06em;color:#b7791f;">&#127855; {esc(term("sub"))}</div>
  <h1>{esc(term("title"))}</h1>
  <p style="margin:0 0 14px;font-size:14px;color:#6b5d43;">{term("pitch")}</p>

  <div style="font-size:12px;font-weight:700;color:#0f7b4f;text-transform:uppercase;letter-spacing:.05em;margin:16px 0 4px;">{esc(term("grades"))}</div>
  <div>{grade_chips}</div>

  <div style="font-size:12px;font-weight:700;color:#0f7b4f;text-transform:uppercase;letter-spacing:.05em;margin:20px 0 2px;">{esc(term("pricing"))}</div>
  <p style="margin:2px 0 4px;font-size:15px;">{term("exwline")}</p>
  <table>
    <thead><tr><th>{esc(term("market"))}</th><th style="text-align:center;">{esc(term("d500"))}</th><th style="text-align:center;">{esc(term("d1000"))}</th></tr></thead>
    <tbody>{price_rows}</tbody>
  </table>
  <p style="margin:8px 0 0;font-size:11.5px;color:#8a8069;">{esc(term("note"))}</p>

  <div style="font-size:12px;font-weight:700;color:#0f7b4f;text-transform:uppercase;letter-spacing:.05em;margin:22px 0 6px;">{esc(term("terms"))}</div>
  <div class="grid2">
    <div style="color:#8a8069;">{esc(term("pack"))}</div><div>{esc(term("packv"))}</div>
    <div style="color:#8a8069;">{esc(term("moq"))}</div><div>{esc(term("moqv"))}</div>
    <div style="color:#8a8069;">{esc(term("vol"))}</div><div>{esc(term("volv"))}</div>
    <div style="color:#8a8069;">{esc(term("docs"))}</div><div>{esc(term("docsv"))}</div>
    <div style="color:#8a8069;">{esc(term("pay"))}</div><div><b>{esc(term("payv"))}</b></div>
    <div style="color:#8a8069;">{esc(term("sample"))}</div><div>{esc(term("samplev"))}</div>
  </div>
  <p style="margin-top:24px;font-size:11px;color:#a99b7d;text-align:center;">go4it &middot; Iran export desk</p>
</div>'''


def wrap(inner, fa):
    imp = ('<style>@import url("https://fonts.googleapis.com/css2?family=Vazirmatn:wght@400;600;800&display=swap");</style>'
           if fa else "")
    lang = "fa" if fa else "en"
    return (f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>Honey offer</title>'
            f'{imp}<style>body{{margin:0;background:#f2ede1;padding:16px}}</style></head><body>{inner}</body></html>')


def main():
    with Session(engine) as s:
        product = s.exec(select(Product).where(Product.category == "food-honey")).first()
    if not product:
        print("No honey product - run scripts/seed_honey_offer.py first."); return
    rows = prices(product)
    en = build(product, rows, fa=False)
    fa = build(product, rows, fa=True)
    open(os.path.join(OUT, "honey_offer_sheet_en.html"), "w", encoding="utf-8").write(wrap(en, False))
    open(os.path.join(OUT, "honey_offer_sheet_fa.html"), "w", encoding="utf-8").write(wrap(fa, True))
    frag = os.environ.get("ARTIFACT_OUT", os.path.join(OUT, "_honey_offer_fragment.html"))
    open(frag, "w", encoding="utf-8").write(en)
    print("wrote docs/prospects/honey_offer_sheet_en.html + _fa.html")
    for iso, en_, fa_, d500, d1000 in rows:
        print(f"  {en_:24} delivered ${d500:.2f}/kg (500kg)  ${d1000:.2f}/kg (1t)")


if __name__ == "__main__":
    main()
