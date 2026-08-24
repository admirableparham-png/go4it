"""Phase 6 — branded quote PDF generation (buyer-safe, immutable, hashed).

Renders an approved QuoteVersion into a Go4it-branded one/two-pager through the hardened sandbox
(pdf_render.render_pdf). Every dynamic field is HTML-escaped. NEVER includes seller/supplier identity or
contacts, internal cost components, Go4it margin, admin notes, buyer research, or another tenant's data. Stored
privately with its sha256 as an immutable QuoteDocument. Buyer access is only through the token portal.
"""
import json
from datetime import datetime

from . import pdf_render as PDF


def _fields(session, quote, version, buyer_name="", link=""):
    """The buyer-facing fields for the PDF — delivered price only, no cost breakdown or margin."""
    from .models import Lead
    lead = session.get(Lead, quote.lead_id) if quote.lead_id else None
    snap = {}
    try:
        snap = json.loads(version.product_snapshot or "{}")
    except Exception:  # noqa: BLE001
        snap = {}
    options = []
    try:
        options = json.loads(version.options or "[]")
    except Exception:  # noqa: BLE001
        options = []
    return {
        "ref": quote.tracking_code, "version": version.version,
        "issued": (quote.created_at or datetime.utcnow()).strftime("%Y-%m-%d"),
        "expires": version.validity_at.strftime("%Y-%m-%d") if version.validity_at else "",
        "buyer": buyer_name or (lead.buyer_company if lead else "") or "Valued buyer",
        "product": snap.get("name", ""), "spec": snap.get("spec", ""), "hs_code": snap.get("hs_code", ""),
        "quantity": version.quantity, "unit": version.unit or snap.get("unit", ""),
        "unit_price": version.unit_price, "total": version.total, "currency": version.currency,
        "incoterm": version.incoterm, "origin": version.origin, "destination": version.destination,
        "packaging": snap.get("packaging", ""), "lead_time": snap.get("lead_time_days", ""),
        "payment_terms": version.payment_terms, "commercial_text": version.commercial_text,
        "options": options, "link": link,
    }


def _money(v) -> str:
    try:
        return f"{float(v):.2f}"
    except (TypeError, ValueError):
        return "0.00"


def render_quote_html(fields: dict) -> str:
    e = PDF.escape
    opt_rows = ""
    for o in fields.get("options", []):
        opt_rows += (f"<tr><td>{e(o.get('name'))} ({e(o.get('incoterm'))})</td>"
                     f"<td class='r'>{e(o.get('unit_price'))} {e(fields.get('currency'))}/unit</td></tr>"
                     f"<tr><td colspan='2' class='sm'>Included: {e(o.get('included'))} · Excluded: {e(o.get('excluded'))}</td></tr>")
    return f"""<!doctype html><html><head><meta charset='utf-8'><style>
      @page {{ size: A4; margin: 18mm; }}
      body {{ font-family:'Helvetica Neue',Arial,sans-serif; color:#0f172a; font-size:12px; }}
      .hd {{ border-bottom:3px solid #0ea5e9; padding-bottom:10px; margin-bottom:14px; }}
      .brand {{ color:#0ea5e9; font-weight:800; letter-spacing:.04em; font-size:12px; }}
      h1 {{ font-size:22px; margin:6px 0 2px; }}
      .meta {{ color:#475569; font-size:11px; }}
      table {{ width:100%; border-collapse:collapse; margin:8px 0; }}
      td {{ padding:5px 4px; border-bottom:1px solid #e2e8f0; }}
      .r {{ text-align:right; }} .sm {{ color:#64748b; font-size:10px; }}
      .tot {{ font-size:16px; font-weight:700; }}
      .ft {{ margin-top:20px; border-top:1px solid #e2e8f0; padding-top:8px; color:#64748b; font-size:10px; }}
      .box {{ background:#f8fafc; border:1px solid #e2e8f0; border-radius:6px; padding:8px 10px; margin:8px 0; }}
    </style></head><body>
      <div class='hd'><div class='brand'>GO4IT · QUOTATION</div>
        <h1>Quotation {e(fields['ref'])} · v{e(fields['version'])}</h1>
        <div class='meta'>Issued {e(fields['issued'])}{(' · valid until ' + e(fields['expires'])) if fields['expires'] else ''} · Prepared for {e(fields['buyer'])}</div>
      </div>
      <table>
        <tr><td class='sm'>Product</td><td>{e(fields['product'])}{(' · ' + e(fields['spec'])) if fields['spec'] else ''}</td></tr>
        <tr><td class='sm'>HS code</td><td>{e(fields['hs_code']) or '—'}</td></tr>
        <tr><td class='sm'>Quantity</td><td>{e(fields['quantity'])} {e(fields['unit'])}</td></tr>
        <tr><td class='sm'>Incoterm</td><td>{e(fields['incoterm'])} · {e(fields['origin']) or '—'} → {e(fields['destination']) or '—'}</td></tr>
        <tr><td class='sm'>Packaging</td><td>{e(fields['packaging']) or '—'}</td></tr>
        <tr><td class='sm'>Lead time</td><td>{(str(e(fields['lead_time'])) + ' days') if fields['lead_time'] else '—'}</td></tr>
        <tr><td class='sm'>Payment terms</td><td>{e(fields['payment_terms']) or 'As agreed'}</td></tr>
      </table>
      {('<div class=box><b>Options</b><table>' + opt_rows + '</table></div>') if opt_rows else ''}
      <table><tr><td class='tot'>Total ({e(fields['incoterm'])})</td><td class='r tot'>{_money(fields['total'])} {e(fields['currency'])}</td></tr>
        <tr><td class='sm'>Unit price</td><td class='r'>{_money(fields['unit_price'])} {e(fields['currency'])}/{e(fields['unit']) or 'unit'}</td></tr></table>
      {('<div class=box>' + e(fields['commercial_text']) + '</div>') if fields['commercial_text'] else ''}
      <div class='box'>To accept this quotation, open your secure link: {e(fields['link']) or '(link provided by email)'}</div>
      <div class='ft'>Prices indicative and subject to confirmation. All enquiries via Go4it (trade@go4it.vip). Go4it mediates the transaction — the underlying source is not disclosed.</div>
    </body></html>"""


def generate_quote_pdf(session, quote, version, out_dir, actor=None, link=""):
    """Render + store an immutable quote PDF with its sha256 (a new QuoteDocument each time; never overwrites).
    Returns (QuoteDocument|None, error). Only for an approved version."""
    from .models import QuoteDocument
    buyer = ""
    fields = _fields(session, quote, version, buyer_name=buyer, link=link)
    html = render_quote_html(fields)
    if PDF.has_unresolved_vars(html):
        return None, "unresolved template variables"
    doc = QuoteDocument(quote_version_id=version.id, created_by=getattr(actor, "email", "") or "")
    session.add(doc); session.flush()
    out_dir = out_dir / str(quote.id)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"quote_{quote.id}_v{version.version}_{doc.id}.pdf"
    ok, err = PDF.render_pdf(html, out_path)
    if not ok:
        session.delete(doc)
        return None, err
    doc.file_path = f"{quote.id}/{out_path.name}"
    doc.sha256 = PDF.sha256_file(out_path)
    doc.size_bytes = out_path.stat().st_size
    session.add(doc)
    version.pdf_document_id = doc.id
    session.add(version)
    return doc, ""
