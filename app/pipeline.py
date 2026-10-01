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
_FOLD_CHARS_CASED = str.maketrans({"ł": "l", "Ł": "L", "ø": "o", "Ø": "O", "æ": "ae", "Æ": "Ae", "œ": "oe",
                                   "Œ": "Oe", "ß": "ss", "đ": "d", "Đ": "D", "ð": "d", "Ð": "D", "þ": "th",
                                   "Þ": "Th", "ı": "i"})


def _fold(s, keep_case=False):
    """Lower-case, accent-free, whitespace-collapsed — the one form needles and text are compared in, so 'Kraków',
    'KRAKOW' and 'krakow' all match. keep_case=True folds the same way but keeps upper/lower case (proper nouns)."""
    s = str(s or "")
    s = s.translate(_FOLD_CHARS_CASED) if keep_case else s.lower().translate(_FOLD_CHARS)
    s = unicodedata.normalize("NFKD", s)
    return " ".join("".join(ch for ch in s if not unicodedata.combining(ch)).split())


# a country (the seller already sees it) or a generic place word in a buyer's name/city field identifies nobody
_NOT_A_NEEDLE = ({_fold(n) for n in _COUNTRY_NAMES.values()} | {_fold(n) for n in _NAME_ISO}
                 | {"usa", "uk", "ksa", "england", "scotland", "wales", "northern ireland", "great britain", "czechia",
                    "holland", "america", "europe", "middle east", "gcc", "asia", "africa", "north america",
                    "south america", "latin america", "nationwide", "national", "worldwide", "international",
                    "global", "online", "multiple", "various", "several", "head office", "headquarters", "branches",
                    "multi-location", "countrywide", "unknown"})
# region-level values stored as a buyer's "city" (provinces, states, counties, regions): a region names nobody, and
# 'buyers across Ontario' is ordinary wording. Folded forms. Names that are also a city ('Quebec', 'Victoria', 'Puebla',
# 'Durham', 'Surrey', 'Münster') are left out on purpose: in a city field they stay a city.
_REGIONS = {
    # Canada
    "alberta", "british columbia", "manitoba", "new brunswick", "newfoundland", "newfoundland and labrador",
    "nova scotia", "ontario", "prince edward island", "saskatchewan", "yukon", "nunavut",
    "northwest territories",
    # United States
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut", "florida",
    "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine", "maryland",
    "massachusetts", "michigan", "minnesota", "mississippi", "missouri", "montana", "nebraska", "nevada",
    "new hampshire", "new jersey", "new mexico", "north carolina", "north dakota", "ohio", "oklahoma", "oregon",
    "pennsylvania", "rhode island", "south carolina", "south dakota", "tennessee", "texas", "utah", "vermont",
    "virginia", "washington state", "west virginia", "wisconsin", "wyoming",
    # Australia / New Zealand
    "new south wales", "queensland", "south australia", "western australia", "tasmania",
    "northern territory", "australian capital territory", "northland", "waikato", "bay of plenty",     "otago", "southland", "taranaki", "manawatu", "hawke's bay", "hawkes bay",
    # Mexico
    "baja california", "baja california sur", "chiapas", "coahuila",
    "guerrero", "jalisco", "michoacan", "morelos", "nayarit",
    "nuevo leon", "quintana roo", "sinaloa", "sonora", "tabasco",
    "tamaulipas", "yucatan",     # South Africa
    "gauteng", "western cape", "eastern cape", "northern cape", "kwazulu-natal", "kwazulu natal", "free state",
    "limpopo", "mpumalanga", "north west",
    # United Kingdom / Ireland
    "midlands", "west midlands", "east midlands", "yorkshire", "north yorkshire", "south yorkshire",
    "west yorkshire", "east yorkshire", "merseyside", "cheshire", "derbyshire", "dorset", "essex",     "leicestershire", "staffordshire", "sussex", "east sussex", "west sussex", "worcestershire",
    "lancashire", "devon", "hampshire", "hertfordshire", "berkshire", "buckinghamshire",
    "oxfordshire", "gloucestershire", "warwickshire", "nottinghamshire", "lincolnshire",     "cumbria", "northumberland", "somerset", "wiltshire", "shropshire", "herefordshire", "cambridgeshire",
    "northamptonshire", "bedfordshire", "tyne and wear", "leinster", "connacht", "ulster",
    # continental Europe
    "flanders", "vlaanderen", "wallonia", "wallonie", "bavaria", "bayern", "baden-wurttemberg", "hesse", "hessen",
    "lower saxony", "niedersachsen", "north rhine-westphalia", "nordrhein-westfalen", "nrw",
    "rhineland-palatinate", "rheinland-pfalz", "saarland", "saxony", "sachsen", "saxony-anhalt", "sachsen-anhalt",
    "schleswig-holstein", "thuringia", "thuringen", "mecklenburg-vorpommern",
    "ile-de-france", "normandy", "normandie", "brittany", "bretagne", "occitanie", "nouvelle-aquitaine",
    "grand est", "hauts-de-france", "provence", "auvergne-rhone-alpes", "pays de la loire", "vendee", "corsica",
    "corse", "catalonia", "catalunya", "cataluna", "andalusia", "andalucia", "galicia", "basque country",
    "pais vasco", "aragon", "castilla y leon", "castilla-la mancha", "asturias", "cantabria", "extremadura",
    "navarra", "la rioja", "canary islands", "canarias", "balearic islands", "baleares", "lombardy", "lombardia",
    "veneto", "piedmont", "piemonte", "emilia-romagna", "tuscany", "toscana", "lazio", "campania", "sicily",
    "sicilia", "sardinia", "sardegna", "puglia", "apulia", "calabria", "liguria", "marche", "abruzzo", "umbria",
    "friuli-venezia giulia", "trentino-alto adige", "basilicata", "molise", "north holland", "noord-holland",
    "south holland", "zuid-holland", "north brabant", "noord-brabant", "gelderland", "overijssel",     "friesland", "drenthe", "zeeland", "flevoland", "silesia", "masovia", "mazowieckie", "malopolska",
    "malopolskie", "wielkopolska", "wielkopolskie", "pomerania", "pomorskie", "slaskie", "ilfov", "transylvania",
    "crete", "attica", "attiki", "peloponnese", "istria", "dalmatia", "slavonia", "bohemia", "moravia", "jutland",
    "zealand", "scania", "skane", "lapland", "tyrol", "tirol", "styria", "carinthia", "valais", "ticino",
    # Middle East
    "samaria", "judea", "galilee", "negev", "eastern province", "western province", "central province",
}
# words that make a region of whatever follows/precedes them ('Co. Cork', 'Estado de México', 'Luzern region')
_REGION_MARKERS = {"county", "co", "province", "provincia", "state", "estado", "region", "regione", "regio",
                   "greater", "shire", "oblast", "voivodeship", "canton", "kanton", "departement", "department",
                   "prefecture", "governorate"}
# descriptors around a place in the city field — dropped before it becomes a needle ('Dublin HQ' → 'Dublin')
_PLACE_FILLER = {"hq", "head", "office", "offices", "area", "areas", "industrial", "branch", "branches", "multi",
                 "multiple", "multi-branch", "nationwide", "national", "chain", "showroom", "showrooms",
                 "distribution", "centre", "centres", "center", "centers", "warehouse", "warehouses", "plus",
                 "metro", "metropolitan", "kingdom-wide", "countrywide", "qld", "nsw", "vic", "tas", "se-qld"}
_DIRECTIONS = {"north", "south", "east", "west", "northern", "southern", "eastern", "western", "central", "upper",
               "lower"}
# trade words that are also some buyer's brand ('Fastener Agencies' on fastener.co.nz) or whole name ('Fixings') —
# never a needle on their own, or every update about the product would be blocked
_TRADE_WORDS = {"fastener", "fasteners", "fastening", "fixing", "fixings", "anchor", "anchors", "bolt", "bolts",
                "screw", "screws", "tool", "tools", "hardware", "supply", "supplies", "building", "trade", "trading",
                "steel", "metal", "metals", "industrial", "direct", "express", "group", "global", "home", "house",
                "drywall", "plaster", "wholesale", "united", "general", "power", "prime", "star", "best", "royal"}
# a buyer name made ONLY of such words ('Plaster Wholesalers', 'Construction Accessories') is ordinary wording, not
# an identity. A legal form keeps it a needle: 'Plaster Wholesalers Ltd' never appears by accident.
_GENERIC_NAME_WORDS = _TRADE_WORDS | {
    "wholesaler", "wholesalers", "distributor", "distributors", "distribution", "distributing", "construction",
    "constructions", "accessories", "accessory", "product", "products", "material", "materials", "builder",
    "builders", "buildings", "merchant", "merchants", "depot", "centre", "center", "store", "stores", "shop", "shops",
    "mart", "market", "import", "imports", "importer", "importers", "export", "exports", "exporter", "exporters",
    "international", "enterprise", "enterprises", "partners", "sales", "agency", "agencies", "plus", "pro", "pros",
    "solution", "solutions", "service", "services", "system", "systems", "industry", "industries", "the", "and",
    "of", "for", "plasterboard", "gypsum", "interior", "interiors", "ceiling", "ceilings", "wall", "walls",
    "partition", "partitions", "insulation", "lining", "drylining", "timber", "paint", "paints", "diy", "total",
    "quality", "value", "discount", "city", "national", "nationwide", "regional", "local", "new", "first", "one",
    "all", "top", "super", "mega", "smart", "easy", "euro", "european", "world", "universal", "standard", "modern",
    "professional", "trader", "traders", "supplier", "suppliers", "equipment", "machinery", "ironmongery",
    "ironmongers", "technical", "technics", "technology", "fix", "fixer", "fixers", "specialist", "specialists",
    "contractor", "contractors", "commercial", "retail", "online", "warehouse", "outlet", "hub"}
# words that make a contact field a role, not a person ('Sales Team', 'Purchasing Manager')
_ROLE_WORDS = {"sales", "purchasing", "purchase", "purchases", "procurement", "buyer", "buyers", "buying", "manager",
               "management", "team", "department", "dept", "office", "info", "information", "admin", "administration",
               "accounts", "accounting", "customer", "customers", "service", "services", "support", "contact",
               "general", "export", "import", "director", "owner", "ceo", "cfo", "coo", "cto", "md", "gm",
               "desk", "enquiries", "inquiries", "orders", "order", "marketing", "logistics", "operations", "ops",
               "warehouse", "store", "shop", "branch", "reception", "hr", "technical", "engineering", "category",
               "commercial", "secretary", "assistant", "coordinator", "officer", "executive", "representative",
               "rep", "agent", "founder", "president", "chairman",
               "account", "regional", "national", "international", "global", "company", "unknown", "n/a", "na",
               "staff", "trade", "counter", "manufacturing", "product", "products", "supply", "chain", "the", "of",
               "and", "for"}
_HONORIFICS = {"mr", "mrs", "ms", "miss", "mx", "dr", "eng", "ing", "sir", "madam", "herr", "frau", "sig", "sra",
               "sr", "prof", "mme", "mlle", "m", "dipl"}
_EMAIL_RE = _PII_PATTERNS[0][1]
_DIGIT_RUN = re.compile(r"\d[\d\s().\-/]*\d")
_MIN_NEEDLE = 4                 # shorter names/cities ('ACE', 'ON') would match ordinary text
_MIN_PHONE_DIGITS = 7           # a phone matches on its last 7 digits, however it is written
_CITY_SHARED = 3                # a city where 3+ DISTINCT buyers of the request are singles none of them out
# towns whose name is also an everyday word: only a capitalised mention counts ('Split', not 'split the order';
# 'Stone', not 'hold in stone'). Every other city matches in any case, links included.
_COMMON_WORD_PLACES = {"split", "stone", "root", "alle", "sala", "leek", "hull", "concord", "corona", "phoenix",
                       "nice", "bath", "reading", "mobile", "hope", "orange", "march", "wells", "mold", "deal",
                       "sandwich", "buffalo", "eagle", "como", "sale", "street", "bury", "ware", "send", "battle",
                       "rugby", "derby", "sandy", "marathon", "eden", "essen", "halle", "toro", "cheddar"}
# words a capitalised ('title') name match ignores: 'the Bolt Store' is still 'The Bolt Store'
_SMALL_WORDS = {"the", "and", "of", "for", "de", "la", "le", "del", "da", "di", "von", "van", "der", "den", "y", "e",
                "et", "und"}
# how a needle matches: 'any' — whole words, any case; 'proper' — the match starts with a capital (a surname, a town
# that is also a word); 'title' — every word of the match is capitalised (a buyer name made only of generic words:
# 'Plaster Wholesalers confirmed' is a name, 'plaster wholesalers in the UK' is prose)
_PROPER, _TITLE, _ANY = "proper", "title", "any"
_STRENGTH = {_PROPER: 0, _TITLE: 1, _ANY: 2}          # 'any' blocks the most


def _host_label(host):
    """'richelieu.com' -> 'richelieu', 'shop.toolbank.co.uk' -> 'toolbank'."""
    parts = (host or "").split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "ac", "gov", "edu") and len(parts[-1]) == 2:
        return parts[-3]
    return parts[-2] if len(parts) >= 2 else ""


# words whose trailing period is an abbreviation's, never the end of a sentence ('St. Gallen', 'Sp. z o.o.')
_ABBREVIATIONS = {"st", "ste", "mt", "ft", "pt", "co", "sp", "mr", "mrs", "ms", "dr", "inc", "ltd", "corp", "no", "nr",
                  "jr", "sr", "bros", "intl", "int", "dept", "ave", "rd", "av", "gen", "ing", "eng", "prof", "est",
                  "pty", "plc", "llc", "bv", "nv", "sa", "srl", "spa", "gmbh", "ag", "kg", "ab", "oy", "as", "aps",
                  "lda", "ltda", "sl", "slu", "sro", "doo", "kft", "fze", "wll", "zrt", "nyrt", "ou", "uab",
                  "brdr", "gebr", "hnos", "cia", "ets", "sto", "spol", "nchfg", "mfg", "sdn", "bhd", "pvt", "pte",
                  "tic", "san", "sti"}
_WORD_DOT = re.compile(r"(\w+)\.(?=\s|$)")


def _words_form(s):
    """Folded text compared word by word: '&' is 'and', and an abbreviation's or an inner period doesn't matter
    ('St. Gallen' = 'St Gallen', 'P.H.U. Eurobolt', 'Sp. z o.o.'), while a sentence's period stays a boundary — 'the
    anchor bolt. It ships' never reads as a buyer called 'Bolt It'."""
    s = (s or "").replace("&", " and ")
    s = _WORD_DOT.sub(lambda m: m.group(1) + (" " if len(m.group(1)) <= 2 or m.group(1).lower() in _ABBREVIATIONS
                                              else " \x00 "), s)          # \x00 = a sentence boundary, never a word
    return " ".join(s.replace(".", " ").split())


def _city_places(value):
    """The single places a buyer's city field names, as written ('Dublin HQ' → ['Dublin'], 'St. Gallen' →
    ['St. Gallen'], 'Montreal, QC' → ['Montreal', 'QC']). Notes in brackets, postcodes, descriptors ('HQ', 'area',
    'branches'), regions, provinces, counties and countries ('Western Canada') are not places — they never single a
    buyer out."""
    out = []
    city = re.sub(r"\([^()]*\)", " ", value or "")                          # "(14 branches across …)"
    for part in re.split(r"[,/;&|+]+|\s-\s|\band\b|\bor\b", city, flags=re.I):
        words = re.sub(r"\S*\d\S*", " ", part).split()                       # postcodes ("Dublin 22")
        words = [w for w in words if w.strip(" .,;:-") and _fold(w.strip(" .,;:-")) not in _PLACE_FILLER]
        folded = [_fold(w.strip(" .,;:-")) for w in words]
        if not words or any(f in _REGION_MARKERS for f in folded):
            continue
        core = " ".join(f for f in folded if f not in _DIRECTIONS)
        if not core or core in _NOT_A_NEEDLE or core in _REGIONS or " ".join(folded) in _REGIONS:
            continue
        out.append(" ".join(words).strip(" ,;:-"))                         # inner periods kept ('St. Gallen')
    return out


def _people(value):
    """[(full name, surname)] of the persons a contact-name field names. Honorifics and role words are dropped; a
    person is 2+ words that are not all generic, or one word after an honorific ('Mr. Tremblay' → ('', 'Tremblay')).
    The surname is the last word, or the one written in capitals ('TREMBLAY Marc'). A role ('Sales Team',
    'Purchasing Manager North East'), an address or a lone first name names nobody."""
    from .campaign_render import _LEGAL_TAIL           # lazy, like display_company below
    out = []
    text = re.sub(r"\([^()]*\)", " ", value or "")
    for seg in re.split(r"[,;/|]+|\s[-–]\s|\band\b|&", text):
        raw = [w.strip(" .,:'\"") for w in seg.split()]
        raw = [w for w in raw if w]
        honorific = any(_fold(w).strip(".") in _HONORIFICS for w in raw)
        words = [w for w in raw if _fold(w).strip(".") not in _HONORIFICS | _ROLE_WORDS]
        folded = [_fold(w) for w in words]
        if (not words or any("@" in w or any(ch.isdigit() for ch in w) for w in words)
                or all(f in _GENERIC_NAME_WORDS | _DIRECTIONS for f in folded)
                or " ".join(folded) in _NOT_A_NEEDLE | _REGIONS):
            continue
        if len(words) == 1:
            if honorific:
                out.append(("", words[0]))
            continue
        caps = [w for w in words if len(w) > 1 and w.isupper()]
        last = caps[0] if caps and len(caps) < len(words) else words[-1]
        generic_last = _fold(last) in _GENERIC_NAME_WORDS | _DIRECTIONS or _LEGAL_TAIL.search(" " + last)
        out.append((" ".join(words), "" if generic_last else last))
    return out


def _tied_to_domain(name, labels) -> bool:
    """The name IS the buyer's own web/mail domain, so it names them in any case: its first word ('Richelieu
    Hardware' ↔ richelieu.com), its words run together ('The Bolt Store' ↔ theboltstore.ie, 'Screws & Fixings' ↔
    screwsandfixings…, also inside a longer label: 'Fixings Direct' ↔ ukfixingsdirect.com), a long prefix of them
    ('Trade Direct Wholesale' ↔ tradedirect…) or its initials ('Fastener Agencies' ↔ fa.co.za)."""
    ws = re.findall(r"\w+", _words_form(_fold(name)))
    core = [w for w in ws if w != "the"]
    if not core or not labels:
        return False
    joined = {"".join(ws), "".join(core), "".join(w for w in core if w != "and")}
    initials = "".join(w[0] for w in core if w != "and")
    for lab in labels:
        lab = lab.replace("-", "")
        if (lab == core[0] or lab in joined or (len(lab) >= 6 and any(j.startswith(lab) for j in joined))
                or (len(core) > 1 and (lab == initials or any(len(j) >= 8 and j in lab for j in joined)))):
            return True
    return False


def _distinct_buyers(groups) -> int:
    """How many distinct buyers a list of identity-key sets describes — two rows sharing any key (Trade Network
    company, own web/mail domain, brand) are one buyer, however many times it was loaded."""
    comps = []
    for keys in groups:
        merged, rest = set(keys), []
        for c in comps:
            if c & merged:
                merged |= c
            else:
                rest.append(c)
        comps = rest + [merged]
    return len(comps)


def denylist_needles(leads, contacts=(), request_id=None):
    """[(kind, folded needle, shown, mode)] for the given buyers (+ their Trade Network contact persons) — what a
    seller-visible text must never contain. A city where _CITY_SHARED+ distinct buyers of the request (`request_id`;
    all `leads` when None) are is not a needle: it can't single one out ('CIF Dubai')."""
    from .campaign_render import display_company       # lazy: keeps this module dependency-light (no cycles)
    from .company_service import normalize_domain
    out, seen = [], {}
    info = []
    for ld in leads:
        name = getattr(ld, "buyer_company", "") or ""
        brand = display_company(name)                           # "Richelieu Hardware Ltd" -> "Richelieu Hardware"
        emails = [em.lower() for em in _EMAIL_RE.findall(getattr(ld, "email", "") or "")]
        hosts = {normalize_domain(em) for em in emails} | {normalize_domain(getattr(ld, "website", "") or "")}
        hosts.discard("")                                       # '' = gmail & co (never a buyer identity)
        labels = {_host_label(h) for h in hosts} - {""}
        keys = {("host", lab) for lab in labels} | ({("brand", _fold(brand))} if _fold(brand) else set())
        if getattr(ld, "company_id", None):
            keys.add(("co", ld.company_id))
        info.append((ld, name, brand, emails, hosts, labels, keys, _city_places(getattr(ld, "dest_city", "") or "")))
    by_city = {}
    for ld, _n, _b, _e, _h, _l, keys, places in info:
        if request_id is None or getattr(ld, "request_id", None) == request_id:
            for place in {_fold(p) for p in places}:
                by_city.setdefault(_words_form(place), []).append(keys)
    shared = {c for c, groups in by_city.items() if _distinct_buyers(groups) >= _CITY_SHARED}

    def add(kind, value, mode=_ANY):
        shown = " ".join(str(value or "").split()).strip(" ,.;:-")
        folded = _fold(shown)
        if kind == "buyer phone":
            ok = len(folded) >= _MIN_PHONE_DIGITS
        else:
            ok = (len(folded) >= _MIN_NEEDLE and folded not in _NOT_A_NEEDLE
                  and not folded.replace(" ", "").isdigit()
                  and not (kind == "buyer city" and _words_form(folded) in shared))
        if not ok:
            return
        i = seen.get((kind, folded))
        if i is None:
            seen[(kind, folded)] = len(out)
            out.append((kind, folded, shown, mode))
        elif _STRENGTH[mode] > _STRENGTH[out[i][3]]:     # two buyers share it: the strictest protection wins
            out[i] = (kind, folded, out[i][2], mode)

    def add_name(value, labels):
        words = re.findall(r"\w+", _fold(value))
        if words and all(w in _GENERIC_NAME_WORDS for w in words):
            if len(words) == 1 and words[0] in _TRADE_WORDS:
                return                                          # 'Fixings' alone is the product, not a buyer
            add("buyer name", value, _ANY if _tied_to_domain(value, labels) else _TITLE)
        else:
            add("buyer name", value)

    def add_person(value):
        for full, surname in _people(value):
            add("buyer contact", full)
            add("buyer surname", surname, _PROPER)

    def add_phone(value):
        for run in _DIGIT_RUN.findall(value or ""):
            add("buyer phone", re.sub(r"\D", "", run)[-_MIN_PHONE_DIGITS:])

    for ld, name, brand, emails, hosts, labels, _keys, places in info:
        add_name(name, labels)
        add_name(brand, labels)
        for inner in re.findall(r"\(([^()]*)\)", name):         # "IHL Canada (Investments Hardware Ltd.)"
            add_name(display_company(inner), labels)
        for em in emails:
            add("buyer email", em)
        for host in sorted(hosts):
            add("buyer website", host)
        # the brand alone ("Richelieu") when it IS the buyer's own web/mail domain — strong evidence it names them
        first = re.sub(r"[^\w-]+", "", (brand.split() or [""])[0])
        if len(brand.split()) > 1 and _fold(first) in labels:
            add_name(first, labels)
        for place in places:
            add("buyer city", place, _PROPER if _words_form(_fold(place)) in _COMMON_WORD_PLACES else _ANY)
        add_person(getattr(ld, "contact_name", "") or "")
        add_phone(getattr(ld, "phone", "") or "")
    for ct in contacts:                                         # the buyers' Trade Network contact persons
        add_person(getattr(ct, "name", "") or "")
        for em in _EMAIL_RE.findall(getattr(ct, "email", "") or ""):
            add("buyer email", em.lower())
        add_phone(getattr(ct, "phone", "") or "")
    return out


def _needle_pattern(needle):
    """(regex, plain words) for a name/city/person needle. A period INSIDE the needle is part of the name, never a
    sentence end, so the text may write it or not ('Brdr. A & O Johansen' = 'Brdr A & O Johansen'); a sentence
    period in the TEXT still separates words the needle runs together ('bolt. It' is never 'Bolt It')."""
    tokens = _words_form(needle).split(" ")
    words = [w for w in tokens if w != "\x00"]
    if not words:
        return "", ""
    pat = re.escape(tokens[0]) if tokens[0] != "\x00" else ""
    for tok in tokens[1:]:
        pat += r"(?: \x00)?" if tok == "\x00" else (" " if pat else "") + re.escape(tok)
    return r"(?<!\w)" + pat + r"(?!\w)", " ".join(words)


def _capitalised(span, every_word):
    words = [w for w in span.split() if _fold(w) not in _SMALL_WORDS] if every_word else span.split()[:1]
    return bool(words) and all(not w[:1].islower() for w in words)


def denylist_hits(text, needles):
    """Hits of `needles` (from denylist_needles) in `text`: names/cities/persons as whole words (periods and '&'
    ignored), emails/hosts as whole addresses, phones by digits whatever the separators. A 'proper'/'title' needle
    counts only where the text writes it capitalised."""
    t = text or ""
    hay = _fold(t)
    words_hay = _words_form(hay)
    words_plain = words_hay.replace(" \x00", "")          # for the quick pre-check only
    cased = None
    runs = [re.sub(r"\D", "", r) for r in _DIGIT_RUN.findall(t)]
    hits = []
    for n in needles:
        kind, needle, shown = n[:3]
        mode = n[3] if len(n) > 3 else _ANY
        if kind == "buyer phone":
            found = any(needle in r for r in runs)
        elif kind in ("buyer email", "buyer website"):
            found = needle in hay and re.search(r"(?<![\w-])" + re.escape(needle) + r"(?![\w-])", hay) is not None
        else:
            pattern, plain = _needle_pattern(needle)
            if not plain or plain not in words_plain:
                found = False
            elif mode == _ANY:
                found = re.search(pattern, words_hay) is not None
            else:
                cased = _words_form(_fold(t, keep_case=True)) if cased is None else cased
                found = any(_capitalised(m.group(0), mode == _TITLE) for m in re.finditer(pattern, cased, re.I))
        if found:
            hits.append({"kind": kind, "match": shown[:80]})
    return hits


def hits_summary(hits, limit=5):
    """What an admin must remove, for a flash message: 'buyer city "Split", email "a@b.example"' (admin-only)."""
    out, seen = [], set()
    for h in hits:
        key = (h.get("kind"), (h.get("match") or "").lower())
        if key not in seen:
            seen.add(key)
            out.append(f'{h.get("kind")} "{(h.get("match") or "")[:60]}"')
    more = len(out) - limit
    return ", ".join(out[:limit]) + (f" (+{more} more)" if more > 0 else "")


def request_denylist(session, sr):
    """Needles for every buyer a text about request `sr` must never name: the request's own buyers plus the seller's
    other managed buyers (still scoped to that one seller — never the whole database), with their Trade Network
    contact persons. Whether a city is shared is judged on the request's own buyers."""
    from .models import Contact
    cond = Lead.request_id == sr.id
    if sr.owner_id:
        cond = cond | ((Lead.managed == True) & (Lead.seller_id == sr.owner_id))  # noqa: E712
    leads = session.exec(select(Lead).where(cond)).all()
    company_ids = sorted({ld.company_id for ld in leads if ld.company_id})
    contacts = []
    for i in range(0, len(company_ids), 500):                   # SQLite's bound-parameter limit
        contacts += session.exec(select(Contact).where(Contact.company_id.in_(company_ids[i:i + 500]))).all()
    return denylist_needles(leads, contacts, request_id=sr.id)


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
