"""Regression tests for Telegram alerting — the silent-drop / injection fix."""
import app.telegram as tg
from app.models import Demand, Offer


def test_esc_escapes_html():
    assert tg._esc("<a href='x'>&") == "&lt;a href=&#x27;x&#x27;&gt;&amp;"
    assert tg._esc(None) == ""


def test_notify_match_escapes_user_fields(monkeypatch):
    """Ordinary data with <, >, & must be escaped (so Telegram doesn't 400 and
    silently drop the alert), while the hard-coded formatting tags survive."""
    captured = {}

    def fake_send(text, _plain=False):
        captured["text"] = text
        return True

    monkeypatch.setattr(tg, "send_message", fake_send)

    d = Demand(product="Steel <grade 60> rebar", category="metals", quantity=10,
               unit="ton", target_price=650, currency="USD", location="Tbilisi",
               contact="<jane@acme.com>")
    o = Offer(product="Rebar & wire", category="metals", quantity=20, unit="ton",
              price=600, currency="USD", location="Tabriz", contact="AT&T")

    tg.notify_match(d, o, 88.0, "text 90%")
    text = captured["text"]

    # User-supplied angle brackets / ampersands are neutralized...
    assert "<grade 60>" not in text and "&lt;grade 60&gt;" in text
    assert "<jane@acme.com>" not in text
    assert "AT&T" not in text and "AT&amp;T" in text
    # ...but the template's own formatting tags remain.
    assert "<b>" in text
