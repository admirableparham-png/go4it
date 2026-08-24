"""Phase 6 (A) — quote workflow + immutable versions.

Central transitions (invalid rejected server-side), consistent expiry, approval invalidated after a revision,
and immutable version snapshots (future product/price changes never alter a historical version).
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app import quote_service as QS, quote_workflow as QW
from app.models import Lead, Product, Quote, QuoteStatusEvent, QuoteVersion, User


@pytest.fixture
def ctx():
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(e)
    with Session(e) as s:
        s.add(User(email="admin@t.local", name="A", role="admin", active=True, password_hash="x")); s.commit()
        ld = Lead(product="x", tracking_code="G4-1", dest_country="GE"); s.add(ld); s.commit(); s.refresh(ld)
        p = Product(name="Copper", exw_price=8000, weight_kg_per_unit=1000, min_order_qty=25, currency="USD",
                    unit="tonne", origin_country="IR"); s.add(p); s.commit(); s.refresh(p)
        q = QS.create_quote(s, ld, p); s.commit()
        QS.ensure_version(s, q); s.commit()
        return e, q.id, p.id


def _q(s, qid):
    return s.get(Quote, qid)


def test_valid_and_invalid_transitions(ctx):
    e, qid, _ = ctx
    with Session(e) as s:
        q = _q(s, qid)
        assert QW.transition(s, q, "sent")[0] is False        # draft → sent rejected (not approved)
        assert QW.transition(s, q, "accepted")[0] is False     # draft → accepted rejected
        assert QW.transition(s, q, "approved")[0] is True
        assert QW.transition(s, q, "sent")[0] is True          # approved → sent ok
        assert QW.transition(s, q, "draft")[0] is False        # sent → draft rejected
        s.commit()
        assert len(s.exec(select(QuoteStatusEvent)).all()) == 2   # only the two valid moves recorded


def test_only_current_unexpired_accepts(ctx):
    e, qid, _ = ctx
    with Session(e) as s:
        q = _q(s, qid); QW.transition(s, q, "approved"); QW.transition(s, q, "sent"); s.commit()
        assert QW.can_accept(q) is True
        q.created_at = datetime.utcnow() - timedelta(days=40); q.validity_days = 14; s.add(q); s.commit()
        assert QW.is_expired(q) is True and QW.can_accept(q) is False
        assert QW.transition(s, q, "accepted")[0] is False     # accepting expired fails safely


def test_mark_expired_never_active(ctx):
    e, qid, _ = ctx
    with Session(e) as s:
        q = _q(s, qid); QW.transition(s, q, "approved"); QW.transition(s, q, "sent")
        q.created_at = datetime.utcnow() - timedelta(days=40); q.validity_days = 14; s.add(q); s.commit()
        assert QW.mark_expired_if_due(s, q) is True and q.status == "expired"
        assert q.status not in QW.PRESENTABLE                  # never presented active


def test_version_is_immutable_snapshot(ctx):
    e, qid, pid = ctx
    with Session(e) as s:
        q = _q(s, qid); ver = s.get(QuoteVersion, q.current_version_id)
        frozen_total, frozen_hash = ver.total, ver.content_hash
        assert ver.margin_pct != ver.markup_pct                # gross margin vs markup are distinct numbers
        # change the product price AFTER the version exists → the version must not move
        p = s.get(Product, pid); p.exw_price = 99999; s.add(p); s.commit()
    with Session(e) as s:
        ver = s.exec(select(QuoteVersion)).first()
        assert ver.total == frozen_total and ver.content_hash == frozen_hash


def test_revise_creates_new_draft_not_edit(ctx):
    e, qid, _ = ctx
    with Session(e) as s:
        q = _q(s, qid); QW.transition(s, q, "approved"); s.commit()
        dup = QS.revise_quote(s, q, actor=None); s.commit()
        assert dup.id != qid and dup.status == "draft" and dup.version == 2
        # the original approved quote + its version are untouched
        assert s.get(Quote, qid).status == "approved"
        v2 = s.get(QuoteVersion, dup.current_version_id)
        assert v2.supersedes_id == q.current_version_id


def test_ensure_version_idempotent(ctx):
    e, qid, _ = ctx
    with Session(e) as s:
        q = _q(s, qid)
        a = QS.ensure_version(s, q); b = QS.ensure_version(s, q); s.commit()
        assert a.id == b.id
        assert len(s.exec(select(QuoteVersion).where(QuoteVersion.quote_id == qid)).all()) == 1
