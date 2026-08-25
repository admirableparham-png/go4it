"""AI Command (Phase 9) — additive, idempotent, CONSERVATIVE backfill.

    ./.venv/bin/python scripts/backfill_command_history.py --dry-run
    ./.venv/bin/python scripts/backfill_command_history.py
    ./.venv/bin/python scripts/backfill_command_history.py --rollback   # PRE-GO-LIVE (imported only)
    ./.venv/bin/python scripts/backfill_command_history.py --recover    # POST-GO-LIVE (untouched imported only)

RUN ORDER (prod): backup_db.py -> migrate.py -> migrate_gate_p9.py -> THIS.

What it does — strictly additive, conservative. Each existing CommandJob becomes ONE archived, IMPORTED
AIConversation with a single historical message (the prompt + result note, ENCRYPTED). It NEVER re-executes a
historical command, NEVER generates AI actions/citations from old commands, NEVER starts Research, NEVER creates
automation rules, and NEVER calls a provider. Operational counts (leads/quotes/deals/requests/outreach/products)
never change.
"""
import os
import sys
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from sqlmodel import Session, func, select   # noqa: E402

from app import ai_encryption as ENC   # noqa: E402
from app.db import engine, init_db   # noqa: E402
from app.models import (AIConversation, AIMessage, CommandJob, Deal, Lead, Outreach, Product, ServiceRequest,
                        Quote)   # noqa: E402

_OPERATIONAL = ("leads", "quotes", "deals", "requests", "outreach", "products")


def _counts(s) -> dict:
    c = lambda m: s.exec(select(func.count()).select_from(m)).one()   # noqa: E731
    return {"leads": c(Lead), "quotes": c(Quote), "deals": c(Deal), "requests": c(ServiceRequest),
            "outreach": c(Outreach), "products": c(Product), "conversations": c(AIConversation),
            "messages": c(AIMessage)}


def _apply(s) -> dict:
    now = datetime.utcnow()
    made = 0
    for job in s.exec(select(CommandJob)).all():
        # idempotent: one imported conversation per CommandJob (dedup on the title marker)
        marker = f"[imported cmd #{job.id}]"
        if s.exec(select(AIConversation).where(AIConversation.title == marker)).first():
            continue
        conv = AIConversation(owner_id=job.owner_id, title=marker, status="archived", imported=True,
                              archived_at=now, created_at=job.created_at or now, updated_at=now)
        s.add(conv)
        s.flush()
        # a single historical message — the original prompt + its result note, secret-redacted + encrypted.
        body = ENC.redact_secrets(f"{job.prompt}\n\n{job.note}".strip())
        s.add(AIMessage(conversation_id=conv.id, role="user", content_enc=ENC.ai_encrypt(body),
                        status="complete", provider="", model="", created_at=job.created_at or now,
                        completed_at=job.finished_at or now))
        made += 1
    return {"conversations_created": made}


def migrate(dry=False):
    init_db()
    with Session(engine) as s:
        pre = _counts(s)
        print("PRE :", pre)
        if dry:
            sp = s.begin_nested()
            res = _apply(s)
            post = _counts(s)
            sp.rollback()
            print(f"[dry-run] would create: {res}")
            print("POST:", post, "(rolled back)")
            return
        try:
            res = _apply(s)
            post = _counts(s)
            for k in _OPERATIONAL:
                if pre[k] != post[k]:
                    raise RuntimeError(f"operational count changed for {k}: {pre[k]} -> {post[k]}")
            s.commit()
            print(f"OK — {res}")
            print("POST:", post)
        except Exception:
            s.rollback()
            print("ERROR — rolled back, no partial backfill applied")
            raise


def _untouched(s, conv) -> bool:
    """An imported archive is 'untouched' if it still has ONLY its single imported message (no real activity)."""
    n = s.exec(select(func.count()).select_from(AIMessage).where(
        AIMessage.conversation_id == conv.id)).one()
    return conv.imported and n <= 1


def rollback():
    """PRE-GO-LIVE: delete the imported archives (+ their single messages). Real conversations untouched."""
    init_db()
    with Session(engine) as s:
        convs = s.exec(select(AIConversation).where(AIConversation.imported == True)).all()  # noqa: E712
        n = 0
        for conv in convs:
            for m in s.exec(select(AIMessage).where(AIMessage.conversation_id == conv.id)).all():
                s.delete(m)
            s.delete(conv); n += 1
        s.commit()
        print(f"PRE-GO-LIVE rollback: removed {n} imported command-history archive(s)")


def recover():
    """POST-GO-LIVE: remove only UNTOUCHED imported archives; preserve every real conversation, approval,
    action and automation record."""
    init_db()
    with Session(engine) as s:
        removed = 0
        for conv in s.exec(select(AIConversation).where(AIConversation.imported == True)).all():  # noqa: E712
            if _untouched(s, conv):
                for m in s.exec(select(AIMessage).where(AIMessage.conversation_id == conv.id)).all():
                    s.delete(m)
                s.delete(conv); removed += 1
        s.commit()
        print(f"POST-GO-LIVE recovery: removed {removed} untouched imported archive(s); all real "
              f"conversations, approvals, actions and automation preserved")


if __name__ == "__main__":
    if "--rollback" in sys.argv:
        rollback()
    elif "--recover" in sys.argv:
        recover()
    else:
        migrate(dry="--dry-run" in sys.argv)
