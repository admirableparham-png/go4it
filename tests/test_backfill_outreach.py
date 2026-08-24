"""Phase 4 hardening — legacy outreach migration semantics.

Legacy Lead.source groups become clearly-labelled INFERRED LEGACY OUTREACH GROUPS (never "campaigns"), stay
paused, and never auto-enroll/auto-send. Historical outbound Outreach is linked to recipients ONLY when the
association is deterministic (a lead's source maps 1:1 to a group); orphan rows (no lead) are counted and
raise ONE aggregate review task, never guessed. Every Outreach row + timestamp is preserved. Idempotent;
rollback restores.
"""
import pytest
from sqlalchemy import text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import scripts.backfill_outreach as BF
from app.models import Campaign, CampaignRecipient, Lead, Outreach, WorkItem


def _indexes(engine):
    with engine.connect() as conn:
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_workitem_idem_open ON workitem(idempotency_key) "
                          "WHERE idempotency_key != '' AND status IN ('open','in_progress','waiting')"))
        conn.commit()


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    _indexes(engine)
    monkeypatch.setattr(BF, "engine", engine)
    monkeypatch.setattr(BF, "init_db", lambda: None)
    monkeypatch.setattr("app.suppression.engine", engine, raising=False)
    return engine


def _seed(engine):
    """Two source groups + emailed/replied/bounced leads + one ORPHAN outbound row (no lead)."""
    with Session(engine) as s:
        # group 'honey': one emailed lead, one replied lead
        h1 = Lead(product="honey", source="iran-honey", email="a@x.com", managed=True); s.add(h1)
        h2 = Lead(product="honey", source="iran-honey", email="b@x.com", buyer_replied_at=None,
                  managed=True); s.add(h2)
        # group 'saffron': one bounced lead + one lead that was NEVER emailed (must NOT become a recipient)
        s1 = Lead(product="saffron", source="iran-saffron", email="c@x.com", next_action_note="bounced",
                  managed=True); s.add(s1)
        s2 = Lead(product="saffron", source="iran-saffron", email="never@x.com", managed=True); s.add(s2)
        # a lead with NO source → outbound row is 'unlinked' (lead exists, maps to no group)
        n1 = Lead(product="misc", source="", email="d@x.com", managed=True); s.add(n1)
        s.commit()
        for L in (h1, h2, s1, n1):
            s.refresh(L)
        s.add(Outreach(lead_id=h1.id, direction="out", recipient="a@x.com", status="sent"))
        s.add(Outreach(lead_id=h1.id, direction="out", recipient="a@x.com", status="sent"))  # 2nd send, same lead
        s.add(Outreach(lead_id=h2.id, direction="out", recipient="b@x.com", status="sent"))
        s.add(Outreach(lead_id=s1.id, direction="out", recipient="c@x.com", status="failed"))
        s.add(Outreach(lead_id=n1.id, direction="out", recipient="d@x.com", status="sent"))   # unlinked
        s.add(Outreach(lead_id=0, direction="out", recipient="ghost@x.com", status="sent"))    # ORPHAN → ambiguous
        s.commit()
        h2.buyer_replied_at = __import__("datetime").datetime.utcnow(); s.add(h2); s.commit()


def test_legacy_groups_labelled_and_paused(db):
    _seed(db)
    BF.migrate(dry=False)
    with Session(db) as s:
        groups = s.exec(select(Campaign).where(Campaign.inferred == True)).all()   # noqa: E712
        assert len(groups) == 2
        for g in groups:
            assert g.name.startswith("[legacy group]")           # NOT called a campaign
            assert g.context_kind == "legacy_import"
            assert g.status in ("paused", "completed")            # never running/auto-sending
            assert g.inferred is True and "legacy" in g.notes.lower()


def test_deterministic_recipient_linking_reconciles_the_gap(db):
    _seed(db)
    BF.migrate(dry=False)
    with Session(db) as s:
        gid = {g.source_slug: g.id for g in s.exec(select(Campaign)).all()}
        # honey: 2 emailed leads → exactly 2 recipients (the double-send on h1 does NOT duplicate)
        honey = s.exec(select(CampaignRecipient).where(CampaignRecipient.campaign_id == gid["iran-honey"])).all()
        assert len(honey) == 2
        # saffron: only the bounced lead was emailed → 1 recipient; the never-emailed lead is NOT a recipient
        saff = s.exec(select(CampaignRecipient).where(CampaignRecipient.campaign_id == gid["iran-saffron"])).all()
        assert len(saff) == 1 and saff[0].status == "hard_bounced"
        emails = {r.to_email for r in honey}
        assert "a@x.com" in emails and "b@x.com" in emails and "never@x.com" not in emails
        # a replied lead is recorded as replied; all recipients are inferred provenance
        assert any(r.status == "replied" for r in honey)
        assert all(r.inferred for r in honey + saff)


def test_ambiguous_orphan_row_raises_review_task_not_guessed(db):
    _seed(db)
    BF.migrate(dry=False)
    with Session(db) as s:
        # the orphan outbound row (lead_id=0) produced NO recipient anywhere...
        total_rcpt = s.exec(select(CampaignRecipient)).all()
        assert len(total_rcpt) == 3                               # honey(2) + saffron(1), never the orphan
        # ...but a single aggregate review task exists
        task = s.exec(select(WorkItem).where(WorkItem.idempotency_key == BF.UNMATCHED_KEY)).first()
        assert task is not None and "review" in task.title.lower()


def test_all_outreach_rows_preserved(db):
    _seed(db)
    before = _outreach_snapshot(db)
    BF.migrate(dry=False)
    assert _outreach_snapshot(db) == before                      # every row + timestamp untouched


def _outreach_snapshot(engine):
    with Session(engine) as s:
        return sorted((o.id, o.lead_id, o.recipient, o.created_at.isoformat())
                      for o in s.exec(select(Outreach)).all())


def test_idempotent_rerun_creates_no_duplicates(db):
    _seed(db)
    BF.migrate(dry=False)
    with Session(db) as s:
        n_camp = len(s.exec(select(Campaign)).all())
        n_rcpt = len(s.exec(select(CampaignRecipient)).all())
        n_task = len(s.exec(select(WorkItem).where(WorkItem.idempotency_key == BF.UNMATCHED_KEY)).all())
    BF.migrate(dry=False)                                        # run again
    with Session(db) as s:
        assert len(s.exec(select(Campaign)).all()) == n_camp
        assert len(s.exec(select(CampaignRecipient)).all()) == n_rcpt
        assert len(s.exec(select(WorkItem).where(WorkItem.idempotency_key == BF.UNMATCHED_KEY)).all()) == n_task


def test_rollback_restores_and_preserves_history(db):
    _seed(db)
    before = _outreach_snapshot(db)
    BF.migrate(dry=False)
    BF.rollback()
    with Session(db) as s:
        assert s.exec(select(Campaign).where(Campaign.inferred == True)).all() == []   # noqa: E712
        assert s.exec(select(CampaignRecipient)).all() == []
        assert s.exec(select(WorkItem).where(WorkItem.idempotency_key == BF.UNMATCHED_KEY)).all() == []
    assert _outreach_snapshot(db) == before                     # history intact after rollback
