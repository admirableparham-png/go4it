"""Tests for the buyer-outreach message builder + SMTP guard."""
from app.models import Lead, Product, Quote
from app.outreach import default_message, send_email


def test_default_message_basic():
    lead = Lead(product="Ceramic tiles", buyer_company="ACME Tiles", contact_name="Sara Ali")
    subject, body = default_message(lead)
    assert "Ceramic tiles" in subject
    assert "Sara" in body                 # greets by first name
    assert "Best regards" in body


def test_default_message_includes_quote_offer():
    lead = Lead(product="Ceramic tiles", buyer_company="ACME")
    product = Product(name="Ceramic tiles A-grade", unit="m2", exw_price=4.5, currency="USD")
    quote = Quote(lead_id=1, product_id=1, tracking_code="G4-Q1", quantity=1000,
                  incoterm="DAP", quote_currency="USD", delivered_unit=6.2, delivered_total=6200)
    subject, body = default_message(lead, quote, product)
    assert "G4-Q1" in subject
    assert "delivered" in body.lower() and "Ceramic tiles A-grade" in body


def test_default_message_includes_buyer_link_when_quote_shared():
    lead = Lead(product="Ceramic tiles", buyer_company="ACME")
    product = Product(name="Tiles", unit="m2", exw_price=4.5, currency="USD")
    shared = Quote(lead_id=1, product_id=1, tracking_code="G4-Q9", quantity=500, incoterm="DAP",
                   quote_currency="USD", delivered_unit=6.0, delivered_total=3000, share_token="abc123")
    _, body = default_message(lead, shared, product)
    assert "/p/abc123" in body                          # buyer link included
    draft = Quote(lead_id=1, product_id=1, tracking_code="G4-Q8", quantity=500, incoterm="DAP",
                  quote_currency="USD", delivered_unit=6.0, delivered_total=3000, share_token="")
    _, body2 = default_message(lead, draft, product)
    assert "/p/" not in body2                            # no link for an unshared (draft) quote


def test_send_email_disabled_returns_reason():
    ok, err = send_email("buyer@example.com", "Hi", "Body")
    assert ok is False and "not configured" in err.lower()   # SMTP off in tests
