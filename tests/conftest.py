"""Shared test fixtures.

Force outreach SMTP OFF during the whole test suite. Once a real Gmail/SMTP is configured in .env,
`SMTP_ENABLED` becomes True in the test process too — without this, tests that exercise send_email
would attempt to deliver real email to test addresses. This keeps tests hermetic regardless of .env.
"""
import pytest


@pytest.fixture(autouse=True)
def _disable_outreach_smtp(monkeypatch):
    import app.outreach
    monkeypatch.setattr(app.outreach, "SMTP_ENABLED", False, raising=False)
