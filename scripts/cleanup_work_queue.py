"""Work Queue cleanup (Phase 12) — clear legacy noise out of the admin Work Queue. Never deletes a row.

    docker exec go4it-app python scripts/cleanup_work_queue.py                                # DRY-RUN (default)
    docker exec go4it-app python scripts/cleanup_work_queue.py --apply --actor <admin email>  # one transaction
    ... --only product_incomplete,approve_quote                       # just these task types
    ... --revert wqc-20261001-093000                                  # dry-run of re-opening what a batch closed
    ... --revert wqc-20261001-093000 --apply --actor <admin email>

RUN ORDER (prod): backup_db.py -> deploy the Phase-12 scanner fixes -> this dry-run -> founder reviews -> --apply.

Per OPEN task (anything else is left for the founder and listed by id):
  * condition already gone -> COMPLETED exactly like the scanners' own auto-resolve (resolved_by NULL, so it never
    counts as anyone's work): product archived/complete, quote no longer draft, duplicate pair already disposed
    (not 'confirmed' — that still needs the merge), contact no longer bounced;
  * known legacy noise -> DISMISSED with a recorded reason: unpriced supplier listings nobody has used, auto-drafted
    quotes (before 2026-08-15) never approved/sent/replied to, pre-campaign (before 2026-09-01) outreach failures,
    legacy honey bounces with no replacement address, quotes of the disabled seed demo accounts;
  * NEVER touched: review_inbound_reply + unmatched_inbound (buyer replies, incl. the founder's test reply), and any
    task that is not 'open', is assigned, was created by hand, or touches a managed buyer / a seller / a recipient of
    a real (non-legacy) campaign.
A dismissal is durable: the scanners never recreate a task while its condition_version is unchanged, and raise a new
one as soon as the condition really changes. Every reason/note starts with "[<batch>]" and every touched task gets a
`work_item_cleanup` audit row (batch, rule, action, previous state) — that is what --revert uses.

OPT-IN data fixes (OFF by default — only with the founder's OK):
  --cancel-stale-drafts       cancel those auto-drafted quotes (draft -> cancelled + QuoteStatusEvent) instead of only
                              dismissing their task. Cancelled is terminal: --revert can NOT undo it.
  --defer-legacy-duplicates   set legacy pairs (before 2026-09-01, no managed buyer, no shared email/domain) to
                              'deferred' (+ the same dup_dispose audit as /duplicates). --revert restores them.

Checked before commit (any violation rolls everything back): same WorkItem row count; same Quote, Lead, Outreach,
DuplicateCandidate, Opportunity, Product, Suppression and BounceRecord counts; quote + candidate statuses unchanged
(except what an opt-in planned); no excluded task and no task outside the plan changed; every touched task terminal.
Output = counts per type + the task ids left for the founder. Never titles, names or addresses.
"""
import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict, namedtuple
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from sqlmodel import Session, func, select  # noqa: E402

from app import authz  # noqa: E402
from app import pipeline  # noqa: E402
from app import quote_workflow as QW  # noqa: E402
from app import work_queue as WQ  # noqa: E402
from app.db import engine, init_db  # noqa: E402
from app.models import (AuditLog, BounceRecord, Campaign, CampaignRecipient, CatalogGenerationJob, Company,  # noqa: E402
                        Deal, DuplicateCandidate, Lead, Opportunity, Outreach, Product, ProductPriceVersion, Quote,
                        QuoteVersion, Suppression, User, WorkItem)
from app.seed import USERS as SEED_USERS  # noqa: E402
from app.suppression import normalize_email  # noqa: E402
from app.tenant import is_admin  # noqa: E402

EXCLUDED = ("review_inbound_reply", "unmatched_inbound")       # buyer replies (+ the founder's test reply): never
RULE_TYPES = ("product_incomplete", "approve_quote", "review_potential_duplicate", "failed_system_job",
              "replace_invalid_contact", "quote_expired")
LEGACY_BEFORE = datetime(2026, 9, 1)     # campaigns started in September 2026 — earlier outreach is legacy
DRAFT_BEFORE = datetime(2026, 8, 15)     # the July lead-import auto-drafts; a newer draft is left for the founder
COUNTED = (Quote, Lead, Outreach, DuplicateCandidate, Opportunity, Product, Suppression, BounceRecord)
BATCH_RE = re.compile(r"^wqc-\d{8}-\d{6}(?:-\d+)?$")
TERMINAL = ("completed", "dismissed")

Decision = namedtuple("Decision", "wi action code text")      # action: resolve|dismiss|cancel|defer|manual


class Refused(Exception):
    """Bad arguments / actor — nothing was attempted."""


class InvariantError(Exception):
    """A pre-commit check failed — the whole batch is rolled back."""


def new_batch(now=None):
    return f"wqc-{(now or datetime.utcnow()):%Y%m%d-%H%M%S}"


def _batch_used(s, batch):
    return s.exec(select(AuditLog.id).where(AuditLog.action == "work_item_cleanup", AuditLog.meta.contains(
        f'"batch": "{batch}"', autoescape=True))).first() is not None


def _unused_batch(s, batch):
    base, n = batch, 1
    while _batch_used(s, batch):
        n += 1
        batch = f"{base}-{n}"
    return batch


def find_actor(s, email):
    """The active internal admin running the cleanup (recorded on every audit row + dismissal)."""
    u = s.exec(select(User).where(func.lower(User.email) == (email or "").strip().lower())).first()
    if u is None or not u.active or not is_admin(u):
        return None
    p = authz.profile(s, u)
    if p is not None and (p.account_status != "active" or p.account_class != "internal"):
        return None
    return u


# --------------------------------------------------------------------------- who is on the seller side
def _disabled(s, u):
    p = authz.profile(s, u)
    return not u.active or (p is not None and p.account_status != "active")


class _Ctx:
    def __init__(self, s, flags):
        self.flags = flags or {}
        emails = [u["email"] for u in SEED_USERS if u["role"] != "admin"]
        # seed demo accounts (app/seed.py) that have been disabled — nothing they own was ever a real buyer's
        self.demo = {u.id for u in s.exec(select(User).where(User.email.in_(emails))).all() if _disabled(s, u)}
        # the seller side of the confidentiality line (Phase-10 class 'seller' / a non-admin without a profile)
        self.sellers = {u.id for u in s.exec(select(User)).all()
                        if u.id not in self.demo and authz.account_class(s, u) == "seller"}


def _managed(ld, ctx):
    return bool(ld.managed or ld.seller_id is not None or ld.request_id is not None or ld.owner_id in ctx.sellers)


def _related_leads(s, wi):
    ids = {wi.related_lead_id} - {None}
    o = s.get(Outreach, wi.related_outreach_id) if wi.related_outreach_id else None
    q = s.get(Quote, wi.related_quote_id) if wi.related_quote_id else None
    ids |= {getattr(o, "lead_id", None), getattr(q, "lead_id", None)} - {None, 0}
    return [ld for ld in (s.get(Lead, i) for i in ids) if ld is not None]


def _companies_managed(s, company_ids, ctx):
    """A company touches a managed buyer when it is a seller's, has a managed/seller buyer, or it (or one of its
    buyers) is a recipient of a real (non-legacy) campaign."""
    ids = sorted({c for c in company_ids if c})
    if not ids:
        return False
    if any(co.tenant_id in ctx.sellers for co in s.exec(select(Company).where(Company.id.in_(ids))).all()):
        return True
    leads = s.exec(select(Lead).where(Lead.company_id.in_(ids))).all()
    if any(_managed(ld, ctx) for ld in leads):
        return True
    lead_ids = [ld.id for ld in leads] or [0]
    return s.exec(select(CampaignRecipient.id).join(Campaign, Campaign.id == CampaignRecipient.campaign_id).where(
        Campaign.inferred == False,  # noqa: E712 — legacy groups are history, not a live campaign
        CampaignRecipient.company_id.in_(ids) | CampaignRecipient.lead_id.in_(lead_ids))).first() is not None


def _cand(s, wi):
    cid = WQ.key_id(wi.idempotency_key) if (wi.idempotency_key or "").startswith("review_dup:cand:") else None
    return s.get(DuplicateCandidate, cid) if cid else None


def _guard(s, wi, ctx):
    """Why this task stays with the founder ('' = a rule may handle it)."""
    if wi.type in EXCLUDED:
        return "excluded"
    if wi.status != "open":
        return "not open"
    if wi.assigned_admin_id is not None:
        return "assigned"
    if wi.source == "manual":
        return "manual task"
    if wi.related_request_id is not None or wi.tenant_id in ctx.sellers:
        return "seller"
    if any(_managed(ld, ctx) for ld in _related_leads(s, wi)):
        return "managed buyer"
    companies = [wi.related_company_id]
    dc = _cand(s, wi) if wi.type == "review_potential_duplicate" else None
    if dc is not None:
        if dc.tenant_id in ctx.sellers:
            return "seller"
        companies += [dc.left_id, dc.right_id]
    if _companies_managed(s, companies, ctx):
        return "managed buyer"
    if wi.type not in RULE_TYPES:
        return "no rule"
    return ""


# --------------------------------------------------------------------------- per-type rules → (action, code, text)
def _rule_product(s, wi, ctx):
    p = s.get(Product, wi.related_product_id) if wi.related_product_id else None
    if p is None:
        return "resolve", "product removed", "product removed"
    if not p.active:
        return "resolve", "product archived", "product archived"
    miss = WQ._missing_fields(s, p)
    if not miss:
        return "resolve", "product complete", "product complete"
    if wi.condition_version != ",".join(sorted(miss)):
        return "manual", "changed since", ""
    if p.verification_status != "unverified" or not set(miss) <= {"base_price", "unit"}:
        return "manual", "needs data", ""
    for model in (Quote, ProductPriceVersion, CatalogGenerationJob):
        if s.exec(select(model.id).where(model.product_id == p.id)).first() is not None:
            return "manual", "in use", ""
    added = f" (added {p.created_at:%Y-%m})" if p.created_at else ""   # pre-Phase-5 products have no created_at
    return "dismiss", "unpriced listing", (
        f"unpriced supplier listing{added}: no supplier unit/EXW price yet; never quoted; "
        "not catalog-ready; re-alerts if a field changes")


def _rule_draft_quote(s, wi, ctx):
    q = s.get(Quote, wi.related_quote_id) if wi.related_quote_id else None
    if q is None:
        return "resolve", "quote removed", "quote removed"
    if q.status != "draft":
        return "resolve", "quote left draft", "quote left draft"
    if wi.condition_version != f"draft:v{q.version}":
        return "manual", "changed since", ""
    ld = s.get(Lead, q.lead_id)
    replied = ld is None or ld.buyer_replied_at is not None or s.exec(select(Outreach.id).where(
        Outreach.lead_id == q.lead_id, Outreach.direction == "in")).first() is not None
    sent = s.exec(select(QuoteVersion.id).where(QuoteVersion.quote_id == q.id,
                                                QuoteVersion.sent_at.is_not(None))).first() is not None
    deal = s.exec(select(Deal.id).where((Deal.quote_id == q.id) | (Deal.lead_id == q.lead_id))).first() is not None
    if (q.created_by or q.approved_by or q.share_token or sent or deal or replied
            or not q.created_at or q.created_at >= DRAFT_BEFORE):
        return "manual", "real draft", ""
    if ctx.flags.get("cancel_stale_drafts"):
        return "cancel", "stale auto-draft", "quote cancelled (stale auto-draft)"
    return "dismiss", "auto-draft", (f"auto-drafted at lead import {q.created_at:%Y-%m-%d}; never approved or sent; "
                                     "no buyer reply")


def _rule_duplicate(s, wi, ctx):
    dc = _cand(s, wi)
    if dc is None:
        return "resolve", "candidate removed", "candidate removed"
    if dc.status not in ("open", "confirmed"):
        return "resolve", f"candidate {dc.status}", f"candidate {dc.status}"
    try:
        strong = {"email_exact", "domain_exact"} & set(json.loads(dc.signals or "[]"))
    except ValueError:
        strong = {"unreadable"}
    if dc.status == "open" and not strong and dc.created_at and dc.created_at < LEGACY_BEFORE:
        if ctx.flags.get("defer_legacy_duplicates"):
            return "defer", "legacy pair", "candidate deferred (legacy pair; no managed buyer)"
        return "manual", "legacy pair (--defer-legacy-duplicates)", ""
    return "manual", "needs review", ""


def _bounce_note(s, email):
    em = normalize_email(email)
    if not em:
        return ""
    bounced = s.exec(select(BounceRecord.id).where(BounceRecord.email_normalized == em)).first() is not None
    suppressed = s.exec(select(Suppression.id).where(Suppression.email_normalized == em,
                                                     Suppression.active == True)).first() is not None  # noqa: E712
    return ("bounce + suppression already recorded; " if bounced and suppressed
            else "bounce already recorded; " if bounced else "")


def _rule_failed_job(s, wi, ctx):
    if not (wi.idempotency_key or "").startswith("failed_job:outreach:"):
        return "manual", "system failure", ""    # render-skip, mailbox paused, send review, ingest/command, ...
    oid = WQ.key_id(wi.idempotency_key)
    o = s.get(Outreach, oid) if oid else None
    if o is None or wi.condition_version != "failed":
        return "manual", "system failure", ""
    if o.campaign_id is not None or not o.created_at or o.created_at >= LEGACY_BEFORE:
        return "manual", "campaign or recent", ""
    return "dismiss", "legacy outreach failure", (f"legacy pre-campaign outreach failure ({o.created_at:%b %Y}); "
                                                  f"{_bounce_note(s, o.recipient)}no retry")


def _rule_bounced_contact(s, wi, ctx):
    ld = s.get(Lead, wi.related_lead_id) if wi.related_lead_id else None
    if ld is None:
        return "resolve", "lead removed", "lead removed"
    if ld.next_action_note != "bounced":
        return "resolve", "contact no longer bounced", "contact no longer bounced"
    if wi.condition_version != (ld.email or "bounced"):
        return "manual", "changed since", ""
    honey = "honey" in f"{ld.source or ''} {ld.product or ''}".lower()
    if ld.email or not honey or not wi.created_at or wi.created_at >= LEGACY_BEFORE:
        return "manual", "needs a replacement", ""
    return "dismiss", "legacy honey bounce", ("legacy honey bounce (pre-campaign); address cleared; enrichment found "
                                              "no replacement; re-alerts if a new address bounces")


def _rule_expired_quote(s, wi, ctx):
    q = s.get(Quote, wi.related_quote_id) if wi.related_quote_id else None
    ld = s.get(Lead, q.lead_id) if q is not None else None
    owners = {getattr(q, "owner_id", None), getattr(ld, "owner_id", None)} - {None}
    if q is not None and owners and owners <= ctx.demo:
        return "dismiss", "demo seed quote", "demo seed quote (disabled demo account); never a real buyer"
    return "manual", "lapsed quote", ""


RULES = {"product_incomplete": _rule_product, "approve_quote": _rule_draft_quote,
         "review_potential_duplicate": _rule_duplicate, "failed_system_job": _rule_failed_job,
         "replace_invalid_contact": _rule_bounced_contact, "quote_expired": _rule_expired_quote}


def plan(s, only=None, flags=None):
    """One Decision per non-terminal task (in the --only types)."""
    ctx = _Ctx(s, flags)
    out = []
    for wi in s.exec(select(WorkItem).where(WorkItem.status.in_(WQ.NONTERMINAL)).order_by(WorkItem.id)).all():
        if only and wi.type not in only:
            continue
        why = _guard(s, wi, ctx)
        out.append(Decision(wi, "manual", why, "") if why else Decision(wi, *RULES[wi.type](s, wi, ctx)))
    return out


# --------------------------------------------------------------------------- apply + invariants
def _prev(wi):
    return {"completed_at": wi.completed_at.isoformat() if wi.completed_at else None, "resolved_by": wi.resolved_by,
            "resolution_note": (wi.resolution_note or "")[:200], "dismissed_reason": (wi.dismissed_reason or "")[:200],
            "condition_version": wi.condition_version or ""}


def _dt(v):
    return datetime.fromisoformat(v) if v else None


def apply(s, decisions, actor, batch):
    """Carry out the plan; returns the ids of the tasks closed. Noise is DISMISSED (by the actor); a task whose
    condition is gone is COMPLETED with no resolver, like the scanners do."""
    tag = f"[{batch}] "
    touched = []
    for d in decisions:
        if d.action == "manual":
            continue
        wi = d.wi
        meta = {"batch": batch, "rule": f"{wi.type}:{d.code}", "action": d.action, "type": wi.type,
                "prev_status": wi.status, "prev": _prev(wi)}
        if d.action == "cancel":
            q = s.get(Quote, wi.related_quote_id)
            ok, err = QW.transition(s, q, "cancelled", actor=actor, reason=tag + "stale auto-draft (Work Queue cleanup)")
            if not ok:
                raise InvariantError(f"quote {q.id} could not be cancelled: {err}")
            meta["quote"] = q.id
        elif d.action == "defer":
            dc = _cand(s, wi)
            meta["cand"] = {"id": dc.id, "status": dc.status, "reviewer": dc.reviewer,
                            "reviewed_at": dc.reviewed_at.isoformat() if dc.reviewed_at else None}
            dc.status, dc.reviewer, dc.reviewed_at = "deferred", getattr(actor, "email", "") or "", datetime.utcnow()
            s.add(dc)
            pipeline.audit(s, actor, "company", dc.left_id, "dup_dispose",
                           {"cand": dc.id, "action": "deferred", "batch": batch}, tenant_id=dc.tenant_id)
        if d.action == "dismiss":
            WQ.dismiss_item(s, wi, tag + d.text, actor)
        else:
            if wi.type == "product_incomplete" and d.code in ("product archived", "product removed"):
                WQ.mark_closed_by_archive(wi)       # same as the scanner: a restored product alerts again
            WQ.complete_item(s, wi, None, tag + d.text)
        pipeline.audit(s, actor, "work_item", wi.id, "work_item_cleanup", meta, tenant_id=wi.tenant_id)
        touched.append(wi.id)
    return touched


def _state(w):
    return (w.status, w.type, w.idempotency_key, w.condition_version, w.assigned_admin_id, w.completed_at,
            w.resolved_by, w.resolution_note, w.dismissed_reason)


def snapshot(s):
    """What must not change besides the planned tasks."""
    s.flush()
    return {"counts": {m.__name__: s.exec(select(func.count(m.id))).one() for m in (WorkItem,) + COUNTED},
            "quotes": dict(s.exec(select(Quote.id, Quote.status)).all()),
            "cands": dict(s.exec(select(DuplicateCandidate.id, DuplicateCandidate.status)).all()),
            "items": {w.id: _state(w) for w in s.exec(select(WorkItem)).all()}}


def check_invariants(s, before, decisions, touched):
    after = snapshot(s)
    bad = [f"{k} count {before['counts'][k]} -> {v}" for k, v in after["counts"].items() if before["counts"][k] != v]
    cancelled = {d.wi.related_quote_id for d in decisions if d.action == "cancel"}
    deferred = {_cand(s, d.wi).id for d in decisions if d.action == "defer"}
    bad += [f"quote {i} status changed" for i, st in after["quotes"].items()
            if st != before["quotes"].get(i) and not (i in cancelled and st == "cancelled")]
    bad += [f"candidate {i} status changed" for i, st in after["cands"].items()
            if st != before["cands"].get(i) and not (i in deferred and st == "deferred")]
    done = set(touched)
    for i, st in after["items"].items():
        was = before["items"].get(i)
        if was and was[1] in EXCLUDED and st != was:
            bad.append(f"excluded task {i} changed")
        elif i in done:
            if st[0] not in TERMINAL:
                bad.append(f"task {i} not terminal")
        elif st != was:
            bad.append(f"task {i} changed outside the plan")
    if bad:
        raise InvariantError("; ".join(bad[:12]) + (f" (+{len(bad) - 12} more)" if len(bad) > 12 else ""))


def cleanup(s, actor, batch, only=None, flags=None):
    """Plan → apply → invariants. The caller commits (apply) or rolls back (dry-run / error)."""
    before = snapshot(s)
    decisions = plan(s, only, flags)
    touched = apply(s, decisions, actor, batch)
    check_invariants(s, before, decisions, touched)
    return decisions


# --------------------------------------------------------------------------- revert
def revert(s, batch, actor):
    """Re-open what one batch closed: previous status back, completion fields restored, deferred candidates back.
    Skips a task changed since, one whose key has a NEWER open task (the partial-unique index), and a cancelled
    quote's task (cancelled is terminal). Returns (reopened ids, [(id, why)])."""
    tag = f"[{batch}]"
    reopened, skipped = [], []
    n_before = s.exec(select(func.count(WorkItem.id))).one()
    rows = s.exec(select(AuditLog).where(AuditLog.action == "work_item_cleanup", AuditLog.meta.contains(
        f'"batch": "{batch}"', autoescape=True)).order_by(AuditLog.id)).all()
    for row in rows:
        try:
            meta = json.loads(row.meta or "{}")
        except ValueError:
            skipped.append((row.entity_id, "unreadable audit row"))
            continue
        if meta.get("batch") != batch:
            continue
        wi = s.get(WorkItem, row.entity_id) if row.entity_id else None
        if wi is None:
            skipped.append((row.entity_id, "task missing"))
            continue
        if meta.get("action") == "cancel":
            skipped.append((wi.id, "its quote was cancelled (cannot be undone)"))
            continue
        if wi.status not in TERMINAL or not ((wi.dismissed_reason or "").startswith(tag)
                                             or (wi.resolution_note or "").startswith(tag)):
            skipped.append((wi.id, "changed since the cleanup"))
            continue
        if any(o.id != wi.id for o in WQ.open_items_for_key(s, wi.idempotency_key)):
            skipped.append((wi.id, "a newer open task has the same key"))
            continue
        cand = meta.get("cand")
        if cand:
            dc = s.get(DuplicateCandidate, cand.get("id"))
            if dc is None or dc.status != "deferred":
                skipped.append((wi.id, "candidate changed since the cleanup"))
                continue
            dc.status, dc.reviewer, dc.reviewed_at = cand.get("status") or "open", cand.get("reviewer") or "", \
                _dt(cand.get("reviewed_at"))
            s.add(dc)
            pipeline.audit(s, actor, "company", dc.left_id, "dup_dispose",
                           {"cand": dc.id, "action": "revert", "batch": batch}, tenant_id=dc.tenant_id)
        prev = meta.get("prev") or {}
        wi.status = meta.get("prev_status") or "open"
        wi.completed_at = _dt(prev.get("completed_at"))
        wi.resolved_by = prev.get("resolved_by")
        wi.resolution_note = prev.get("resolution_note") or ""
        wi.dismissed_reason = prev.get("dismissed_reason") or ""
        if "condition_version" in prev:     # batches written before this field keep the version they have
            wi.condition_version = prev["condition_version"] or ""
        wi.updated_at = datetime.utcnow()
        s.add(wi)
        s.flush()                       # a partial-unique clash surfaces here, before anything is committed
        pipeline.audit(s, actor, "work_item", wi.id, "work_item_cleanup_revert", {"batch": batch, "to": wi.status},
                       tenant_id=wi.tenant_id)
        reopened.append(wi.id)
    s.flush()
    if s.exec(select(func.count(WorkItem.id))).one() != n_before:
        raise InvariantError("WorkItem row count changed")
    return reopened, skipped


# --------------------------------------------------------------------------- output (ids + counts only)
def _ids(ids, cap=60):
    return ", ".join(str(i) for i in ids[:cap]) + (f" (+{len(ids) - cap} more)" if len(ids) > cap else "")


def print_plan(decisions, header, flags):
    rows, manual = defaultdict(Counter), defaultdict(list)
    for d in decisions:
        r = rows[d.wi.type]
        r["open"] += 1
        r["dismiss" if d.action == "dismiss" else "manual" if d.action == "manual" else "resolve"] += 1
        if d.action == "manual":
            manual[(d.wi.type, d.code)].append(d.wi.id)
    order = [t for t in RULE_TYPES if t in rows] + sorted((t for t in rows if t not in RULE_TYPES),
                                                         key=lambda t: (-rows[t]["open"], t))
    print(header)
    print(f"{'type':<30}{'open':>6}{'resolve':>9}{'dismiss':>9}{'manual':>8}")
    total = Counter()
    for t in order:
        total.update(rows[t])
        print(f"{t:<30}{rows[t]['open']:>6}{rows[t]['resolve']:>9}{rows[t]['dismiss']:>9}{rows[t]['manual']:>8}")
    print(f"{'TOTAL':<30}{total['open']:>6}{total['resolve']:>9}{total['dismiss']:>9}{total['manual']:>8}")
    if manual:
        print("left for the founder (task ids):")
        for (t, code), ids in sorted(manual.items(), key=lambda kv: (order.index(kv[0][0]), kv[0][1])):
            print(f"  {t} · {code}: {_ids(ids)}")
    n_cancel = sum(1 for d in decisions if d.action == "cancel")
    n_defer = sum(1 for d in decisions if d.action == "defer")
    if flags.get("cancel_stale_drafts") or flags.get("defer_legacy_duplicates"):
        print(f"opt-in data fixes: {n_cancel} draft quote(s) cancelled, {n_defer} duplicate pair(s) deferred")
    return total


def _print_revert(batch, reopened, skipped):
    print(f"revert {batch}: {len(reopened)} task(s) re-opened, {len(skipped)} skipped")
    if reopened:
        print(f"  re-opened: {_ids(reopened)}")
    by = defaultdict(list)
    for i, why in skipped:
        by[why].append(i)
    for why, ids in by.items():
        print(f"  skipped · {why}: {_ids(ids)}")


def _err(e):
    """The error's type + first line only (a driver error can carry SQL parameters)."""
    return f"{type(e).__name__}: {(str(e).splitlines() or [''])[0][:300]}"


# --------------------------------------------------------------------------- CLI
def _run(s, a, only, flags, mode):
    actor = None
    if a.actor:
        actor = find_actor(s, a.actor)
        if actor is None:
            raise Refused("--actor must be an active internal admin account")
    who = f" · actor #{actor.id}" if actor else ""
    if a.revert:
        reopened, skipped = revert(s, a.revert, actor)
        print(f"Work Queue cleanup REVERT · {mode}{who}")
        _print_revert(a.revert, reopened, skipped)
        return
    batch = _unused_batch(s, new_batch())
    decisions = cleanup(s, actor, batch, only, flags)
    scope = f" · only {','.join(sorted(only))}" if only else ""
    print_plan(decisions, f"Work Queue cleanup · {mode} · batch {batch}{who}{scope}", flags)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true", help="write the changes (default: dry-run, nothing persisted)")
    ap.add_argument("--actor", default="", help="email of the active admin running it (required with --apply)")
    ap.add_argument("--only", default="", help="comma-separated task types (default: every rule type)")
    ap.add_argument("--revert", default="", metavar="BATCH", help="re-open what one cleanup batch closed")
    ap.add_argument("--cancel-stale-drafts", action="store_true",
                    help="OPT-IN: cancel the stale auto-drafted quotes (irreversible; founder's OK only)")
    ap.add_argument("--defer-legacy-duplicates", action="store_true",
                    help="OPT-IN: set legacy duplicate pairs with no managed buyer to 'deferred'")
    a = ap.parse_args(argv)
    only = {t.strip() for t in a.only.split(",") if t.strip()}
    flags = {"cancel_stale_drafts": a.cancel_stale_drafts, "defer_legacy_duplicates": a.defer_legacy_duplicates}
    why = ""
    if only & set(EXCLUDED):
        why = f"--only {','.join(sorted(only & set(EXCLUDED)))}: never cleaned up by this script"
    elif only - set(WQ.TYPES):
        why = f"--only {','.join(sorted(only - set(WQ.TYPES)))}: unknown task type"
    elif a.revert and not BATCH_RE.match(a.revert):
        why = "--revert needs a batch id like wqc-20261001-093000"
    elif a.revert and (only or any(flags.values())):
        why = "--revert takes no --only / opt-in flags"
    elif a.apply and not a.actor:
        why = "--apply needs --actor <active admin email>"
    if why:
        print(f"REFUSED: {why}")
        return 2
    init_db()
    if not a.apply:
        # DRY-RUN: the exact apply path on a dedicated CONNECTION-level transaction, rolled back at the end — nothing
        # persists whatever the inner code does (same pattern as backfill_work_items.py).
        conn = engine.connect()
        trans = conn.begin()
        s = Session(bind=conn)
        try:
            _run(s, a, only, flags, "DRY-RUN (nothing is written)")
        except Refused as e:
            print(f"REFUSED: {e}")
            return 2
        except Exception as e:  # noqa: BLE001
            print(f"ERROR — {_err(e)} — dry-run rolled back, nothing written")
            return 1
        finally:
            s.close()
            trans.rollback()
            conn.close()
        print("[dry-run] rolled back — nothing persisted."
              + ("" if a.revert else " To apply: --apply --actor <admin email>"))
        return 0
    with Session(engine) as s:
        try:
            _run(s, a, only, flags, "APPLIED")
            s.commit()
        except Refused as e:
            s.rollback()
            print(f"REFUSED: {e}")
            return 2
        except Exception as e:  # noqa: BLE001
            s.rollback()
            print(f"ERROR — {_err(e)} — rolled back, nothing changed")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
