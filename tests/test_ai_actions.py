"""Phase 9 (B) — action proposals + approval matrix + safe idempotent execution: nonce, double-approval no-op,
stale payload-hash rejection, forbidden actions refused, structured (not free-text) execution, expiry."""
from datetime import datetime, timedelta

from sqlmodel import Session, select

from app import ai_actions as ACT
from app import ai_command as CMD
from app.models import AIActionProposal, User, WorkItem


def _admin(s):
    return s.exec(select(User).where(User.email == "admin@t.local")).one()


def _conv(s, u):
    c = CMD.new_conversation(s, u); s.commit()
    return c


def test_forbidden_actions_never_proposed(ops_engine):
    with Session(ops_engine) as s:
        u = _admin(s); c = _conv(s, u)
        for act in ("confirm_payment", "initiate_remittance", "accept_quote_as_buyer", "sign_contract",
                    "bypass_suppression", "change_user_role", "send_email", "advance_deal"):
            p, err = ACT.propose(s, c, action_type=act, payload={}, actor=u)
            assert p is None and ("never executable" in err or "unknown" in err), act


def test_approval_requires_nonce_and_is_idempotent(ops_engine):
    with Session(ops_engine) as s:
        u = _admin(s); c = _conv(s, u)
        p, _ = ACT.propose(s, c, action_type="create_work_item",
                           payload={"type": "admin_action_required", "title": "Follow up"}, actor=u); s.commit()
        assert p.status == "proposed" and p.approval_nonce
        # wrong nonce is rejected (CSRF/replay guard)
        assert ACT.approve(s, p, nonce="wrong", actor=u) == (False, "invalid approval token")
        ok, res = ACT.approve(s, p, nonce=p.approval_nonce, actor=u); s.commit()
        assert ok and res["work_item_created"]
        # double-click / replay is a no-op (executed once)
        ok2, res2 = ACT.approve(s, p, nonce=p.approval_nonce, actor=u)
        assert ok2 and res2 == "already executed"
        # the ACTION executed exactly once (the awaiting-review task is a separate item)
        assert len(s.exec(select(WorkItem).where(WorkItem.related_proposal_id == p.id,
                                                 WorkItem.type == "admin_action_required")).all()) == 1


def test_stale_payload_hash_rejected(ops_engine):
    with Session(ops_engine) as s:
        u = _admin(s); c = _conv(s, u)
        p, _ = ACT.propose(s, c, action_type="assign_owner",
                           payload={"opportunity_id": 1, "owner_id": 1}, actor=u); s.commit()
        p.payload = '{"opportunity_id": 999, "owner_id": 1}'   # tamper the stored payload
        s.add(p); s.commit()
        ok, err = ACT.approve(s, p, nonce=p.approval_nonce, actor=u)
        assert ok is False and "stale" in err                  # hash mismatch → rejected


def test_expired_proposal_cannot_execute(ops_engine):
    with Session(ops_engine) as s:
        u = _admin(s); c = _conv(s, u)
        p, _ = ACT.propose(s, c, action_type="create_work_item", payload={"title": "x"}, actor=u); s.commit()
        p.expires_at = datetime.utcnow() - timedelta(minutes=1); s.add(p); s.commit()
        ok, err = ACT.approve(s, p, nonce=p.approval_nonce, actor=u)
        assert ok is False and "expired" in err


def test_seller_only_admin_and_target_revalidated(ops_engine):
    with Session(ops_engine) as s:
        u = _admin(s); c = _conv(s, u)
        seller = s.exec(select(User).where(User.email == "sellerA@t.local")).one()
        p, _ = ACT.propose(s, c, action_type="create_work_item", payload={"title": "x"}, actor=u); s.commit()
        assert ACT.approve(s, p, nonce=p.approval_nonce, actor=seller) == (False, "admin only")
        # assign to a non-existent opportunity → stale target rejected, marked failed
        p2, _ = ACT.propose(s, c, action_type="assign_owner", payload={"opportunity_id": 12345, "owner_id": 1},
                            actor=u); s.commit()
        ok, err = ACT.approve(s, p2, nonce=p2.approval_nonce, actor=u)
        assert ok is False and "target state" in err


def test_research_proposal_queues_but_does_not_run(ops_engine):
    with Session(ops_engine) as s:
        u = _admin(s); c = _conv(s, u)
        p, _ = ACT.propose(s, c, action_type="start_research",
                           payload={"prompt": "honey buyers in Georgia"}, actor=u); s.commit()
        ok, res = ACT.approve(s, p, nonce=p.approval_nonce, actor=u); s.commit()
        # a CommandJob is created via the existing pipeline but left QUEUED (never auto-run here)
        from app.models import CommandJob
        job = s.exec(select(CommandJob)).one()
        assert ok and res["command_job_id"] == job.id and job.status == "queued"
