"""Inbound reply + bounce effects (Phase 4). Deterministic (no autonomous AI classification) — an admin
confirms the final outcome in the Inbox. Every effect here is NON-BLOCKING (the caller wraps it) and
idempotent + condition-versioned via the Work Queue, so a genuinely NEW reply/bounce creates a new condition
version while a dispositioned task is never resurrected.

Rules honoured: a real human reply makes the company an Engaged Buyer; a negative human reply stays a valuable
Trade Network record; an automatic reply never creates engagement; an unsubscribe immediately suppresses the
address; a hard bounce suppresses + cancels pending campaign steps; a spam complaint suppresses + alerts.
"""
import re
from datetime import datetime

from sqlmodel import func, select

from . import suppression as SUP
from .models import BounceRecord, Outreach

_AUTO_RE = re.compile(r"\b(out of office|auto-?reply|automatic reply|autoresponder|vacation|"
                      r"do not reply|no-?reply|away from my|on leave|out of the office)\b", re.I)
_UNSUB_RE = re.compile(r"\b(unsubscribe|opt[- ]?out|remove me|stop emailing|take me off|do not contact)\b", re.I)
SOFT_BOUNCE_LIMIT = 3

# Phase 11 — quoted history must never be read as the buyer's words: every campaign email ends with an opt-out
# line, so a normal reply that quotes it ("send the price list" + our quoted footer) must NOT unsubscribe anyone.
# "On … wrote:"-style lines introduce a '>'-quoted block: skip the line and keep reading (a buyer may write BELOW the
# quote). "-----Original Message-----", a long rule or an Outlook From:/Sent: block introduce an UNMARKED copy of our
# email: everything after it is ours, so reading stops there.
_ATTRIBUTION = re.compile(
    r"^\s*(?:On\b.{0,300}\bwrote:|wrote:|Le\b.{0,300}\ba [ée]crit\s*:|Am\b.{0,300}\bschrieb.{0,40}:|"
    r"El\b.{0,300}\bescribi[óo]:|Il\b.{0,300}\bha scritto:|Op\b.{0,300}\bschreef.{0,40}:|.{0,300}\bpisze:|"
    r".{0,300}\bskrev:|.{0,300}\bnapsal\(a\):|.{0,300}\b[íi]rta:)\s*$", re.I)
_QUOTE_STOP = re.compile(r"^\s*(?:-{2,}\s*Original Message\s*-{2,}|_{8,})\s*$", re.I)
_FROM_HDR = re.compile(r"^\s*\*?(?:From|Von|De|Da|Od|Van|Från)\s*:\*?\s", re.I)
_SENT_HDR = re.compile(r"^\s*\*?(?:Sent|Date|Gesendet|Datum|Envoy[ée]|Enviado|Inviato|Wys[łl]ano|Verzonden|"
                       r"Skickat)\s*:", re.I)


def reply_text(body: str) -> str:
    """The buyer's OWN words: '>' quoted lines and quote attributions are dropped (text written below a quote is
    kept), reading stops at an unmarked copy of our email (Outlook From:/Sent: block, Original Message, a rule), and
    any line carrying our footer's opt-out sentence or unsubscribe link is never read as the buyer's words."""
    from .campaign_render import OPT_OUT_LINE
    lines = (body or "").splitlines()
    optout = " ".join(OPT_OUT_LINE.split()).lower()
    out = []
    for i, ln in enumerate(lines):
        if _QUOTE_STOP.match(ln) or (_FROM_HDR.match(ln) and any(_SENT_HDR.match(x) for x in lines[i + 1:i + 5])):
            break
        flat = " ".join(ln.split()).lower()
        if (_ATTRIBUTION.match(ln) or ln.lstrip().startswith(">") or optout in flat
                or "subject=unsubscribe" in flat):
            continue
        out.append(ln)
    return "\n".join(out).strip()


def is_auto_reply(subject: str, body: str) -> bool:
    return bool(_AUTO_RE.search(f"{subject or ''} {body or ''}"))


def is_unsubscribe(subject: str, body: str) -> bool:
    return bool(_UNSUB_RE.search(f"{subject or ''} {body or ''}"))


def classify_bounce(reason: str, smtp_status: str = "") -> tuple:
    """Return (bounce_type, is_hard). Deterministic from the DSN reason/status text."""
    t = f"{reason or ''} {smtp_status or ''}".lower()
    if any(k in t for k in ("spam", "complaint", "abuse", "blocked as spam", "listed on")):
        return "spam_complaint", True
    if any(k in t for k in ("mailbox full", "quota", "over quota", "552", "insufficient storage")):
        return "mailbox_full", False       # transient — retry, not an invalid address
    if any(k in t for k in ("no such user", "does not exist", "user unknown", "no such recipient",
                            "invalid recipient", "recipient rejected", "550", "5.1.1", "5.1.0")):
        return "hard", True
    if any(k in t for k in ("host not found", "no mx", "domain not found", "unable to resolve", "5.1.2")):
        return "domain_failure", True
    if any(k in t for k in ("policy", "rejected due to policy", "blocked", "5.7")):
        return "policy", False             # often transient/deliverability — retry / manual review
    if any(k in t for k in ("4.", "421", "450", "451", "452", "temporar", "try again", "deferred", "greylist")):
        return "soft", False
    return "unknown", False


def _inbound_count(session, lead_id) -> int:
    return session.exec(select(func.count()).where(Outreach.lead_id == lead_id,
                        Outreach.direction == "in")).one()


def _wq(session, actor, **spec):
    from . import work_queue as WQ
    return WQ.create_work_item_safe(session, actor=actor, **spec)


def on_reply(session, lead, subject, body, actor=None) -> dict:
    """Effects of an inbound buyer reply (already threaded + committed by the caller). Non-blocking. Classified on
    the buyer's own words only (quoted history + our footer stripped — see reply_text)."""
    from . import campaign_service as CS
    result = {"kind": "human", "engaged": False}
    own = reply_text(body)
    auto = is_auto_reply(subject, own)
    unsub = is_unsubscribe(subject, own)
    if unsub:
        result["kind"] = "unsubscribe"
        SUP.suppress(session, lead.email, "unsubscribe", actor, scope="platform",
                     source_event=f"reply:lead:{lead.id}")
        lead.reply_outcome = "negative"
        CS.stop_recipient(session, lead.id, "unsubscribed", actor)
    elif auto:
        result["kind"] = "auto"
        lead.reply_outcome = "auto_reply"          # auto replies never create Engaged Buyer status
        CS.stop_recipient(session, lead.id, "replied", actor)
    else:
        # a real human reply → Engaged Buyer (positive/negative is admin-confirmed in the Inbox)
        lead.engagement_class = "engaged"
        CS.stop_recipient(session, lead.id, "replied", actor)
        result["engaged"] = True
        n = _inbound_count(session, lead.id)
        _wq(session, actor, type="review_inbound_reply",
            title=f"Review inbound reply from buyer #{lead.id}",
            description="A buyer replied — confirm the outcome (positive/negative/…) in the Inbox.",
            tenant_id=lead.seller_id, related_lead_id=lead.id,
            idempotency_key=f"review_inbound_reply:lead:{lead.id}", condition_version=f"reply:{n}")
    session.add(lead)
    session.commit()
    # a managed buyer who really answered our email moves forward in the seller's (anonymized) funnel — only when
    # we actually emailed them, so a sender-address match onto some other buyer record can't advance it
    if result["engaged"] and CS._last_out(session, lead.id):
        try:
            from . import pipeline
            if pipeline.advance_stage(session, lead, "responded", actor, note="buyer replied by email"):
                session.commit()
        except Exception:  # noqa: BLE001
            session.rollback()
    return result


def _record_bounce(session, lead, email, bounce_type, reason, smtp_status="", enhanced="", outreach_id=None):
    em = SUP.normalize_email(email)
    rec = session.exec(select(BounceRecord).where(BounceRecord.email_normalized == em)).first()
    now = datetime.utcnow()
    if rec:
        rec.bounce_count += 1
        rec.last_bounce_at = now
        rec.bounce_type = bounce_type
        rec.diagnostic = (reason or "")[:500]
        rec.updated_at = now
    else:
        rec = BounceRecord(email_normalized=em, tenant_id=getattr(lead, "seller_id", None),
                           company_id=getattr(lead, "company_id", None), lead_id=getattr(lead, "id", None),
                           outreach_id=outreach_id, bounce_type=bounce_type, smtp_status=smtp_status[:20],
                           enhanced_status=enhanced[:12], diagnostic=(reason or "")[:500])
    session.add(rec)
    return rec


def on_bounce(session, lead, email, reason, smtp_status="", actor=None) -> dict:
    """Effects of a bounce (the caller already marked the Outreach failed + cleared the address + committed).
    Non-blocking. Classifies, records a durable BounceRecord, and applies the suppression/cancel/alert rule."""
    from . import campaign_service as CS
    btype, is_hard = classify_bounce(reason, smtp_status)
    rec = _record_bounce(session, lead, email, btype, reason, smtp_status)
    out = {"bounce_type": btype, "suppressed": False}
    if btype == "spam_complaint":
        SUP.suppress(session, email, "spam_complaint", actor, scope="platform", source_event=f"bounce:lead:{lead.id}")
        CS.stop_recipient(session, lead.id, "hard_bounced", actor)
        rec.suppression_decision = "suppressed"
        out["suppressed"] = True
        _wq(session, actor, type="spam_complaint", priority="high",
            title=f"Spam complaint for {SUP.normalize_email(email)}",
            description="A spam complaint was received — address suppressed. Review deliverability.",
            tenant_id=getattr(lead, "seller_id", None), related_lead_id=getattr(lead, "id", None),
            idempotency_key=f"spam_complaint:{SUP.normalize_email(email)}", condition_version="complaint")
    elif is_hard:                              # hard / domain_failure → invalid address
        SUP.suppress(session, email, "hard_bounce", actor, scope="platform", source_event=f"bounce:lead:{lead.id}")
        CS.stop_recipient(session, lead.id, "hard_bounced", actor)
        rec.suppression_decision = "suppressed"
        rec.replacement_status = "pending"
        out["suppressed"] = True
        # (the replace_invalid_contact work item is created idempotently by sync_bounced_contacts)
    else:                                      # soft / mailbox_full / policy → retry until the limit
        CS.stop_recipient(session, lead.id, "soft_bounced", actor)
        if rec.bounce_count >= SOFT_BOUNCE_LIMIT:
            SUP.suppress(session, email, "persistent_soft", actor, scope="platform",
                         source_event=f"bounce:lead:{lead.id}")
            rec.suppression_decision = "suppressed"
            out["suppressed"] = True
        else:
            rec.suppression_decision = "retry"
    session.add(rec)
    session.commit()
    return out
