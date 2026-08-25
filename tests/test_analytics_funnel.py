"""Phase 8 (A) — the commercial funnel, provenance/freshness classification, source health, and the snapshot
cache (tenant-scoped keys, stale fallback)."""
from datetime import datetime, timedelta

from sqlmodel import Session, select

from app import analytics as A
from app import data_sources as DS
from app import provenance_view as PV
from app.models import IngestionRun, Lead, Quote, User


def _seller(s):
    return s.exec(select(User).where(User.email == "sellerA@t.local")).one()


def test_funnel_excludes_negative_from_positive_interest(ops_engine):
    with Session(ops_engine) as s:
        o = _seller(s)
        now = datetime.utcnow()
        for i in range(6):
            s.add(Lead(product="x", tracking_code=f"P{i}", owner_id=o.id, email="a@b.com",
                       buyer_replied_at=now, reply_outcome="positive"))
        for i in range(4):
            s.add(Lead(product="x", tracking_code=f"N{i}", owner_id=o.id, email="a@b.com",
                       buyer_replied_at=now, reply_outcome="negative"))
        s.commit()
        f = A.funnel(s)
        stages = {r["key"]: r["count"] for r in f["stages"]}
        assert stages["human_reply"] == 10          # positive + negative are both human replies
        assert stages["positive_interest"] == 6     # negatives NEVER enter positive interest
        assert "cohort" in f["basis"].lower() or "message" in f["basis"].lower()


def test_funnel_conversion_insufficient_below_min_sample(ops_engine):
    with Session(ops_engine) as s:
        o = _seller(s)
        s.add(Lead(product="x", tracking_code="A", owner_id=o.id, email="a@b.com",
                   buyer_replied_at=datetime.utcnow(), reply_outcome="positive"))
        s.commit()
        f = A.funnel(s)
        # with only 1 lead, downstream conversions are flagged insufficient (prev stage below min sample)
        assert any(r["insufficient"] for r in f["stages"])


def test_provenance_classification_honest():
    assert PV.classify("inferred")["label"] == "Inferred"
    assert "never" in PV.classify("inferred")["detail"].lower()
    assert PV.classify("verified")["label"] == "Verified"
    # freshness thresholds
    now = datetime.utcnow()
    assert PV.freshness(now, 24, now=now) == "Current"
    assert PV.freshness(now - timedelta(hours=40), 24, now=now) == "Aging"
    assert PV.freshness(now - timedelta(hours=200), 24, now=now) == "Stale"
    assert PV.freshness(None, 24) == "Unknown"


def test_source_health_not_configured_vs_failed(ops_engine, monkeypatch):
    monkeypatch.delenv("IMAP_HOST", raising=False)
    with Session(ops_engine) as s:
        health = {h["key"]: h for h in DS.source_health(s)}
        assert health["email_inbound"]["freshness"] == "Not configured"   # no integration → honest label
        # once configured, a failed ingestion run reads "Failed" (never "Stale")
        monkeypatch.setenv("IMAP_HOST", "imap.example.com")
        s.add(IngestionRun(source="email-inbound", status="error", finished_at=datetime.utcnow(),
                           error="IMAP timeout"))
        s.commit()
        health2 = {h["key"]: h for h in DS.source_health(s)}
        assert health2["email_inbound"]["freshness"] == "Failed"


def test_snapshot_cache_is_tenant_scoped_and_falls_back(ops_engine):
    with Session(ops_engine) as s:
        o = _seller(s)
        k1 = A.cache_key("dashboard", tenant_id=None, time_range="30d")
        k2 = A.cache_key("dashboard", tenant_id=o.id, time_range="30d")
        assert k1 != k2 and "global" in k1 and str(o.id) in k2
        # first call computes + writes a snapshot; second within TTL is served cached
        r1 = A.cached(s, kind="dashboard", key=k1, compute_fn=lambda: {"n": 1}); s.commit()
        assert r1["cached"] is False and r1["result"] == {"n": 1}
        r2 = A.cached(s, kind="dashboard", key=k1, compute_fn=lambda: {"n": 2}); s.commit()
        assert r2["cached"] is True and r2["result"] == {"n": 1}   # served from snapshot, not recomputed
        # a compute failure falls back to the last snapshot, flagged stale
        def boom():
            raise RuntimeError("worker down")
        r3 = A.cached(s, kind="dashboard", key=k1, compute_fn=boom, ttl_minutes=0)
        assert r3["stale"] is True and r3["result"] == {"n": 1}
