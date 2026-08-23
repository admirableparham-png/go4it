"""Trade Network (Phase 2) — canonical identity resolution + data quality + dedup.

The SINGLE choke point that turns denormalized Lead/Supplier rows into a canonical, admin-only
Company/Contact/Provenance graph. Everything here is ADDITIVE and NON-BLOCKING: a link failure never breaks
lead/supplier creation (callers use the `*_safe` wrappers). Dedup is conservative and strictly tenant-scoped
(never across tenants); nothing is ever auto-merged. Confidentiality: buyer companies are scoped to the
seller they are FOR (managed → seller_id), so seller A can never learn seller B got the same buyer.
"""
import json
import logging
import re
import unicodedata
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from .enrich_service import valid_email
from .models import (Company, CompanyRole, Contact, Deal, DuplicateCandidate, Lead, Outreach,
                     Provenance, Supplier)
from .pipeline import audit

logger = logging.getLogger("go4it.company")

# --------------------------------------------------------------------------- constants
ROLES = ("buyer", "seller", "supplier")

# Free-mail / consumer domains — NEVER used as a company-identity match signal.
GENERIC_DOMAINS = {
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "hotmail.co.uk", "live.com",
    "live.co.uk", "msn.com", "yahoo.com", "yahoo.co.uk", "yahoo.co.in", "ymail.com", "rocketmail.com",
    "icloud.com", "me.com", "mac.com", "aol.com", "protonmail.com", "proton.me", "gmx.com", "gmx.net",
    "gmx.de", "mail.com", "mail.ru", "inbox.ru", "list.ru", "bk.ru", "yandex.com", "yandex.ru",
    "zoho.com", "qq.com", "163.com", "126.com", "sina.com", "foxmail.com", "naver.com", "hanmail.net",
    "daum.net", "web.de", "t-online.de", "freenet.de", "orange.fr", "wanadoo.fr", "free.fr", "laposte.net",
    "rediffmail.com", "hotmail.fr", "libero.it", "virgilio.it", "terra.com.br", "uol.com.br",
}
_JUNK_HOSTS = {"facebook.com", "instagram.com", "linkedin.com", "twitter.com", "x.com", "youtube.com",
               "wa.me", "whatsapp.com", "t.me", "google.com", "wixsite.com", "blogspot.com",
               "wordpress.com", "godaddy.com", "sites.google.com", "bit.ly", "amazon.com", "ebay.com",
               "alibaba.com", "made-in-china.com", "indiamart.com"}


def _is_junk_host(s):
    """Host-aware junk match: 'x.com' or 'sub.x.com' is junk, but 'acmefix.com' is NOT (substring-safe)."""
    return any(s == h or s.endswith("." + h) for h in _JUNK_HOSTS)
_LEGAL_TOKENS = {"ltd", "llc", "inc", "co", "corp", "corporation", "company", "gmbh", "fze", "fzc", "fzco",
                 "llp", "plc", "pvt", "pte", "sarl", "srl", "bv", "b.v", "ag", "sa", "s.a", "jsc", "ooo",
                 "as", "ab", "oy", "sl", "s.l", "spa", "kg", "nv", "group", "trading", "intl",
                 "international", "import", "export", "imports", "exports", "the", "and"}
ROLE_MAILBOXES = {"sales", "export", "exports", "info", "contact", "office", "commercial", "trade",
                  "hello", "order", "orders", "purchase", "purchasing", "procurement", "admin", "enquiry",
                  "enquiries", "inquiries", "mail", "buy", "buying", "sourcing"}

# Anti-placeholder guards: a phone/domain shared by more than N companies in a tenant is treated as noise.
PHONE_SHARE_MAX = 8
DOMAIN_SHARE_MAX = 15

# Raw-source → (readable source_type, readable name) mapping. First match wins; never guess (→ unknown).
SOURCE_TYPES = ("research_agent", "command", "seller_request", "manual", "csv_import", "directory",
                "marketplace", "customs", "tender_rfq", "referral", "existing_supplier", "unknown")


# --------------------------------------------------------------------------- normalizers
def normalize_name(name):
    s = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    toks = [t for t in s.split() if t and t not in _LEGAL_TOKENS]
    return " ".join(toks).strip()


def normalize_domain(website_or_email):
    """Registrable host from a website OR an email; '' if generic/junk/empty."""
    s = str(website_or_email or "").strip().lower()
    if not s:
        return ""
    if "@" in s:
        s = s.rsplit("@", 1)[-1]
    s = re.sub(r"^https?://", "", s).split("/")[0].split("?")[0]
    s = s.split(":")[0].strip().strip(".")
    if s.startswith("www."):
        s = s[4:]
    if not s or "." not in s or _is_junk_host(s):
        return ""
    if s in GENERIC_DOMAINS:
        return ""
    return s


def normalize_email(email):
    e = str(email or "").strip().lower()
    return e if e and valid_email(e) else ""


def normalize_phone(phone):
    d = re.sub(r"\D", "", str(phone or ""))
    return d[-9:] if len(d) >= 7 else ""


# --------------------------------------------------------------------------- source mapping
def map_source(raw_source, external_id=""):
    """(source_type, readable_name, run_ref). Honest: an undeterminable slug stays 'unknown' with the raw
    slug preserved as the name so an admin can reclassify it later."""
    s = (raw_source or "").strip().lower()
    ext = (external_id or "").strip()
    if not s:
        return ("unknown", "Unknown", "")
    m = re.match(r"req-(\d+)", s)
    if m:
        return ("seller_request", f"Concierge request {m.group(1)}", f"req-{m.group(1)}")
    if "command" in s or ext.startswith("cmd:"):
        return ("command", "Founder command", f"command:{ext}" if ext else "command")
    if "research" in s:
        return ("research_agent", "Research agent", s)
    if "customs" in s:
        return ("customs", "Customs declarations", s)
    if "tender" in s or "procurement" in s or "rfq" in s:
        return ("tender_rfq", "Tender / RFQ", s)
    if any(k in s for k in ("go4world", "tradeindia", "alibaba", "made-in-china", "tradekey",
                            "europages", "kompass", "marketplace")):
        return ("marketplace", s.replace("_browser", "").replace("-", " ").title(), s)
    if any(k in s for k in ("osm", "directory", "yellowpages", "yell", "businesses")):
        return ("directory", "Directory harvest", s)
    if "csv" in s or "import" in s:
        return ("csv_import", "CSV import", s)
    if "referral" in s:
        return ("referral", "Referral", s)
    if s == "manual" or s == "seed":
        return ("manual", "Manual entry", s)
    if s == "existing_supplier":
        return ("existing_supplier", "Existing supplier", s)
    return ("unknown", s, s)          # keep the raw slug — never mis-bucket


SOURCE_LABELS = {"research_agent": "Research agent", "command": "Founder command",
                 "seller_request": "Concierge request", "manual": "Manual entry", "csv_import": "CSV import",
                 "directory": "Directory harvest", "marketplace": "Marketplace / B2B portal",
                 "customs": "Customs data", "tender_rfq": "Tender / RFQ", "referral": "Referral",
                 "existing_supplier": "Existing supplier", "unknown": "Unknown source"}


# --------------------------------------------------------------------------- email health / contactability
def email_health(email, bounced=False):
    e = str(email or "").strip().lower()
    if not e:
        return "unknown"
    if not valid_email(e):
        return "invalid"
    if bounced:
        return "bounced"
    dom = e.rsplit("@", 1)[-1]
    local = e.split("@", 1)[0]
    if dom in GENERIC_DOMAINS:
        return "generic"
    if local in ROLE_MAILBOXES:
        return "role"
    return "valid"


def contactability(health, has_phone, verified=False):
    score = 0
    if health in ("valid", "role"):
        score += 45
    elif health == "generic":
        score += 25
    if health == "role":
        score += 10
    if has_phone:
        score += 30
    if verified:
        score += 15
    return min(100, score)


# --------------------------------------------------------------------------- company get-or-create
def _add_role(session, company, role):
    if role not in ROLES:
        return
    exists = session.exec(select(CompanyRole).where(
        CompanyRole.company_id == company.id, CompanyRole.role == role)).first()
    if not exists:
        try:
            with session.begin_nested():
                session.add(CompanyRole(company_id=company.id, role=role))
        except IntegrityError:
            pass


def get_or_create_company(session, tenant_id, name, country="", website="", role="buyer",
                          email="", phone=""):
    """Find an existing company IN THE SAME TENANT via STRONG signals only (exact normalized email/phone/
    non-generic domain, then exact normalized name+country), else create one. Returns the Company."""
    nname = normalize_name(name)
    dom = normalize_domain(website) or normalize_domain(email)
    nemail = normalize_email(email)
    nphone = normalize_phone(phone)

    def _same_tenant(stmt):
        return stmt.where(Company.tenant_id == tenant_id) if tenant_id is not None \
            else stmt.where(Company.tenant_id.is_(None))

    # AUTO-LINK only on the highest-confidence identity signals (exact non-generic domain, exact email).
    # Phone-exact and name+country are surfaced as dedup CANDIDATES for admin review, never auto-collapsed
    # (a shared receptionist number or a common name must not silently merge distinct companies).
    found = None
    if dom:                       # strong: shared non-generic domain
        found = session.exec(_same_tenant(select(Company)).where(
            Company.domain == dom, Company.status == "active")).first()
    if not found and nemail:      # strong: a contact carrying this exact email
        c = session.exec(select(Contact).where(
            Contact.email_normalized == nemail,
            Contact.tenant_id == tenant_id if tenant_id is not None else Contact.tenant_id.is_(None))).first()
        if c:
            found = session.get(Company, c.company_id)
    if not found and nname and not dom and not nemail:
        # name-only record with no contact anchor: consolidate ONLY with another name-only company (never
        # override a domain/email-identified one) — keeps name-less directory rows from proliferating.
        found = session.exec(_same_tenant(select(Company)).where(
            Company.name_normalized == nname, Company.country == (country or ""),
            Company.domain == "", Company.status == "active")).first()

    if found and found.status == "active":
        if dom and not found.domain:
            found.domain = dom
        found.updated_at = datetime.utcnow()
        session.add(found)
        _add_role(session, found, role)
        return found

    company = Company(tenant_id=tenant_id, name=(name or "").strip()[:200], name_normalized=nname,
                      primary_role=role, country=(country or "").strip(), website=(website or "").strip(),
                      domain=dom)
    session.add(company)
    session.flush()
    _add_role(session, company, role)
    return company


def get_or_create_contact(session, company, name="", title="", email="", phone="", website="",
                          bounced=False, is_primary=False):
    nemail = normalize_email(email)
    nphone = normalize_phone(phone)
    q = select(Contact).where(Contact.company_id == company.id)
    existing = None
    for c in session.exec(q).all():
        if (nemail and c.email_normalized == nemail) or (nphone and c.phone_normalized == nphone) or \
           (not nemail and not nphone and (c.name or "").strip().lower() == (name or "").strip().lower()):
            existing = c
            break
    health = email_health(email, bounced=bounced)
    if existing:
        existing.email = existing.email or (email or "")
        existing.email_normalized = existing.email_normalized or nemail
        existing.phone = existing.phone or (phone or "")
        existing.phone_normalized = existing.phone_normalized or nphone
        if health != "unknown":
            existing.email_health = health
        existing.contactability = contactability(existing.email_health, bool(existing.phone_normalized))
        existing.updated_at = datetime.utcnow()
        session.add(existing)
        return existing
    contact = Contact(company_id=company.id, tenant_id=company.tenant_id, name=(name or "").strip()[:120],
                      title=(title or "").strip()[:120], email=(email or "").strip(), email_normalized=nemail,
                      phone=(phone or "").strip(), phone_normalized=nphone, website=(website or "").strip(),
                      email_health=health, contactability=contactability(health, bool(nphone)),
                      is_primary=is_primary)
    session.add(contact)
    session.flush()
    return contact


def add_provenance(session, entity_type, entity_id, tenant_id, source_type, source_name, source_ref="",
                   source_url="", run_ref="", inferred=False, collected_at=None):
    """Idempotent (the uq_provenance_dedup partial index makes re-seen provenance a last_seen_at bump)."""
    now = datetime.utcnow()
    if source_ref:
        ex = session.exec(select(Provenance).where(
            Provenance.entity_type == entity_type, Provenance.entity_id == entity_id,
            Provenance.source_type == source_type, Provenance.source_ref == source_ref)).first()
        if ex:
            ex.last_seen_at = now
            session.add(ex)
            return ex
    p = Provenance(entity_type=entity_type, entity_id=entity_id, tenant_id=tenant_id, source_type=source_type,
                   source_name=source_name[:160], source_ref=source_ref[:160], source_url=(source_url or "")[:400],
                   run_ref=run_ref[:80], inferred=inferred, collected_at=collected_at or now, last_seen_at=now)
    try:
        with session.begin_nested():
            session.add(p)
            session.flush()
        return p
    except IntegrityError:
        return None


# --------------------------------------------------------------------------- linking
def _lead_tenant(lead):
    """The isolation scope for a lead's company: managed buyers → the seller they are FOR, else owner_id."""
    if lead.managed and lead.seller_id is not None:
        return lead.seller_id
    return lead.owner_id


def link_lead_company(session, lead, inferred=False):
    """Resolve/create the buyer Company for a Lead, set lead.company_id, add a Contact + Provenance, and
    (re)derive engagement. Returns the Company. Assumes the lead is already committed."""
    if not (lead.buyer_company or "").strip():
        return None
    tenant = _lead_tenant(lead)
    company = get_or_create_company(session, tenant, lead.buyer_company, lead.dest_country, lead.website,
                                    role="buyer", email=lead.email, phone=lead.phone)
    lead.company_id = company.id
    if lead.contact_name or lead.email or lead.phone:
        get_or_create_contact(session, company, name=lead.contact_name, email=lead.email, phone=lead.phone,
                              website=lead.website, bounced=(lead.next_action_note == "bounced"),
                              is_primary=True)
    st, sname, run_ref = map_source(lead.source, lead.external_id)
    add_provenance(session, "company", company.id, tenant, st, sname, source_ref=(lead.external_id or ""),
                   source_url=lead.source_url, run_ref=run_ref, inferred=inferred,
                   collected_at=lead.posted_at or lead.created_at)
    ec, ro = classify_engagement(session, lead)
    lead.engagement_class, lead.reply_outcome = ec, ro
    session.add(lead)
    return company


def link_supplier_company(session, supplier, inferred=False):
    if not (supplier.name or "").strip():
        return None
    company = get_or_create_company(session, None, supplier.name, supplier.country, "",
                                    role="supplier", email=supplier.email, phone=supplier.phone)
    supplier.company_id = company.id
    if supplier.contact or supplier.email or supplier.phone:
        get_or_create_contact(session, company, name=supplier.contact, email=supplier.email,
                              phone=supplier.phone, is_primary=True)
    add_provenance(session, "company", company.id, None, "existing_supplier", "Existing supplier",
                   source_ref=f"supplier:{supplier.id}", inferred=inferred, collected_at=supplier.created_at)
    session.add(supplier)
    return company


def link_lead_company_safe(session, lead):
    """Non-blocking: a link failure must NEVER break lead creation. Logs a dq_ audit row on failure."""
    try:
        with session.begin_nested():
            link_lead_company(session, lead)
        return True
    except Exception:  # noqa: BLE001
        logger.warning("company link failed for lead %s", getattr(lead, "id", None), exc_info=True)
        try:
            audit(session, None, "lead", getattr(lead, "id", None), "dq_link_failed", {"stage": "create_lead"})
        except Exception:  # noqa: BLE001
            pass
        return False


def link_supplier_company_safe(session, supplier):
    try:
        with session.begin_nested():
            link_supplier_company(session, supplier)
        return True
    except Exception:  # noqa: BLE001
        logger.warning("company link failed for supplier %s", getattr(supplier, "id", None), exc_info=True)
        return False


# --------------------------------------------------------------------------- engagement classification
_AUTO_RE = re.compile(r"\b(out of office|automatic reply|auto-?reply|autoresponder|on vacation|"
                      r"away from my email|delivery status notification)\b", re.I)
_NEG_REASONS = ("price", "too high", "mismatch", "spec", "terms", "not buying", "no budget", "on hold",
                "found another", "unresponsive")


def classify_engagement(session, lead):
    """Evidence-only (Outreach/reply/status/Deal). Returns (engagement_class, reply_outcome). Never invents a
    reply. Auto-replies never count as engaged. 'archived' is admin-set, not derived here."""
    lid = lead.id
    has_deal = session.exec(select(func.count(Deal.id)).where(Deal.lead_id == lid)).one() if lid else 0
    if has_deal or lead.status == "won":
        return "customer", ("positive" if lead.accepted_at or lead.status == "won" else "neutral")

    inbound = session.exec(select(Outreach).where(Outreach.lead_id == lid, Outreach.direction == "in")).all() \
        if lid else []
    human_reply = None
    auto_only = False
    for o in inbound:
        if _AUTO_RE.search((o.subject or "") + " " + (o.body or "")):
            auto_only = True
        else:
            human_reply = o
    replied = bool(lead.buyer_replied_at) or human_reply is not None

    # invalid — only on explicit evidence
    lr = (lead.lost_reason or "").lower()
    if "not a real buyer" in lr or "wrong contact" in lr or lead.next_action_note == "wrong-contact":
        return "invalid", "none"

    qualified_stages = ("qualified", "interested", "pricing_requested", "quote_prepared", "quote_sent",
                        "negotiating")
    if lead.pipeline_stage in qualified_stages or lead.status == "negotiating":
        outcome = "positive" if lead.accepted_at else ("negative" if any(k in lr for k in _NEG_REASONS)
                                                        else "neutral")
        return "qualified", outcome

    if replied:
        if lead.accepted_at:
            outcome = "positive"
        elif any(k in lr for k in _NEG_REASONS) or lead.status == "lost":
            outcome = "negative"
        elif lead.next_action_note == "bounced":
            outcome = "bounced"
        else:
            outcome = "neutral"
        return "engaged", outcome

    # bounced with no human reply → contacted but bad email
    outbound = session.exec(select(func.count(Outreach.id)).where(
        Outreach.lead_id == lid, Outreach.direction == "out")).one() if lid else 0
    if outbound or lead.first_response_at:
        return "contacted", ("bounced" if lead.next_action_note == "bounced"
                             else ("auto_reply" if auto_only else "none"))
    return "prospect", "none"


# --------------------------------------------------------------------------- deduplication
def _company_signals(session, companies):
    """Build match indexes for a set of companies: domain/email/phone/name+country/name+city → [company_id]."""
    by_domain, by_email, by_phone, by_namec, by_namecity = {}, {}, {}, {}, {}
    contacts = {}
    if companies:
        ids = [c.id for c in companies]
        for ct in session.exec(select(Contact).where(Contact.company_id.in_(ids))).all():
            contacts.setdefault(ct.company_id, []).append(ct)
    for c in companies:
        if c.domain:
            by_domain.setdefault(c.domain, []).append(c.id)
        if c.name_normalized:
            by_namec.setdefault((c.name_normalized, c.country or ""), []).append(c.id)
            if c.city:
                by_namecity.setdefault((c.name_normalized, c.city.lower()), []).append(c.id)
        for ct in contacts.get(c.id, []):
            if ct.email_normalized:
                by_email.setdefault(ct.email_normalized, []).append(c.id)
            if ct.phone_normalized:
                by_phone.setdefault(ct.phone_normalized, []).append(c.id)
    return by_domain, by_email, by_phone, by_namec, by_namecity


def _pairs(ids):
    ids = sorted(set(ids))
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            yield ids[i], ids[j]


def scan_duplicates(session, tenant_id="__all__"):
    """Create conservative, tenant-scoped DuplicateCandidate rows. NEVER merges. Returns count created."""
    if tenant_id == "__all__":
        tenants = list({row for row in session.exec(select(Company.tenant_id).distinct()).all()})
    else:
        tenants = [tenant_id]
    created = 0
    for t in tenants:
        q = select(Company).where(Company.status == "active")
        q = q.where(Company.tenant_id.is_(None)) if t is None else q.where(Company.tenant_id == t)
        companies = session.exec(q).all()
        if len(companies) < 2:
            continue
        by_domain, by_email, by_phone, by_namec, by_namecity = _company_signals(session, companies)
        acc = {}  # (a,b) -> {signals:set, strong:bool, strength:int}

        def add(a, b, sig, strong, weight):
            key = (a, b) if a < b else (b, a)
            e = acc.setdefault(key, {"sig": set(), "strong": False, "strength": 0})
            e["sig"].add(sig)
            e["strong"] = e["strong"] or strong
            e["strength"] = max(e["strength"], weight)

        for dom, ids in by_domain.items():
            if 2 <= len(set(ids)) <= DOMAIN_SHARE_MAX:
                for a, b in _pairs(ids):
                    add(a, b, "domain_exact", True, 80)
        for em, ids in by_email.items():
            if len(set(ids)) >= 2:
                for a, b in _pairs(ids):
                    add(a, b, "email_exact", True, 95)
        for ph, ids in by_phone.items():
            if 2 <= len(set(ids)) <= PHONE_SHARE_MAX:
                for a, b in _pairs(ids):
                    add(a, b, "phone_exact", True, 85)
        for key, ids in by_namec.items():
            if len(set(ids)) >= 2:
                for a, b in _pairs(ids):
                    add(a, b, "name_country", False, 55)
        for key, ids in by_namecity.items():
            if len(set(ids)) >= 2:
                for a, b in _pairs(ids):
                    add(a, b, "name_city", False, 45)

        for (a, b), e in acc.items():
            strength = min(100, e["strength"] + 10 * (len(e["sig"]) - 1))
            if record_candidate(session, t, a, b, sorted(e["sig"]),
                                 "strong" if e["strong"] else "potential", strength):
                created += 1
    return created


def record_candidate(session, tenant_id, left_id, right_id, signals, match_type, strength):
    a, b = (left_id, right_id) if left_id < right_id else (right_id, left_id)
    ex = session.exec(select(DuplicateCandidate).where(
        DuplicateCandidate.left_id == a, DuplicateCandidate.right_id == b)).first()
    if ex:
        if ex.status == "open":     # refresh signals on an open candidate
            ex.signals = json.dumps(signals)
            ex.match_type = match_type
            ex.strength = strength
            session.add(ex)
        return False
    try:
        with session.begin_nested():
            session.add(DuplicateCandidate(tenant_id=tenant_id, left_id=a, right_id=b,
                                           signals=json.dumps(signals), match_type=match_type,
                                           strength=strength, status="open"))
        return True
    except IntegrityError:
        return False


# --------------------------------------------------------------------------- merge / unmerge (reversible)
def merge_companies(session, canonical_id, dup_id, actor):
    """Merge dup INTO canonical: reassign contacts/provenance/leads/suppliers/roles, archive the dup, record
    exactly what moved in an AuditLog (for reversibility). Never cross-tenant. Returns (ok, err)."""
    if canonical_id == dup_id:
        return False, "cannot merge a company into itself"
    canonical = session.get(Company, canonical_id)
    dup = session.get(Company, dup_id)
    if not canonical or not dup:
        return False, "company not found"
    if canonical.tenant_id != dup.tenant_id:
        return False, "refusing cross-tenant merge"
    if dup.status == "archived" or canonical.status == "archived":
        return False, "cannot merge an already-archived company"

    moved = {"contacts": [], "leads": [], "suppliers": [], "provenance": [], "roles": []}
    for ct in session.exec(select(Contact).where(Contact.company_id == dup_id)).all():
        ct.company_id = canonical_id
        ct.is_primary = False
        session.add(ct)
        moved["contacts"].append(ct.id)
    for p in session.exec(select(Provenance).where(
            Provenance.entity_type == "company", Provenance.entity_id == dup_id)).all():
        p.entity_id = canonical_id
        session.add(p)
        moved["provenance"].append(p.id)
    for ld in session.exec(select(Lead).where(Lead.company_id == dup_id)).all():
        ld.company_id = canonical_id
        session.add(ld)
        moved["leads"].append(ld.id)
    for sp in session.exec(select(Supplier).where(Supplier.company_id == dup_id)).all():
        sp.company_id = canonical_id
        session.add(sp)
        moved["suppliers"].append(sp.id)
    for r in session.exec(select(CompanyRole).where(CompanyRole.company_id == dup_id)).all():
        has = session.exec(select(CompanyRole).where(
            CompanyRole.company_id == canonical_id, CompanyRole.role == r.role)).first()
        if has:
            session.delete(r)
        else:
            r.company_id = canonical_id
            session.add(r)
            moved["roles"].append(r.role)

    dup.status = "archived"
    dup.merged_into_id = canonical_id
    session.add(dup)
    for dc in session.exec(select(DuplicateCandidate).where(
            ((DuplicateCandidate.left_id == dup_id) | (DuplicateCandidate.right_id == dup_id)))).all():
        if dc.status in ("open", "confirmed"):
            dc.status = "merged"
            dc.merged_into_id = canonical_id
            dc.reviewed_at = datetime.utcnow()
            session.add(dc)
    audit(session, actor, "company", canonical_id, "company_merge",
          {"dup": dup_id, "moved": moved}, tenant_id=canonical.tenant_id)
    return True, ""


def unmerge_companies(session, dup_id, actor):
    """Reverse the most recent merge of `dup_id`, restoring EXACTLY the rows that moved (post-merge additions
    to the canonical stay put). Returns (ok, err)."""
    dup = session.get(Company, dup_id)
    if not dup or dup.status != "archived" or not dup.merged_into_id:
        return False, "not a merged company"
    from .models import AuditLog
    log = session.exec(select(AuditLog).where(
        AuditLog.action == "company_merge", AuditLog.entity_id == dup.merged_into_id)
        .order_by(AuditLog.id.desc())).all()
    rec = next((json.loads(l.meta or "{}") for l in log if json.loads(l.meta or "{}").get("dup") == dup_id), None)
    if not rec:
        return False, "no merge record to reverse"
    moved = rec.get("moved", {})
    for cid in moved.get("contacts", []):
        c = session.get(Contact, cid)
        if c:
            c.company_id = dup_id
            session.add(c)
    for pid in moved.get("provenance", []):
        p = session.get(Provenance, pid)
        if p:
            p.entity_id = dup_id
            session.add(p)
    for lid in moved.get("leads", []):
        ld = session.get(Lead, lid)
        if ld:
            ld.company_id = dup_id
            session.add(ld)
    for sid in moved.get("suppliers", []):
        sp = session.get(Supplier, sid)
        if sp:
            sp.company_id = dup_id
            session.add(sp)
    for role in moved.get("roles", []):
        _add_role(session, dup, role)
    canonical_id = dup.merged_into_id
    dup.status = "active"
    dup.merged_into_id = None
    session.add(dup)
    for dc in session.exec(select(DuplicateCandidate).where(
            (DuplicateCandidate.left_id == dup_id) | (DuplicateCandidate.right_id == dup_id))).all():
        if dc.status == "merged":
            dc.status = "open"
            dc.merged_into_id = None
            session.add(dc)
    audit(session, actor, "company", canonical_id, "company_unmerge", {"dup": dup_id}, tenant_id=dup.tenant_id)
    return True, ""
