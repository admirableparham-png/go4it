"""Buyer outreach: build a first-touch message from a lead (+ its latest quote) and send it via
SMTP when configured. When SMTP is off, the routes fall back to click-to-email (mailto) and
click-to-WhatsApp links and record the attempt as 'logged'. send_email never raises.
"""
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import make_msgid

from .config import (BASE_URL, SMTP_ENABLED, SMTP_FROM, SMTP_HOST, SMTP_PASSWORD,
                     SMTP_PORT, SMTP_USER)


def default_message(lead, quote=None, product=None):
    """A ready-to-edit (subject, body) for a first outreach to this buyer."""
    name = (lead.contact_name or lead.buyer_company or "").strip()
    who = name.split()[0] if name else "there"
    want = lead.product or (product.name if product else "your requirement")
    subject = f"Re: {want}"
    if quote is not None:
        subject += f" — quote {quote.tracking_code}"
    lines = [f"Hello {who},", "",
             f"Thank you for your interest in {want}. We source directly and handle the "
             "full trade — freight, customs, and delivery."]
    if quote is not None and product is not None:
        lines += ["", f"Indicative offer for {product.name}: "
                  f"{quote.delivered_unit} {quote.quote_currency}/{product.unit} delivered "
                  f"({quote.incoterm}), for {quote.quantity} {product.unit}."]
    # Only approved/sent quotes carry a share_token — link straight to the buyer-safe pro-forma.
    if quote is not None and (getattr(quote, "share_token", "") or "").strip():
        lines += ["", f"View your quotation: {BASE_URL}/p/{quote.share_token}"]
    lines += ["", "Happy to share full specifications and terms. What quantity and destination "
              "are you targeting?", "", "Best regards,", "go4it"]
    return subject, "\n".join(lines)


def honey_first_touch(lead, delivered_unit=None, dest_name="", trial_kg=25):
    """A tailored first-touch (subject, body) for a HONEY buyer lead — leads with the offer, the
    indicative delivered price to their market, and the bankable payment terms (the real differentiator)."""
    name = (lead.contact_name or lead.buyer_company or "").strip()
    who = name.split()[0] if name else "there"
    subject = "Iranian natural honey — direct export offer (single-origin, Certificate of Origin)"
    price = ""
    if delivered_unit:
        where = f" to {dest_name}" if dest_name else ""
        price = (f" Indicative delivered price{where}: about ${delivered_unit:.2f}/kg CPT on a 500 kg "
                 f"order (EXW $8.00/kg).")
    lines = [
        f"Hello {who},", "",
        "We are a direct exporter of genuine single-origin Iranian natural honey — multifloral, thyme, "
        "Sidr, honeydew, and honey with royal jelly — supplied in 25 kg food-grade drums, with "
        "Certificate of Origin and laboratory specifications (moisture / HMF / antibiotic-free) available "
        "on request.", "",
        f"EXW ex-works Iran is $8.00/kg.{price} Payment by Letter of Credit, SWIFT bank transfer, or "
        f"cryptocurrency. A {trial_kg} kg trial order is available so you can verify the quality first.", "",
        "Which grade and quantity suit you, and what is your delivery destination? I'll send full "
        "specifications and can arrange a sample.", "",
        "Best regards,", "go4it",
    ]
    return subject, "\n".join(lines)


def send_email(to_addr, subject, body):
    """Send via SMTP. Returns (ok: bool, error: str, message_id: str) — never raises.

    The Message-ID is stamped on the sent mail and returned so the outbound row can store it; a
    buyer reply's In-Reply-To then threads back to this message. message_id is "" when not sent."""
    if not SMTP_ENABLED:
        return False, "SMTP not configured", ""
    if not (to_addr or "").strip():
        return False, "no recipient email", ""
    try:
        domain = SMTP_FROM.split("@")[-1] if "@" in (SMTP_FROM or "") else "go4it.local"
        mid = make_msgid(domain=domain)
        msg = EmailMessage()
        msg["Message-ID"] = mid
        msg["From"] = SMTP_FROM
        msg["To"] = to_addr
        msg["Subject"] = subject or "(no subject)"
        msg.set_content(body or "")
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as s:
            s.starttls(context=ssl.create_default_context())
            s.login(SMTP_USER, SMTP_PASSWORD)
            s.send_message(msg)
        return True, "", mid
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:300], ""
