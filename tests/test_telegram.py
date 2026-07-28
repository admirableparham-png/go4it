"""Regression tests for Telegram alerting — the silent-drop / injection fix."""
import app.telegram as tg
from app.models import Lead, Product


def test_esc_escapes_html():
    assert tg._esc("<a href='x'>&") == "&lt;a href=&#x27;x&#x27;&gt;&amp;"
    assert tg._esc(None) == ""


def test_notify_lead_matches_escapes_user_fields(monkeypatch):
    """Ordinary data with <, >, & must be escaped so Telegram doesn't 400 and
    silently drop the alert, while the template's own tags survive."""
    captured = {}

    def fake_send(text, _plain=False):
        captured["text"] = text
        return True

    monkeypatch.setattr(tg, "send_message", fake_send)

    lead = Lead(product="Steel <grade 60> rebar", quantity=100, unit="ton",
                target_price=700, currency="USD", dest_country="GE",
                buyer_company="AT&T Georgia", contact_name="<G. B>",
                email="b@x.example", tracking_code="G4-202607-0001")
    product = Product(name="Rebar & wire", exw_price=590, currency="USD", unit="ton")

    tg.notify_lead_matches(lead, [(product, 92.0, "text 95%, on GE corridor")])
    text = captured["text"]

    assert "<grade 60>" not in text and "&lt;grade 60&gt;" in text
    assert "AT&T" not in text and "AT&amp;T" in text
    assert "Rebar & wire" not in text and "Rebar &amp; wire" in text
    assert "<b>" in text  # hard-coded formatting tags intact
