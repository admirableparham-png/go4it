"""Shared test fixtures.

Force outreach SMTP OFF and Telegram OFF during the whole test suite. Once a real Gmail/SMTP and a real
Telegram bot token are configured in .env, those side effects would fire against the real world from the
test process too — tests use fixture data like "ACME" / buyer@acme.com, so an unstubbed notify path would
send REAL Telegram alerts to the founder's phone (and send_email would try to deliver real mail). These
autouse fixtures keep the suite hermetic regardless of .env.
"""
import pytest


@pytest.fixture(autouse=True)
def _disable_outreach_smtp(monkeypatch):
    import app.outreach
    monkeypatch.setattr(app.outreach, "SMTP_ENABLED", False, raising=False)


@pytest.fixture(autouse=True)
def _disable_telegram(monkeypatch):
    """Neutralize the Telegram bot in tests. send_message() (the single choke point every notify_*
    routes through) is a no-op when the token is blank — so blanking it here kills ALL alerts, however
    they are called, without the test caring which notify helper fired."""
    import app.telegram
    monkeypatch.setattr(app.telegram, "TELEGRAM_BOT_TOKEN", "", raising=False)
    monkeypatch.setattr(app.telegram, "TELEGRAM_CHAT_ID", "", raising=False)
