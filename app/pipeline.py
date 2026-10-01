"""Confidential managed buyer-outreach pipeline.

Buyer PII lives on the (admin-owned) Lead; sellers only ever get `anon_prospect(...)` — an allow-listed,
non-identifying projection. The 13-stage `Lead.pipeline_stage` is the admin's managed pipeline; it is kept
in sync with the coarse 5-stage `Lead.status` (CRM) by the SINGLE helper `set_pipeline_stage`, so the two
can never drift. The seller funnel is computed from real `StageEvent` history (`reached_stages`), never by
inferring "reached" from the current stage — so a Lost/Disqualified buyer is never counted as reaching Won.
"""
import json
import re
import unicodedata
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from .countries import _NAMES as _COUNTRY_NAMES
from .models import AuditLog, Lead, SellerUpdate, StageEvent

# --------------------------------------------------------------------------- stages
# Linear progression up to Negotiating, then THREE separate terminal branches.
LINEAR_PATH = ["identified", "verified", "contacted", "responded", "interested", "qualified",
               "pricing_requested", "quote_prepared", "quote_sent", "negotiating"]
TERMINALS = {"won", "lost", "disqualified"}
PIPELINE_STAGES = LINEAR_PATH + ["won", "lost", "disqualified"]
_ORDER = {s: i for i, s in enumerate(LINEAR_PATH)}

# Sanitized labels the seller sees (never leak internal wording / buyer identity).
PUBLIC_STAGE_LABEL = {
    "identified": "Identified", "verified": "Verified", "contacted": "Contacted",
    "responded": "In conversation", "interested": "Interested", "qualified": "Qualified",
    "pricing_requested": "Pricing requested", "quote_prepared": "Quote in preparation",
    "quote_sent": "Quote sent", "negotiating": "Negotiating",
    "won": "Won", "lost": "Closed — no deal", "disqualified": "Not a fit",
}

# pipeline_stage -> the coarse 5-stage CRM status (new|quoted|negotiating|won|lost). Kept in sync so quotes,
# deals and the old CRM never disagree with the managed pipeline.
STATUS_MAP = {
    "identified": "new", "verified": "new", "contacted": "new", "responded": "new",
    "interested": "new", "qualified": "new",
    "pricing_requested": "quoted", "quote_prepared": "quoted", "quote_sent": "quoted",
    "negotiating": "negotiating", "won": "won", "lost": "lost", "disqualified": "lost",
}

STANDARD_LOSS_REASONS = ["price too high", "found another supplier", "no budget / on hold",
                         "quality / spec mismatch", "payment / banking friction", "unresponsive",
                         "not a real buyer", "other"]

SIZE_BANDS = ["1-10", "11-50", "51-200", "201-1000", "1000+"]

# Structured, sanitized seller-update templates.
UPDATE_TEMPLATES = [
    "Buyer requested pricing for {quantity} under {incoterm}.",
    "Waiting for the buyer to confirm {requirement}.",
    "Buyer is reviewing the quote; expecting a reply by {date}.",
    "Buyer declined — {reason}.",
    "Contacted {count} new prospects this week; awaiting responses.",
    "Sample / spec requested before the buyer commits.",
]

_NAME_ISO = {"iraq": "IQ", "türkiye": "TR", "turkiye": "TR", "turkey": "TR", "uzbekistan": "UZ",
             "georgia": "GE", "united arab emirates": "AE", "uae": "AE", "kazakhstan": "KZ",
             "afghanistan": "AF", "azerbaijan": "AZ", "russia": "RU", "turkmenistan": "TM",
             "saudi arabia": "SA", "pakistan": "PK", "kuwait": "KW", "egypt": "EG", "ukraine": "UA",
             "bahrain": "BH", "iran": "IR", "qatar": "QA", "oman": "OM"}


def _country_iso(dest_country):
    c = (dest_country or "").strip()
    if not c:
        return "XX"
    if c.lower() in _NAME_ISO:
        return _NAME_ISO[c.lower()]
    up = "".join(ch for ch in c.upper() if ch.isalpha())
    return up[:2] if len(up) >= 2 else "XX"


def can_transition(from_stage, to_stage):
    """Validated graph. From a non-terminal an admin may move to any other stage (correcting/advancing) or a
    terminal; from a terminal only an explicit reopen to 'negotiating' is allowed; never a no-op."""
    if to_stage not in PIPELINE_STAGES or from_stage == to_stage:
        return False
    if from_stage in TERMINALS:
        return to_stage == "negotiating"
    return True


# --------------------------------------------------------------------------- anonymization
def anon_prospect(lead):
    """The ONLY per-buyer data a seller may receive. No company/contact/email/phone/website/source_url/
    notes/tracking_code/db-id — ever. No city either: in a niche market, city + country can name the buyer."""
    return {
        "anon_ref": lead.anon_ref or "",
        "stage": PUBLIC_STAGE_LABEL.get(lead.pipeline_stage, "In progress"),
        "stage_key": lead.pipeline_stage,
        "country": lead.dest_country or "",
        "category": lead.buyer_category or "",
        "size_band": lead.company_size_band or "",
        "fit_score": int(lead.fit_score or 0),
        "action_required": bool(lead.seller_action_required),
    }


def next_anon_ref(session, request_id, dest_country):
    """`Buyer-<ISO>-<NNN>`, sequential WITHIN the request per country prefix — derived from existing refs,
    not the DB id."""
    iso = _country_iso(dest_country)
    prefix = f"Buyer-{iso}-"
    existing = session.exec(select(Lead.anon_ref).where(
        Lead.request_id == request_id, Lead.anon_ref.like(prefix + "%"))).all()
    mx = 0
    for r in existing:
        m = re.search(r"-(\d+)$", r or "")
        if m:
            mx = max(mx, int(m.group(1)))
    return f"{prefix}{mx + 1:03d}"


def assign_anon_ref(session, lead, retries=8):
    """Set a unique anon_ref on `lead` and flush; on the UNIQUE(request_id,anon_ref) race, recompute + retry
    inside a savepoint so a concurrent create can never duplicate a reference."""
    for _ in range(retries):
        ref = next_anon_ref(session, lead.request_id, lead.dest_country)
        lead.anon_ref = ref
        session.add(lead)
        try:
            with session.begin_nested():
                session.flush()
            return ref
        except IntegrityError:
            continue
    raise RuntimeError("could not assign a unique anon_ref after retries")


# --------------------------------------------------------------------------- audit
def audit(session, actor, entity_type, entity_id, action, meta=None, tenant_id=None):
    """Write an audit record. `tenant_id` scopes it to the seller the action concerns (via a validated
    relationship — the lead's/request's seller_id), so the audit trail is filterable per tenant and never
    orphaned from the party it affects."""
    session.add(AuditLog(actor_id=getattr(actor, "id", None), tenant_id=tenant_id, entity_type=entity_type,
                         entity_id=entity_id, action=action, meta=json.dumps(meta or {})[:2000]))


# --------------------------------------------------------------------------- the ONE stage helper
def set_pipeline_stage(session, lead, to_stage, actor, note=""):
    """The single entry point for changing a managed buyer's stage. Validates the transition, records a
    StageEvent (authoritative history) + AuditLog, sets pipeline_stage AND syncs Lead.status via STATUS_MAP
    so the two systems can never disagree. Won/Lost side-effects (Deal creation, loss reason) are handled by
    the caller. Returns (ok, error)."""
    frm = lead.pipeline_stage or "identified"
    if not can_transition(frm, to_stage):
        return False, f"cannot move {frm} → {to_stage}"
    lead.pipeline_stage = to_stage
    lead.status = STATUS_MAP.get(to_stage, lead.status)          # keep CRM status in lock-step
    if to_stage in ("lost", "disqualified") and note:
        lead.lost_reason = note[:200]
    session.add(lead)
    session.add(StageEvent(lead_id=lead.id, request_id=lead.request_id, from_stage=frm,
                           to_stage=to_stage, actor_id=getattr(actor, "id", None), note=(note or "")[:500]))
    audit(session, actor, "lead", lead.id, "stage_change", {"from": frm, "to": to_stage},
          tenant_id=lead.seller_id)
    return True, ""


def advance_stage(session, lead, to_stage, actor=None, note=""):
    """Forward-only AUTOMATIC stage move for a managed buyer (campaign send → contacted, human reply → responded).
    Never regresses (a buyer already at/after `to_stage`, or at a terminal, is left alone), never touches an
    unmanaged lead, and never runs once the CRM status has left 'new' (the stage→status sync would otherwise walk
    it backwards). Goes through set_pipeline_stage, so the StageEvent + audit are written. Returns True if moved."""
    if lead is None or not getattr(lead, "managed", False) or to_stage not in _ORDER:
        return False
    cur = lead.pipeline_stage or "identified"
    if cur in TERMINALS or cur not in _ORDER or _ORDER[cur] >= _ORDER[to_stage]:
        return False
    if (lead.status or "new") != "new":
        return False
    ok, _err = set_pipeline_stage(session, lead, to_stage, actor, note=note)
    return ok


# --------------------------------------------------------------------------- funnel (branch-correct)
def reached_stages(session, lead_id):
    """The set of stages a buyer ACTUALLY visited, from StageEvent history — the basis of 'reached'."""
    rows = session.exec(select(StageEvent.to_stage).where(StageEvent.lead_id == lead_id)).all()
    return {r for r in rows if r}


def request_funnel(session, sr):
    """Per-request progress. Returns BOTH `at_<stage>` (current-stage counts) and `reached_<stage>`
    (cumulative from real history) with unambiguous names, plus rates + bottleneck + outstanding actions.
    A Lost/Disqualified buyer is counted ONLY in the stages its history actually contains (never Won)."""
    leads = session.exec(select(Lead).where(Lead.request_id == sr.id, Lead.managed == True)).all()  # noqa: E712
    at = {s: 0 for s in PIPELINE_STAGES}
    for lead in leads:
        at[lead.pipeline_stage] = at.get(lead.pipeline_stage, 0) + 1

    visited = {}
    last_activity = None
    migrated_ids = set()                     # buyers whose history includes a migration-seeded (inferred) event
    if leads:
        lead_ids = [lead.id for lead in leads]
        for lid, st, ts, inf in session.exec(
                select(StageEvent.lead_id, StageEvent.to_stage, StageEvent.created_at, StageEvent.inferred)
                .where(StageEvent.lead_id.in_(lead_ids))).all():
            visited.setdefault(lid, set()).add(st)
            if inf:
                migrated_ids.add(lid)        # inferred: don't count as observed activity
            elif ts and (last_activity is None or ts > last_activity):
                last_activity = ts
    for lead in leads:                       # the current stage is always "reached"
        visited.setdefault(lead.id, set()).add(lead.pipeline_stage)
    reached = {s: sum(1 for v in visited.values() if s in v) for s in PIPELINE_STAGES}

    contacted = reached["contacted"] or 1e-9
    countries = sorted({lead.dest_country for lead in leads if lead.dest_country})
    # bottleneck = the linear stage (before terminals) where the most buyers are currently stuck
    open_counts = [(at[s], s) for s in LINEAR_PATH]
    bottleneck = max(open_counts, default=(0, ""))[1] if any(c for c, _ in open_counts) else ""
    next_dates = [lead.next_action_at for lead in leads if lead.next_action_at]
    # "Action required" is driven by OPEN, resolvable seller questions — never a flag that can silently
    # go stale. A published question stays counted until it is explicitly resolved (seller answers / admin
    # closes it). See SellerUpdate.status.
    outstanding = len(session.exec(select(SellerUpdate.id).where(
        SellerUpdate.request_id == sr.id, SellerUpdate.status == "open",
        SellerUpdate.seller_question != "")).all())

    out = {
        "total_prospects": len(leads),
        "at": {s: at[s] for s in PIPELINE_STAGES},          # current-stage counts
        "reached": {s: reached[s] for s in PIPELINE_STAGES},  # cumulative, from history
        "countries_covered": len(countries), "countries": countries,
        "response_rate": round(100 * reached["responded"] / contacted),
        "interest_rate": round(100 * reached["interested"] / contacted),
        "last_activity": last_activity,
        "current_bottleneck": PUBLIC_STAGE_LABEL.get(bottleneck, bottleneck),
        "next_planned_action": min(next_dates) if next_dates else None,
        "outstanding_seller_actions": outstanding,
        # migrated buyers carry a single inferred starting event — their PRE-migration funnel history is not
        # tracked, so cumulative "reached" counts for them are incomplete. Surfaced honestly, never invented.
        "migrated_prospects": len(migrated_ids),
        "history_complete": len(migrated_ids) == 0,
    }
    # flat, unambiguous convenience fields for templates/api
    for s in PIPELINE_STAGES:
        out[f"reached_{s}"] = reached[s]
        out[f"at_{s}"] = at[s]
    return out


# --------------------------------------------------------------------------- sanitization
# bare domains: ANY 2-letter country ending (.pl .de .it .ca .nz .se .ch …) plus the common generic ones. Legal
# forms (.ltd/.llc/.gmbh) are left out on purpose: "Co.Ltd" in a company name is not a website.
_GENERIC_TLDS = ("com|net|org|info|biz|shop|store|online|site|website|xyz|app|dev|tech|pro|group|global|trade|"
                 "company|business|email|world|asia|mobi|services|solutions|supply|tools|center|centre|systems|"
                 "industries|international")
_PII_PATTERNS = [
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("mailto/tel", re.compile(r"\b(?:mailto|tel):", re.I)),
    ("whatsapp", re.compile(r"(?:wa\.me/|whatsapp)", re.I)),
    ("url", re.compile(r"\b(?:https?://|www\.)\S+", re.I)),
    ("domain", re.compile(r"\b[a-z0-9][a-z0-9-]{1,}\.(?:[a-z]{2}|" + _GENERIC_TLDS + r")\b", re.I)),
    ("phone", re.compile(r"(?:\+|00)\d[\d\s().\-]{6,}\d|\b\d[\d\s().\-]{8,}\d\b")),
]


def sanitize_scan(text):
    """Return a list of PII/contact hits an admin must remove before a seller-visible update can publish."""
    t = text or ""
    hits = []
    seen = set()
    for kind, pat in _PII_PATTERNS:
        for m in pat.finditer(t):
            frag = m.group(0).strip()[:80]
            key = (kind, frag.lower())
            if frag and key not in seen:
                seen.add(key)
                hits.append({"kind": kind, "match": frag})
    return hits


# --------------------------------------------------------------------------- request-scoped buyer denylist
# The pattern scan cannot know a buyer's NAME or CITY. Every admin-authored text that can reach a seller (published
# updates, chat, answers, seller-safe deliveries) is also matched against the request's own buyers: company names
# (4+ chars, legal form dropped), emails, website hosts, cities and phone digits.
_FOLD_CHARS = str.maketrans({"ł": "l", "ø": "o", "æ": "ae", "œ": "oe", "ß": "ss", "đ": "d", "ð": "d", "þ": "th",
                             "ı": "i"})


def _fold(s):
    """Lower-case, accent-free, whitespace-collapsed — the one form needles and text are compared in, so 'Kraków',
    'KRAKOW' and 'krakow' all match."""
    s = unicodedata.normalize("NFKD", str(s or "").lower().translate(_FOLD_CHARS))
    return " ".join("".join(ch for ch in s if not unicodedata.combining(ch)).split())


# a country (the seller already sees it) or a generic place word in a buyer's name/city field identifies nobody
_NOT_A_NEEDLE = ({_fold(n) for n in _COUNTRY_NAMES.values()} | {_fold(n) for n in _NAME_ISO}
                 | {"usa", "uk", "ksa", "england", "scotland", "wales", "northern ireland", "great britain", "czechia",
                    "holland", "america", "europe", "middle east", "gcc", "asia", "africa", "north america",
                    "south america", "latin america", "nationwide", "national", "worldwide", "international",
                    "global", "online", "multiple", "various", "several", "head office", "headquarters", "branches",
                    "multi-location", "countrywide", "unknown"})
# trade words that are also some buyer's brand ('Fastener Agencies' on fastener.co.nz) or whole name ('Fixings') —
# never a needle on their own, or every update about the product would be blocked
_TRADE_WORDS = {"fastener", "fasteners", "fastening", "fixing", "fixings", "anchor", "anchors", "bolt", "bolts",
                "screw", "screws", "tool", "tools", "hardware", "supply", "supplies", "building", "trade", "trading",
                "steel", "metal", "metals", "industrial", "direct", "express", "group", "global", "home", "house",
                "drywall", "plaster", "wholesale", "united", "general", "power", "prime", "star", "best", "royal"}
_EMAIL_RE = _PII_PATTERNS[0][1]
_DIGIT_RUN = re.compile(r"\d[\d\s().\-/]*\d")
_MIN_NEEDLE = 4                 # shorter names/cities ('ACE', 'ON') would match ordinary text
_MIN_PHONE_DIGITS = 7           # a phone matches on its last 7 digits, however it is written


def _host_label(host):
    """'richelieu.com' -> 'richelieu', 'shop.toolbank.co.uk' -> 'toolbank'."""
    parts = (host or "").split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "ac", "gov", "edu") and len(parts[-1]) == 2:
        return parts[-3]
    return parts[-2] if len(parts) >= 2 else ""


def denylist_needles(leads):
    """[(kind, folded needle, shown)] for the given buyers — what a seller-visible text must never contain."""
    from .campaign_render import display_company       # lazy: keeps this module dependency-light (no cycles)
    from .company_service import normalize_domain
    out, seen = [], set()

    def add(kind, value):
        shown = " ".join(str(value or "").split()).strip(" ,.;:-")
        folded = _fold(shown)
        if kind == "buyer phone":
            ok = len(folded) >= _MIN_PHONE_DIGITS
        else:
            ok = (len(folded) >= _MIN_NEEDLE and folded not in _NOT_A_NEEDLE
                  and not folded.replace(" ", "").isdigit()
                  and not (kind == "buyer name" and folded in _TRADE_WORDS))
        if ok and (kind, folded) not in seen:
            seen.add((kind, folded))
            out.append((kind, folded, shown))

    for ld in leads:
        name = getattr(ld, "buyer_company", "") or ""
        brand = display_company(name)                           # "Richelieu Hardware Ltd" -> "Richelieu Hardware"
        add("buyer name", name)
        add("buyer name", brand)
        for inner in re.findall(r"\(([^()]*)\)", name):         # "IHL Canada (Investments Hardware Ltd.)"
            add("buyer name", display_company(inner))
        emails = [em.lower() for em in _EMAIL_RE.findall(getattr(ld, "email", "") or "")]
        hosts = {normalize_domain(em) for em in emails} | {normalize_domain(getattr(ld, "website", "") or "")}
        hosts.discard("")                                       # '' = gmail & co (never a buyer identity)
        for em in emails:
            add("buyer email", em)
        for host in sorted(hosts):
            add("buyer website", host)
        # the brand alone ("Richelieu") when it IS the buyer's own web/mail domain — strong evidence it names them
        first = re.sub(r"[^\w-]+", "", (brand.split() or [""])[0])
        if len(brand.split()) > 1 and _fold(first) in {_host_label(h) for h in hosts}:
            add("buyer name", first)
        city = re.sub(r"\([^()]*\)", " ", getattr(ld, "dest_city", "") or "")   # "(14 branches across …)"
        for part in re.split(r"[,/;&|+]+|\s-\s|\band\b|\bor\b", city, flags=re.I):
            add("buyer city", re.sub(r"\S*\d\S*", " ", part))   # postcodes / districts ("Dublin 22")
        for run in _DIGIT_RUN.findall(getattr(ld, "phone", "") or ""):
            add("buyer phone", re.sub(r"\D", "", run)[-_MIN_PHONE_DIGITS:])
    return out


def denylist_hits(text, needles):
    """Hits of `needles` (from denylist_needles) in `text`: names/cities as whole words, emails/hosts as whole
    addresses, phones by digits whatever the separators."""
    t = text or ""
    hay = _fold(t)
    runs = [re.sub(r"\D", "", r) for r in _DIGIT_RUN.findall(t)]
    hits = []
    for kind, needle, shown in needles:
        if kind == "buyer phone":
            found = any(needle in r for r in runs)
        elif needle not in hay:
            found = False
        elif kind in ("buyer email", "buyer website"):
            found = re.search(r"(?<![\w-])" + re.escape(needle) + r"(?![\w-])", hay) is not None
        else:
            found = re.search(r"(?<!\w)" + re.escape(needle) + r"(?!\w)", hay) is not None
        if found:
            hits.append({"kind": kind, "match": shown[:80]})
    return hits


def request_denylist(session, sr):
    """Needles for every buyer a text about request `sr` must never name: the request's own buyers plus the seller's
    other managed buyers (still scoped to that one seller — never the whole database)."""
    cond = Lead.request_id == sr.id
    if sr.owner_id:
        cond = cond | ((Lead.managed == True) & (Lead.seller_id == sr.owner_id))  # noqa: E712
    return denylist_needles(session.exec(select(Lead).where(cond)).all())


def seller_text_hits(session, sr, text, needles=None):
    """Everything an admin must remove before `text` may reach the seller of `sr`: contact/PII patterns + the
    request's buyer denylist. Empty list = safe to show."""
    if needles is None:
        needles = request_denylist(session, sr)
    return sanitize_scan(text) + denylist_hits(text, needles)


def link_hits(link, needles):
    """A delivered link is a URL by nature, so only contact schemes, addresses and the buyers' own identifiers
    (website host, name, …) count against it."""
    return ([h for h in sanitize_scan(link) if h["kind"] in ("email", "mailto/tel", "whatsapp")]
            + denylist_hits(link, needles))


def strip_seller_identity(text, seller, seller_emails=(), extra_names=()):
    """Redact the SELLER's own identity (name/email + connected mailbox addresses + profile company/names) out of
    buyer-facing text — the confidentiality is two-way. Names match as whole words only, so a short seller name
    never mangles ordinary words (e.g. 'Ali' inside 'quality')."""
    t = text or ""
    needles = ([getattr(seller, "email", ""), getattr(seller, "name", "")] + list(seller_emails or [])
               + list(extra_names or []))
    for nd in needles:
        nd = (nd or "").strip()
        if nd and len(nd) > 2:
            pat = re.escape(nd) if "@" in nd else r"(?<!\w)" + re.escape(nd) + r"(?!\w)"
            t = re.sub(pat, "[redacted]", t, flags=re.I)
    return t
