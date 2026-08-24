"""Outreach, Campaigns & Email (Phase 4) — additive, idempotent backfill.

    ./.venv/bin/python scripts/backfill_outreach.py --dry-run   # map legacy statuses; change nothing
    ./.venv/bin/python scripts/backfill_outreach.py             # apply (transactional)
    ./.venv/bin/python scripts/backfill_outreach.py --rollback  # PRE-GO-LIVE revert
    ./.venv/bin/python scripts/backfill_outreach.py --recover   # POST-GO-LIVE safe cleanup (untouched only)

RUN ORDER (prod, sending DISABLED): backup_db.py -> migrate.py -> (prior backfills) -> THIS.

What it does — strictly additive, NO messages sent:
  * PRESERVES every existing Outreach event + Message-ID (never modified).
  * Maps each legacy campaign (a distinct Lead.source group) to a Campaign row (inferred=True), status derived
    from whether its leads are still active; links source_slug for the bridge.
  * Populates SUPPRESSION from existing hard evidence — leads whose next_action_note=='bounced' and 'out'
    Outreach rows marked failed — reason hard_bounce (source_event 'backfill:*' so rollback can find them).
  * Records a BounceRecord per bounced address. Automatic replies are NEVER converted to engaged buyers.
Inferred/backfill-tagged rows are reversible; operational counts (leads/outreach/requests) never change.
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from sqlmodel import Session, select, func   # noqa: E402

from app.db import engine, init_db   # noqa: E402
from app import suppression as SUP   # noqa: E402
from app import work_queue as WQ   # noqa: E402
from app.models import (BounceRecord, Campaign, CampaignRecipient, Lead, Outreach, ServiceRequest,
                        Suppression, WorkItem)   # noqa: E402

BF = "backfill:outreach"
UNMATCHED_KEY = "legacy_unmatched_outreach"   # single aggregate review task for un-attributable rows


def _counts(s) -> dict:
    c = lambda q: s.exec(q).one()   # noqa: E731
    return {"leads": c(select(func.count(Lead.id))), "outreach": c(select(func.count(Outreach.id))),
            "requests": c(select(func.count(ServiceRequest.id))),
            "campaigns": c(select(func.count(Campaign.id))),
            "recipients": c(select(func.count(CampaignRecipient.id))),
            "suppression": c(select(func.count(Suppression.id))),
            "bounces": c(select(func.count(BounceRecord.id)))}


_OPERATIONAL = ("leads", "outreach", "requests")   # must be invariant across the backfill


def _link_recipients(s) -> dict:
    """Deterministically bridge historical outbound Outreach → CampaignRecipient. A lead's `source` maps 1:1
    to an inferred legacy group, so a lead that was actually emailed becomes a recipient of exactly its group —
    no guessing. This RECONCILES the '197 historical Outreach vs 0 recipients' gap: recipients were never
    created by the shell backfill; here we create one per (group, emailed lead) with a status derived from the
    lead's real state. Rows we can't deterministically attribute (no lead, or lead with no source) are NOT
    guessed: they are counted and, if any, raise ONE aggregate admin review task. Idempotent (dedup on
    (campaign, lead)); preserves every Outreach row + timestamp untouched. Returns the partition of outbound
    rows into linked / unlinked / ambiguous plus how many recipients were created."""
    groups = {c.source_slug: c for c in s.exec(select(Campaign).where(Campaign.inferred == True)).all()  # noqa: E712
              if c.source_slug}
    linked = unlinked = ambiguous = created = 0
    seen = set()
    for o in s.exec(select(Outreach).where(Outreach.direction == "out")).all():
        L = s.get(Lead, o.lead_id) if o.lead_id else None
        if L is None:
            ambiguous += 1                          # orphan outbound row → can't attribute → review, don't guess
            continue
        grp = groups.get(L.source)
        if grp is None:
            unlinked += 1                           # lead exists but its source maps to no group (empty/unknown)
            continue
        linked += 1
        key = (grp.id, L.id)
        if key in seen or s.exec(select(CampaignRecipient.id).where(
                CampaignRecipient.campaign_id == grp.id, CampaignRecipient.lead_id == L.id)).first():
            seen.add(key)
            continue                                # a prior outreach for this lead already made the recipient
        status = ("replied" if L.buyer_replied_at else
                  "hard_bounced" if L.next_action_note == "bounced" else "sent")
        n_out = s.exec(select(func.count(Outreach.id)).where(
            Outreach.lead_id == L.id, Outreach.direction == "out")).one()
        s.add(CampaignRecipient(campaign_id=grp.id, tenant_id=L.seller_id, lead_id=L.id, company_id=L.company_id,
                                to_email=(L.email or o.recipient or "")[:200], sequence_version=1,
                                current_step=min(int(n_out), 50), status=status,
                                reply_outcome=L.reply_outcome or "", last_sent_at=o.created_at, inferred=True))
        seen.add(key); created += 1
    if ambiguous and not s.exec(select(WorkItem).where(WorkItem.idempotency_key == UNMATCHED_KEY)).first():
        WQ.create_work_item_safe(
            s, tenant_id=None, type="failed_system_job", source="automatic",
            title="Historical outreach needs review (unattributable)",
            description=(f"{ambiguous} historical outbound Outreach row(s) could not be deterministically "
                         "linked to a legacy group (missing lead). Preserved untouched; review before any "
                         "campaign re-use."),
            idempotency_key=UNMATCHED_KEY)
    return {"linked": linked, "unlinked": unlinked, "ambiguous": ambiguous, "recipients_created": created}


def _apply(s):
    # 1) legacy groups = distinct Lead.source groups → an INFERRED LEGACY OUTREACH GROUP each (NOT a campaign;
    #    idempotent on source_slug). Paused, never auto-enrolls/auto-sends; provenance recorded.
    have = {c.source_slug for c in s.exec(select(Campaign).where(Campaign.inferred == True)).all()}  # noqa: E712
    # relabel prior shell groups to the clear "inferred legacy group" concept (idempotent; auto-shells only,
    # never a group an admin has started/edited — guarded on context_kind != legacy_import AND started_at None)
    relabelled = 0
    for c in s.exec(select(Campaign).where(Campaign.inferred == True)).all():  # noqa: E712
        if c.context_kind != "legacy_import" and c.started_at is None:
            c.name = (c.name.replace("[legacy] ", "[legacy group] ", 1) if c.name.startswith("[legacy] ")
                      else f"[legacy group] {c.source_slug}"[:120])
            c.context_kind = "legacy_import"
            c.notes = ("Inferred legacy outreach group (imported from Lead.source). Not an authored campaign; "
                       "paused, never auto-enrolls or auto-sends.")
            s.add(c); relabelled += 1
    sources = {r for r in s.exec(select(Lead.source).distinct()) if r}
    new_campaigns = 0
    for src in sources:
        if src in have:
            continue
        leads = s.exec(select(Lead).where(Lead.source == src)).all()
        active = any(L.active and not L.buyer_replied_at for L in leads)
        status = "paused" if active else "completed"   # never auto-resume; conservative mapping
        s.add(Campaign(name=f"[legacy group] {src}"[:120], context_kind="legacy_import", source_slug=src,
                       status=status, inferred=True,
                       notes="Inferred legacy outreach group (imported from Lead.source). Not an authored "
                             "campaign; paused, never auto-enrolls or auto-sends."))
        new_campaigns += 1
    s.flush()   # so the just-created groups are visible to recipient linking below
    # 2) deterministically link historical outbound Outreach → recipients (reconciles the 197-vs-0 gap)
    link = _link_recipients(s)
    # 3) suppression + bounce records from existing hard bounces (evidence only)
    new_sup = new_bounce = 0
    bounced = s.exec(select(Lead).where(Lead.next_action_note == "bounced")).all()
    for L in bounced:
        # the address that bounced = the recipient of the last failed OUT row for this lead
        o = s.exec(select(Outreach).where(Outreach.lead_id == L.id, Outreach.direction == "out",
                   Outreach.status == "failed").order_by(Outreach.id.desc())).first()
        addr = SUP.normalize_email((o.recipient if o else "") or L.email)
        if not addr:
            continue
        if not SUP.is_suppressed(s, addr):
            SUP.suppress(s, addr, "hard_bounce", None, scope="platform", source_event=f"{BF}:lead:{L.id}")
            new_sup += 1
        if not s.exec(select(BounceRecord).where(BounceRecord.email_normalized == addr)).first():
            s.add(BounceRecord(email_normalized=addr, tenant_id=L.seller_id, lead_id=L.id, company_id=L.company_id,
                               bounce_type="hard", diagnostic=(o.error if o else "")[:500],
                               suppression_decision="suppressed", replacement_status="pending"))
            new_bounce += 1
    return {"campaigns": new_campaigns, "relabelled": relabelled, "suppressions": new_sup,
            "bounce_records": new_bounce, **link}


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
            out_total = post["outreach"]
            print(f"[dry-run] RECONCILIATION — {out_total} historical Outreach rows had 0 CampaignRecipient "
                  "because the shell backfill created only group shells; this run links them deterministically:")
            print(f"[dry-run]   outbound linked to a legacy group : {res['linked']}")
            print(f"[dry-run]   outbound unlinked (lead, no source): {res['unlinked']}")
            print(f"[dry-run]   outbound ambiguous (no lead → review task, not guessed): {res['ambiguous']}")
            print(f"[dry-run]   CampaignRecipient rows that would be created: {res['recipients_created']}")
            return
        try:
            res = _apply(s)
            post = _counts(s)
            for k in _OPERATIONAL:
                if pre[k] != post[k]:
                    raise RuntimeError(f"operational count changed for {k}: {pre[k]} -> {post[k]}")
            s.commit()
            print(f"OK — created {res}")
            print("POST:", post)
        except Exception:
            s.rollback()
            print("ERROR — rolled back, no partial backfill applied")
            raise


def rollback():
    """PRE-GO-LIVE: delete inferred legacy groups + their inferred recipients + backfill-sourced
    suppressions/bounce records + the aggregate review task. Historical Outreach rows are never touched."""
    init_db()
    with Session(engine) as s:
        camps = s.exec(select(Campaign).where(Campaign.inferred == True)).all()   # noqa: E712
        camp_ids = {c.id for c in camps}
        rcpts = [r for r in s.exec(select(CampaignRecipient).where(CampaignRecipient.inferred == True)).all()  # noqa: E712
                 if r.campaign_id in camp_ids]
        sups = s.exec(select(Suppression).where(Suppression.source_event.like(f"{BF}%"))).all()
        brs = s.exec(select(BounceRecord).where(BounceRecord.suppression_decision == "suppressed",
                     BounceRecord.replacement_status == "pending")).all()
        # only remove bounce records that pair a backfill suppression (by address)
        bf_addrs = {x.email_normalized for x in sups}
        brs = [b for b in brs if b.email_normalized in bf_addrs]
        tasks = s.exec(select(WorkItem).where(WorkItem.idempotency_key == UNMATCHED_KEY)).all()
        for row in rcpts + camps + sups + brs + tasks:   # recipients before campaigns (FK order)
            s.delete(row)
        s.commit()
        print(f"PRE-GO-LIVE rollback: deleted {len(camps)} inferred group(s), {len(rcpts)} inferred "
              f"recipient(s), {len(sups)} backfill suppression(s), {len(brs)} bounce record(s), "
              f"{len(tasks)} review task(s)")


def recover():
    """POST-GO-LIVE: remove only inferred campaigns that are still 'draft'/'paused' AND have NO recipients
    (never worked), preserving every campaign an admin has run/enrolled and all suppression (never auto-drop
    a do-not-contact entry post-go-live)."""
    from app.models import CampaignRecipient
    init_db()
    with Session(engine) as s:
        removed = 0
        for c in s.exec(select(Campaign).where(Campaign.inferred == True)).all():   # noqa: E712
            has_rcpt = s.exec(select(func.count()).where(CampaignRecipient.campaign_id == c.id)).one()
            if not has_rcpt and c.status in ("draft", "paused", "completed") and c.started_at is None:
                s.delete(c); removed += 1
        s.commit()
        print(f"POST-GO-LIVE recovery: removed {removed} untouched inferred campaign(s); all suppression, "
              f"bounce history and admin-run campaigns preserved")


if __name__ == "__main__":
    if "--rollback" in sys.argv:
        rollback()
    elif "--recover" in sys.argv:
        recover()
    else:
        migrate(dry="--dry-run" in sys.argv)
