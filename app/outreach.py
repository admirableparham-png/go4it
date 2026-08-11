"""Buyer outreach: build a first-touch message from a lead and send it via SMTP (HTML + plain text)
when configured. When SMTP is off, the routes fall back to click-to-email (mailto) and click-to-
WhatsApp links and record the attempt as 'logged'. send_email never raises.

The KIMIEL brand signature (app/outreach_signature.html) is appended at SEND time, so the editable
message body stays clean and the signature is always attached. HTML email carries the styled
signature; a plain-text alternative is always included for deliverability.
"""
import os
import smtplib
import ssl
from datetime import date, timedelta
from email.message import EmailMessage
from email.utils import make_msgid

from .config import (BASE_URL, OUTREACH_SIGNATURE, SMTP_ENABLED, SMTP_FROM, SMTP_HOST,
                     SMTP_PASSWORD, SMTP_PORT, SMTP_USER)

_SIG_PATH = os.path.join(os.path.dirname(__file__), "outreach_signature.html")
_SIGNATURE_TEXT = ("Best regards,\n\n"
                   "KIMIEL Sales Team\n"
                   "Honey Supply | Bulk & Private Label\n"
                   "hello@kimiel.com  |  kimiel.com  |  +971 58 817 3498\n"
                   "Dubai, UAE")

COUNTRY = {"IQ": "Iraq", "AE": "the UAE", "QA": "Qatar", "PK": "Pakistan", "GE": "Georgia",
           "AM": "Armenia", "AF": "Afghanistan", "KZ": "Kazakhstan", "RU": "Russia",
           "OM": "Oman", "SA": "Saudi Arabia", "KW": "Kuwait", "BH": "Bahrain"}

# Founder-supplied honey offer (delivered CPT, budgetary; subject to written freight confirmation).
HONEY_VARIETIES = [
    ("Raw 40-flower honey", "10.25"),
    ("Astragalus honey", "10.25"),
    ("Coriander honey", "10.25"),
    ("Mountain Javashir honey", "13.25"),
]


def signature_html():
    """The styled KIMIEL HTML signature block (read from app/outreach_signature.html)."""
    try:
        with open(_SIG_PATH, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def signature_text():
    """Plain-text signature (OUTREACH_SIGNATURE override in .env, else the KIMIEL default)."""
    return OUTREACH_SIGNATURE or _SIGNATURE_TEXT


def _valid_until(days=7):
    return (date.today() + timedelta(days=days)).strftime("%d %B %Y")


def honey_message(lead, valid_until=""):
    """(subject, body) for a HONEY buyer — KIMIEL wholesale / private-label first-touch, per the
    founder's template. Body has NO signature (appended at send). Prices are the founder's delivered
    CPT list; the destination city/country are filled from the lead."""
    company = (lead.buyer_company or "").strip()
    greet = f"Dear {company} Purchasing Team," if company else "Dear Sir/Madam,"
    country = COUNTRY.get(lead.dest_country, "")          # e.g. "the UAE" (reads well mid-sentence)
    market = country or "your market"
    city = (lead.dest_city or "").strip()
    if "(" in city or len(city) > 24:                     # drop messy/overlong city fields
        city = ""
    place = city or country.replace("the ", "") or "your destination"   # CPT names a clean place
    vu = valid_until or _valid_until()
    subject = "KIMIEL - honey supply & private-label offer" + (f" ({country})" if country else "")
    prices = [f"- {name} - USD {price}/kg" for name, price in HONEY_VARIETIES]
    lines = [
        greet, "",
        "I am contacting you on behalf of KIMIEL, a Dubai-based supplier of laboratory-tested honey "
        "sourced from our own hives and trusted independent beekeepers.", "",
        f"Considering your established food-distribution network in {market}, we would be pleased to "
        "explore a wholesale or private-label supply relationship.", "",
        f"Please find below our budgetary commercial quotation for a 500 kg order delivered CPT {place}:",
        "", *prices, "",
        "The proposed packaging is 20 food-grade bulk containers of 25 kg each. Batch-specific "
        "laboratory documentation, product-origin information, a commercial invoice and a packing list "
        "will be provided.", "",
        "Please confirm:",
        "- Your preferred honey variety",
        "- Exact delivery address",
        "- Legal company and consignee details",
        "- Required product or import documentation",
        "- Whether you are interested in bulk, retail-ready or private-label supply", "",
        f"This quotation is valid through {vu} and remains subject to final batch availability and "
        "written freight confirmation.", "",
        "We look forward to discussing a potential cooperation.",
    ]
    return subject, "\n".join(lines)


def default_message(lead, quote=None, product=None):
    """A ready-to-edit (subject, body) for a first outreach to a non-honey buyer. No signature (appended
    at send)."""
    name = (lead.contact_name or "").strip()
    greet = f"Hello {name.split()[0]}," if name else "Hello,"
    want = lead.product or (product.name if product else "your requirement")
    subject = f"Re: {want}"
    if quote is not None:
        subject += f" — quote {quote.tracking_code}"
    lines = [greet, "",
             f"Thank you for your interest in {want}. We source directly and handle the "
             "full trade — freight, customs, and delivery."]
    if quote is not None and product is not None:
        lines += ["", f"Indicative offer for {product.name}: "
                  f"{quote.delivered_unit} {quote.quote_currency}/{product.unit} delivered "
                  f"({quote.incoterm}), for {quote.quantity} {product.unit}."]
    if quote is not None and (getattr(quote, "share_token", "") or "").strip():
        lines += ["", f"View your quotation: {BASE_URL}/p/{quote.share_token}"]
    lines += ["", "Happy to share full specifications and terms. What quantity and destination "
              "are you targeting?"]
    return subject, "\n".join(lines)


def _esc(s):
    return (str(s or "")).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def body_to_html(body):
    """Convert a plain-text outreach body into simple, email-safe HTML (paragraphs + bullet lists)."""
    blocks = body.split("\n\n")
    out = []
    for blk in blocks:
        rows = [r for r in blk.split("\n") if r.strip()]
        if rows and all(r.lstrip().startswith("- ") for r in rows):
            items = "".join(f'<li style="margin:2px 0;">{_esc(r.lstrip()[2:])}</li>' for r in rows)
            out.append(f'<ul style="margin:10px 0;padding-left:20px;">{items}</ul>')
        elif rows:
            out.append('<p style="margin:0 0 12px;">' + "<br>".join(_esc(r) for r in rows) + "</p>")
    return ('<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.55;'
            f'color:#15130f;max-width:620px;">{"".join(out)}</div>')


def build_parts(body):
    """Return (text, html) for sending: plain body + text signature, and HTML body + styled signature."""
    text = f"{body}\n\n{signature_text()}"
    html = (f'<html><body style="margin:0;padding:0;background:#ffffff;">{body_to_html(body)}'
            f'<p style="font-family:Arial,sans-serif;font-size:14px;color:#15130f;margin:0 0 4px;">'
            f'Best regards,</p>{signature_html()}</body></html>')
    return text, html


def send_email(to_addr, subject, body, html=None):
    """Send via SMTP. Returns (ok, error, message_id) — never raises. When `html` is given, sends a
    multipart/alternative (plain `body` + `html`); otherwise a plain-text message."""
    if not SMTP_ENABLED:
        return False, "SMTP not configured", ""
    if not (to_addr or "").strip():
        return False, "no recipient email", ""
    try:
        domain = SMTP_FROM.split("@")[-1] if "@" in (SMTP_FROM or "") else "go4it.local"
        mid = make_msgid(domain=domain)
        msg = EmailMessage()
        msg["Message-ID"] = mid
        msg["From"] = f"KIMIEL <{SMTP_FROM}>" if SMTP_FROM else SMTP_FROM
        msg["To"] = to_addr
        msg["Subject"] = subject or "(no subject)"
        msg.set_content(body or "")
        if html:
            msg.add_alternative(html, subtype="html")
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as s:
            s.starttls(context=ssl.create_default_context())
            s.login(SMTP_USER, SMTP_PASSWORD)
            s.send_message(msg)
        return True, "", mid
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:300], ""
