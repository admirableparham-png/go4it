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


# --- Phase 7 (Operations) shared helpers ---------------------------------------------------------
# The partial-unique indexes init_db() would create — create_all() alone does not make them, so the
# operations tests build them by hand (mirrors the Phase-6 test fixtures).
_OPS_INDEXES = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_workitem_idem_open ON workitem(idempotency_key) "
    "WHERE idempotency_key != '' AND status IN ('open','in_progress','waiting')",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_shipmentevent_ext ON shipmentevent(source, external_event_id) "
    "WHERE external_event_id != ''",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_operationcase_reference ON operationcase(reference) "
    "WHERE reference != ''",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_opcase_deal_primary ON operationcase(deal_id) "
    "WHERE case_type = 'deal' AND deal_id IS NOT NULL",
    # Phase 8 intelligence guards
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_demandsignal_dedup ON demandsignal(dedup_key) WHERE dedup_key != ''",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_intelalert_key ON intelalert(alert_key, condition_version) "
    "WHERE alert_key != ''",
)


@pytest.fixture
def ops_engine():
    """An in-memory engine with the operations partial-unique indexes + a seeded admin and two sellers."""
    from sqlalchemy import text
    from sqlalchemy.pool import StaticPool
    from sqlmodel import Session, SQLModel, create_engine
    from app.auth import hash_password
    from app.models import User
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(e)
    with e.connect() as c:
        for ddl in _OPS_INDEXES:
            c.execute(text(ddl))
        c.commit()
    with Session(e) as s:
        s.add(User(email="admin@t.local", name="Admin", role="admin", active=True,
                   password_hash=hash_password("pw")))
        s.add(User(email="sellerA@t.local", name="Seller A", role="agent", active=True,
                   password_hash=hash_password("pw")))
        s.add(User(email="sellerB@t.local", name="Seller B", role="agent", active=True,
                   password_hash=hash_password("pw")))
        s.commit()
    return e


@pytest.fixture
def ops_users(ops_engine):
    """(admin, sellerA, sellerB) User rows for the ops_engine."""
    from sqlmodel import Session, select
    from app.models import User
    with Session(ops_engine) as s:
        admin = s.exec(select(User).where(User.email == "admin@t.local")).one()
        a = s.exec(select(User).where(User.email == "sellerA@t.local")).one()
        b = s.exec(select(User).where(User.email == "sellerB@t.local")).one()
        s.expunge_all()
    return admin, a, b
