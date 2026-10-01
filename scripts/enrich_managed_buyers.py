"""Find emails for a request's email-less CONFIDENTIAL managed buyers on their OWN websites — reviewed, never blind
(Phase 12). Three steps, each run by hand and each one refusing on any surprise:

  scan    READ-ONLY database. Reads every candidate's own site — robots.txt honoured, neutral User-Agent, at most
          --max-pages pages, --pause seconds between requests — and classifies what it found: accept / review /
          reject. The review CSV goes to STDOUT only (progress + counts to stderr); nothing is written on the server.
            ssh root@<host> 'docker exec go4it-app python scripts/enrich_managed_buyers.py scan \
                --request SR-202608-0001 --expect-candidates 462 --offset 0 --limit 120' > debug/enrich_req1_a.csv
          (debug/ is gitignored — never keep a review CSV under docs/prospects: save.sh would commit and push it)
  apply   The founder marks approve=y on the rows to use; --include-auto also takes every 'accept' row (an explicit
          approve=n still wins). ONE short transaction with the network locked. Every row is re-checked first (same
          request + seller, website and external_id as in the CSV — any mismatch rolls the whole run back), then per
          row (email still blank, valid, not suppressed/bounced, not on another buyer or any campaign) before the
          email is written with a 'Web-enrich (reviewed…)' Activity. --mark-misses notes every other row as tried,
          so no automatic enrichment ever fills those buyers unreviewed. --dry-run first (scan chunks may be
          concatenated — repeated header lines are skipped):
            cat debug/enrich_req1_*.csv | ssh root@<host> 'docker exec -i go4it-app python \
                scripts/enrich_managed_buyers.py apply --request SR-202608-0001 --stdin --include-auto --mark-misses \
                --dry-run'
  enrol   Adds EXACTLY the applied buyers (the 'Web-enrich (reviewed…)' marker, not yet in the campaign) to the
          request's running/paused campaign: refuses unless the eligible set is that set and its size is --expected.
          New recipients get higher ids than every existing one, so the worker sends them AFTER everyone already
          queued, on the campaign's current sequence version.
            ssh root@<host> 'docker exec go4it-app python scripts/enrich_managed_buyers.py enrol \
                --request SR-202608-0001 --campaign 33 --expected <N> --dry-run'

Refuses unless the request is a buyer hunt whose buyers are all managed, admin-owned (owner_id NULL) and FOR the
request's seller. Never notifies anyone; the seller only ever sees anonymized stage counts.
"""
import argparse
import csv
import io
import os
import re
import sys
from collections import Counter

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from sqlalchemy import func                                                    # noqa: E402
from sqlmodel import Session, or_, select                                      # noqa: E402

from app import campaign_service as CAMP                                       # noqa: E402
from app import enrich_service as ES                                           # noqa: E402
from app import suppression as SUP                                             # noqa: E402
from app.company_service import GENERIC_DOMAINS, normalize_name                # noqa: E402
from app.db import engine                                                      # noqa: E402
from app.models import (Activity, BounceRecord, Campaign, CampaignRecipient, Lead, Outreach,  # noqa: E402
                        Suppression)
from scripts.campaign_dryrun import lock_network, readonly_engine              # noqa: E402
from scripts.load_managed_buyers import find_request                           # noqa: E402

COLS = ("lead_id", "external_id", "anon_ref", "country", "company", "website", "final_url", "decision", "reasons",
        "email", "alt_emails", "found_on", "phone", "name_score", "approve")
REVIEWED = "Web-enrich (reviewed"           # every applied address carries this marker — enrol keys on it
MISS = "Web-enrich: no usable contact"
NAME_OK = 75                                 # name_score needed for 'accept'
# Company inboxes good enough to use without a human look (with a same-domain site and a name match), in the
# buyers' own languages: sales / info / office / orders / purchasing …
ROLE_OK = ("info", "sales", "export", "office", "biuro", "kontakt", "verkauf", "vendite", "ventas", "comercial",
           "myynti", "salg", "obchod", "sprzedaz", "zamowienia", "orders", "order", "purchasing", "procurement",
           "einkauf", "zakupy", "hello", "contact", "contactus", "enquiries", "enquiry", "inquiries", "inquiry",
           "mail", "post", "postmottak", "kontor", "trade", "commercial", "vente", "ventes", "verkoop", "handel",
           "compras", "acquisti", "ordini", "pedidos", "bestellung")
_ROLE_OK_RE = re.compile(r"^(?:%s)(?:[._-](?:[a-z]{2,3}|\d{1,2})|\d{1,2})?$" % "|".join(ROLE_OK))
_SUPPORT_RE = re.compile(r"^(?:support|service|customer|kunden|kund|serwis|help|marketing|shop|webshop|e-?shop|sklep|"
                         r"online|e-?commerce|admin|tech|reklam)")
# legal forms normalize_name keeps but a name match must ignore ('Sp. z o.o.', 's.r.o.', 'Pty Ltd' …)
_NAME_STOP = {"sp", "zoo", "spol", "sro", "doo", "kft", "zrt", "bt", "aps", "asa", "oyj", "pty", "limited", "lda",
              "sas", "eurl", "snc", "vof", "ohg", "ug", "wll", "pjsc", "slu", "sau", "ae", "epe", "ike", "plc",
              "company", "holding", "holdings"}
# trade / place words a domain may share with an unrelated buyer — never enough on their own for a name match
_TRADE_WORDS = {"steel", "metal", "metals", "hardware", "tools", "tool", "building", "builders", "construction",
                "fasteners", "fixings", "fixing", "supplies", "supply", "technical", "industrial", "industries",
                "materials", "distribution", "wholesale", "timber", "drywall", "ceiling", "warehouse", "bolts",
                "screws", "anchors", "home", "house", "shop", "store", "center", "centre", "online", "direct", "solutions",
                "services", "systems", "products", "hurtownia", "schrauben", "deutschland", "polska", "austria",
                "france", "schweiz", "suisse", "italia", "espana", "nederland", "ireland", "australia", "zealand",
                "africa", "ksa", "uae", "brands", "brdr", "brdrene"}
_PHONE_RE = re.compile(r"^(?:\+\d{7,15}|0\d{6,14})$")
_YES = ("y", "yes", "1", "true", "x", "ok")
_NO = ("n", "no", "0", "false", "skip")


def _err(msg):
    print(msg, file=sys.stderr)


def refuse_reason(session, sr):
    if sr is None:
        return "request not found"
    if sr.request_type != "buyer_hunt":
        return f"{sr.tracking_code} is a '{sr.request_type}' request, not a buyer hunt"
    if sr.status in ("rejected", "cancelled"):
        return f"{sr.tracking_code} is {sr.status}"
    if not sr.owner_id:
        return f"{sr.tracking_code} has no seller"
    stray = session.exec(select(func.count()).where(
        Lead.request_id == sr.id,
        or_(Lead.managed == False, Lead.managed.is_(None), Lead.owner_id.is_not(None),    # noqa: E712
            Lead.seller_id.is_(None), Lead.seller_id != sr.owner_id))).one()
    if stray:
        return f"{stray} buyer(s) of {sr.tracking_code} are not confidential managed buyers of its seller"
    return ""


def _blank(col):
    return func.trim(func.coalesce(col, "")) == ""


# --------------------------------------------------------------------------------------------------- scan
def scan_candidates(session, sr) -> list:
    """The request's confidential buyers with a website and no email that were never contacted, enrolled or bounced
    — ordered by id, so --offset/--limit chunks are stable. Plain dicts: no session is held during the network I/O."""
    stmt = select(Lead).where(
        Lead.request_id == sr.id, Lead.managed == True, Lead.owner_id.is_(None),            # noqa: E712
        Lead.seller_id == sr.owner_id, _blank(Lead.email), ~_blank(Lead.website),
        Lead.id.not_in(select(CampaignRecipient.lead_id).where(CampaignRecipient.lead_id.is_not(None))),
        Lead.id.not_in(select(Outreach.lead_id).where(Outreach.direction == "out")),
        Lead.id.not_in(select(BounceRecord.lead_id).where(BounceRecord.lead_id.is_not(None)))).order_by(Lead.id)
    return [{"id": ld.id, "external_id": ld.external_id or "", "anon_ref": ld.anon_ref or "",
             "country": ld.dest_country or "", "company": ld.buyer_company or "", "website": ld.website or "",
             "notes": ld.notes or ""} for ld in session.exec(stmt).all()]


def load_context(session, sr) -> dict:
    """Everything classify() checks an address against, read ONCE before any site is fetched."""
    norm = SUP.normalize_email
    lead_emails = {norm(e) for e in session.exec(select(Lead.email).where(~_blank(Lead.email))).all()}
    rcpt = session.exec(select(CampaignRecipient.to_email, CampaignRecipient.tenant_id)
                        .where(~_blank(CampaignRecipient.to_email))).all()
    suppressed = {x.email_normalized for x in session.exec(select(Suppression).where(
        Suppression.active == True)).all()                                                   # noqa: E712
        if x.scope == "platform" or (x.scope == "tenant" and x.tenant_id == sr.owner_id)}
    taken = {norm(e) for e, tenant in rcpt if tenant == sr.owner_id}
    taken |= {norm(e) for e in session.exec(select(Lead.email).where(Lead.request_id == sr.id,
                                                                      ~_blank(Lead.email))).all()}
    return {"lead_emails": lead_emails, "recipient_emails": {norm(e) for e, _ in rcpt}, "suppressed": suppressed,
            "bounced": {e for e in session.exec(select(BounceRecord.email_normalized)).all() if e},
            "taken_domains": {e.partition("@")[2] for e in taken if e.partition("@")[2] not in GENERIC_DOMAINS},
            "found": Counter()}


def _domain_label(host) -> str:
    """'acme-steel' from 'shop.acme-steel.co.uk'."""
    labels = [x for x in (host or "").split(".") if x]
    if len(labels) < 2:
        return labels[0] if labels else ""
    if len(labels) >= 3 and labels[-2] in ES._SLD + ("or", "ne", "ltd", "plc", "in"):
        return labels[-3]
    return labels[-2]


def _name(s) -> str:
    return " ".join(t for t in normalize_name(s).split() if len(t) > 1 and t not in _NAME_STOP)


def name_score(company, host="", title="", site_name="") -> int:
    """0-100: how clearly the site is the buyer's own — the buyer's name (legal forms dropped) against the domain
    label and the site's <title> / og:site_name. Strict on purpose: a token-SET ratio scores 100 whenever one name's
    words are a subset of the other's, so a shared trade word ('steel') would pass for a different company."""
    from rapidfuzz import fuzz
    n = _name(company)
    compact = n.replace(" ", "")
    if not compact:
        return 0
    scores = []
    label = _domain_label(host)
    lab = re.sub(r"[^a-z0-9]", "", label)
    words = n.split()
    distinct = {w for w in words if len(w) >= 3 and w not in _TRADE_WORDS}
    if lab:
        scores.append(fuzz.ratio(compact, lab))                                       # 'acmesteel' ~ 'acme-steel'
        if len(lab) >= 4 and not {lab, compact} & _TRADE_WORDS \
                and (compact.startswith(lab) or lab.startswith(compact)):              # 'wurth' ~ 'Würth Polska'
            scores.append(100)
        if lab in distinct or any(p in distinct for p in label.split("-")):           # 'berner' ~ 'Albert Berner'
            scores.append(100)
        if len(words) >= 2 and len(lab) >= 2 and lab == "".join(w[0] for w in words):
            scores.append(100)                                                        # 'oo' ~ 'Otto Olsen'
    for t in (title, site_name):
        tn = _name(t)
        if tn:
            scores.append(fuzz.token_sort_ratio(n, tn))
            if len(compact) >= 5:                                                     # the name inside a long title
                scores.append(fuzz.partial_ratio(compact, tn.replace(" ", "")))
    return int(round(max(scores or [0])))


def _family(a, b) -> bool:
    return a == b or a.endswith("." + b) or b.endswith("." + a)


def _taken(em, ctx) -> str:
    if em in ctx["suppressed"]:
        return "on the do-not-contact list"
    if em in ctx["bounced"]:
        return "bounced before"
    if em in ctx["lead_emails"]:
        return "already on another buyer record"
    if em in ctx["recipient_emails"]:
        return "already a campaign recipient"
    if ctx["found"][em] > 1:
        return f"same address found for {ctx['found'][em]} buyers in this scan"
    return ""


def classify(cand, scraped, ctx) -> dict:
    """accept / review / reject for one scanned buyer (pure — `ctx` holds the preloaded sets).
    accept = a same-domain company inbox (sales/info/office…) on the buyer's own, name-matching site, used by nobody
    else; review = plausible, a person decides (personal / free-mail / support inbox, redirected or weakly matching
    site, domain already in a campaign, obfuscated address); reject = never use (off-domain, taken, suppressed,
    bounced, parked site, shared email, nothing found)."""
    site = scraped.get("site") or ""
    host = ES._bare_host(site) if site else ""
    final_host = ES._bare_host(scraped.get("final_url") or "") or host
    out = {"decision": "reject", "reasons": [], "email": "", "alt_emails": "", "found_on": "",
           "name_score": name_score(cand.get("company"), host, scraped.get("title", ""),
                                    scraped.get("site_name", ""))}
    why = out["reasons"]

    def done(decision=None, email="", others=()):
        if decision:
            out["decision"] = decision
        if email:
            out.update(email=email, found_on=(scraped.get("email_pages") or {}).get(email, ""),
                       alt_emails=" ".join(e for e in others if e != email)[:300])
        out["reasons"] = "; ".join(why)
        return out

    if not site:
        why.append("website unusable (social / directory / marketplace / invalid)")
        return done()
    emails = list(scraped.get("emails") or [])
    hidden = list(scraped.get("obfuscated") or [])
    if "same email as another buyer" in (cand.get("notes") or ""):
        why.append("the loader blanked an email this buyer shares with another")
    if scraped.get("parked"):
        why.append("parked / for-sale domain")
    if not emails:
        if hidden and not why:
            why.append("only an obfuscated ([at]/(dot)) address on the site")
            return done("review", hidden[0], hidden)
        why.append("no email found" + (f" ({scraped['blocked']})" if scraped.get("blocked") else ""))
        return done()
    if why:                                       # shared-email buyer / parked site: never use what was found
        return done("reject", emails[0], emails)
    chosen, problem = None, ""
    for em in emails:                             # the best address nobody else has and nothing blocks
        p = _taken(em, ctx)
        if not p:
            chosen = em
            break
        problem = problem or p
    if chosen is None:
        why.append(problem)
        return done("reject", emails[0], emails)
    local, _, dom = chosen.partition("@")
    decision = "accept"
    if dom in GENERIC_DOMAINS:
        decision = "review"
        why.append("free-mail address")
    elif not ES._same_domain(chosen, host):
        if final_host != host and ES._same_domain(chosen, final_host):
            decision = "review"
        else:
            why.append("address on another domain (agency / partner / platform?)")
            return done("reject", chosen, emails)
    if final_host and not _family(final_host, host):
        decision = "review"
        why.append(f"site redirects to another domain ({final_host})")
    if not _ROLE_OK_RE.match(local):
        decision = "review"
        why.append("support / marketing inbox" if _SUPPORT_RE.match(local) else "personal-looking address")
    if out["name_score"] < NAME_OK:
        decision = "review"
        why.append(f"site name does not clearly match the buyer (score {out['name_score']})")
    if any(_family(dom, d) for d in ctx["taken_domains"]):
        decision = "review"
        why.append("this domain already has an address in a campaign / on a buyer of this request")
    return done(decision, chosen, emails)


def scan(session_factory, request, expect=None, offset=0, limit=None, max_pages=5, pause=1.0, timeout=8,
         out=None) -> int:
    """Read the candidates (read-only), fetch their sites, write the review CSV to `out` (stdout)."""
    with session_factory() as s:
        sr = find_request(s, request)
        why = refuse_reason(s, sr)
        if why:
            _err(f"REFUSED: {why}")
            return 2
        cands = scan_candidates(s, sr)
        if expect is not None and len(cands) != expect:
            _err(f"REFUSED: {sr.tracking_code} has {len(cands)} candidate(s), not the expected {expect} — "
                 "nothing scanned")
            return 2
        ctx = load_context(s, sr)
        code = sr.tracking_code
        s.rollback()
    chunk = cands[offset:(offset + limit) if limit else None]
    _err(f"{code}: {len(cands)} email-less buyer(s) with a website; scanning {len(chunk)} from offset {offset}")
    results = []
    for i, c in enumerate(chunk, 1):
        try:
            r = ES.scrape_site(c["website"], max_pages=max_pages, pause=pause, timeout=timeout)
        except Exception as e:  # noqa: BLE001 — one broken site becomes a 'reject' row, never a lost chunk
            r = {"site": ES.clean_site(c["website"]), "final_url": "", "email": "", "emails": [], "email_pages": {},
                 "obfuscated": [], "phone": "", "pages": 0, "fetches": 0, "title": "", "site_name": "",
                 "parked": False, "blocked": f"scrape error ({type(e).__name__})"}
        results.append(r)
        _err(f"  [{i}/{len(chunk)}] lead {c['id']}: {len(r['emails'])} address(es) on {r['pages']} page(s)"
             + (f" — {r['blocked']}" if r["blocked"] else ""))
    for r in results:
        ctx["found"].update(set(r["emails"]))
    w = csv.DictWriter(out or sys.stdout, fieldnames=COLS, lineterminator="\n")
    w.writeheader()
    counts = Counter()
    for c, r in zip(chunk, results):
        k = classify(c, r, ctx)
        counts[k["decision"]] += 1
        w.writerow({"lead_id": c["id"], "external_id": c["external_id"], "anon_ref": c["anon_ref"],
                    "country": c["country"], "company": c["company"], "website": c["website"],
                    "final_url": r.get("final_url", ""), "decision": k["decision"], "reasons": k["reasons"],
                    "email": k["email"], "alt_emails": k["alt_emails"], "found_on": k["found_on"],
                    # 'tel:' keeps a spreadsheet from turning +48… into a number (apply strips it again)
                    "phone": ("tel:" + r["phone"]) if r.get("phone") else "", "name_score": k["name_score"],
                    "approve": ""})
    _err(f"done: accept {counts['accept']} · review {counts['review']} · reject {counts['reject']} — "
         "CSV on stdout only; nothing was written")
    return 0


# --------------------------------------------------------------------------------------------------- apply
class ApplyError(Exception):
    pass


def read_rows(text) -> list:
    """The review CSV back from the founder (a spreadsheet may add a BOM or save with ';'; several scan chunks may be
    concatenated, repeating the header line)."""
    text = (text or "").lstrip("\ufeff")
    first = text.splitlines()[0] if text.strip() else ""
    rd = csv.DictReader(io.StringIO(text), delimiter=";" if first.count(";") > first.count(",") else ",")
    rows = list(rd)
    need = {"lead_id", "external_id", "website", "decision", "email", "approve"}
    missing = need - {(h or "").strip() for h in (rd.fieldnames or [])}
    if missing:
        raise ApplyError(f"the CSV lacks column(s): {', '.join(sorted(missing))}")
    rows = [{(k or "").strip(): (v or "").strip() for k, v in row.items() if k} for row in rows]
    return [r for r in rows if r.get("lead_id", "").lstrip("\ufeff") != "lead_id"]


def _wanted(row, include_auto) -> str:
    ap = row.get("approve", "").lower()
    if ap in _YES:
        return "reviewed"
    if ap in _NO:
        return ""                                  # an explicit no beats --include-auto
    if include_auto and row.get("decision", "").lower() == "accept":
        return "auto-accept"
    return ""


def _clean_phone(raw) -> str:
    p = re.sub(r"[\s().-]", "", re.sub(r"^tel:", "", (raw or "").strip(), flags=re.I))
    return p if _PHONE_RE.match(p) else ""        # a spreadsheet-mangled number (lost '+', 4.8E+10) is dropped


def _apply_problem(s, sr, ld, em, dup) -> str:
    """Why this address must not be written onto this buyer now ('' = fine). Checked against the LIVE database."""
    if not em:
        return "no email in the row"
    if (ld.email or "").strip():
        return "buyer already has an email"
    if not ES.valid_email(em) or not CAMP._EMAIL_RE.match(em):
        return "invalid email"
    if ES.is_function_inbox(em.partition("@")[0]) or ES._is_placeholder(em):
        return "function inbox or placeholder (noreply / webmaster / name@ …)"
    if dup[em] > 1:
        return "same email chosen for several buyers"
    if SUP.is_suppressed(s, em, tenant_id=sr.owner_id):
        return "on the do-not-contact list"
    if s.exec(select(BounceRecord.id).where(BounceRecord.email_normalized == em)).first():
        return "bounced before"
    if s.exec(select(Lead.id).where(func.lower(func.trim(Lead.email)) == em, Lead.id != ld.id)).first():
        return "on another buyer record"
    if s.exec(select(CampaignRecipient.id).where(func.lower(func.trim(CampaignRecipient.to_email)) == em)).first():
        return "already a campaign recipient"
    if (s.exec(select(CampaignRecipient.id).where(CampaignRecipient.lead_id == ld.id)).first()
            or s.exec(select(Outreach.id).where(Outreach.lead_id == ld.id, Outreach.direction == "out")).first()
            or s.exec(select(BounceRecord.id).where(BounceRecord.lead_id == ld.id)).first()):
        return "buyer already contacted or enrolled"
    return ""


def apply_rows(s, sr, rows, include_auto=False, mark_misses=False, link=False) -> dict:
    """Write the approved addresses (the caller commits or rolls back — ONE transaction). Raises ApplyError, before
    anything is written, when any row no longer matches its buyer."""
    leads = {}
    for i, row in enumerate(rows, 2):             # 1. every row must still be the buyer the scan saw
        lid = row.get("lead_id", "")
        if not lid.isdigit():
            raise ApplyError(f"CSV line {i}: lead_id {lid!r} is not a number")
        ld = s.get(Lead, int(lid))
        if ld is None:
            raise ApplyError(f"CSV line {i}: lead {lid} not found")
        if ld.request_id != sr.id or not ld.managed or ld.owner_id is not None or ld.seller_id != sr.owner_id:
            raise ApplyError(f"CSV line {i}: lead {lid} is no longer a confidential buyer of {sr.tracking_code}")
        if (ld.external_id or "").strip() != row.get("external_id", "") \
                or (ld.website or "").strip() != row.get("website", ""):
            raise ApplyError(f"CSV line {i}: lead {lid} no longer matches the CSV (external_id / website)")
        if ld.id in leads:
            raise ApplyError(f"CSV line {i}: lead {lid} appears twice")
        leads[ld.id] = (ld, row)
    picked = {lid: (_wanted(row, include_auto), SUP.normalize_email(row.get("email")))
              for lid, (ld, row) in leads.items() if _wanted(row, include_auto)}
    dup = Counter(em for _, em in picked.values() if em)
    # one company = one address: the LIVE domain families already used by this seller's recipients / this request's
    # buyers, grown as this run applies — a second auto-accepted address at the same company needs approve=y
    taken = set(load_context(s, sr)["taken_domains"])
    res = {"applied": Counter(), "phones": 0, "skipped": Counter(), "misses": 0}
    for lid, (ld, row) in leads.items():          # 2. per row, against the live database
        problem = ""
        if lid in picked:
            how, em = picked[lid]
            problem = _apply_problem(s, sr, ld, em, dup)
            dom = em.partition("@")[2] if em else ""
            generic = dom in GENERIC_DOMAINS
            if not problem and how == "auto-accept" and not generic and any(_family(dom, d) for d in taken):
                problem = "this company's domain already has an address — set approve=y to use it anyway"
            if not problem:
                if dom and not generic:
                    taken.add(dom)
                ld.email = em
                added = f"email {em}"
                ph = _clean_phone(row.get("phone"))
                if ph and not (ld.phone or "").strip():
                    ld.phone = ph
                    added += f", phone {ph}"
                    res["phones"] += 1
                s.add(ld)
                tag = "Web-enrich (reviewed)" if how == "reviewed" else "Web-enrich (reviewed, auto-accept)"
                s.add(Activity(lead_id=ld.id, kind="enrichment",
                               body=f"{tag}: {added} from {(row.get('found_on') or ld.website or '')[:300]}"))
                if link:
                    from app.company_service import link_lead_company_safe
                    link_lead_company_safe(s, ld)          # savepoint; never breaks the run
                res["applied"][how] += 1
                continue
            res["skipped"][problem] += 1
        if mark_misses and not (ld.email or "").strip() and not s.exec(select(Activity.id).where(
                Activity.lead_id == ld.id, Activity.kind == "enrichment",
                Activity.body.startswith(MISS, autoescape=True))).first():
            note = problem or "; ".join(x for x in (row.get("decision", ""), row.get("reasons", "")) if x) \
                or "not approved"
            s.add(Activity(lead_id=ld.id, kind="enrichment", body=f"{MISS} ({note[:200]})"))
            res["misses"] += 1
    s.flush()
    return res


# --------------------------------------------------------------------------------------------------- enrol
def marked_leads(s, sr) -> list:
    """This request's confidential buyers whose address came through `apply` (the reviewed marker)."""
    return sorted(set(s.exec(select(Activity.lead_id).join(Lead, Lead.id == Activity.lead_id).where(
        Activity.kind == "enrichment", Activity.body.startswith(REVIEWED, autoescape=True),
        Lead.request_id == sr.id, Lead.managed == True, Lead.owner_id.is_(None),             # noqa: E712
        Lead.seller_id == sr.owner_id)).all()))


def enrol_refusal(c, sr) -> str:
    if c is None:
        return "campaign not found"
    if c.request_id != sr.id:
        return f"campaign #{c.id} is not the campaign of {sr.tracking_code}"
    if c.tenant_id != sr.owner_id:
        return f"campaign #{c.id} is for another seller"
    if c.status not in ("running", "paused"):
        return (f"campaign #{c.id} is {c.status} — only a running or paused campaign takes new buyers "
                "(set a completed one back to running first)")
    return ""


def enrol(s, sr, c, expected, dry_run=False, skip_ineligible=False) -> int:
    marked = marked_leads(s, sr)
    in_campaign = set(s.exec(select(CampaignRecipient.lead_id).where(
        CampaignRecipient.campaign_id == c.id, CampaignRecipient.lead_id.is_not(None))).all())
    want = [lid for lid in marked if lid not in in_campaign]
    f = {"request_id": sr.id, "lead_ids": want}
    prev = CAMP.audience_preview(s, c, f)
    eligible = prev["eligible_lead_ids"]
    print(f"campaign #{c.id} · {c.status} · sequence v{c.sequence_version} · {len(marked)} applied buyer(s), "
          f"{len(marked) - len(want)} already enrolled, {len(want)} to enrol → {len(eligible)} eligible "
          f"(missing email {prev['missing_email']}, invalid {prev['invalid_email']}, duplicate {prev['duplicates']}, "
          f"suppressed {prev['suppressed']})")
    if set(eligible) - set(want):                 # the lead_ids filter makes this impossible — never trust it blindly
        print("REFUSED: the preview contains buyers that were not applied by this tool")
        return 2
    if len(eligible) != len(want) and not skip_ineligible:
        print(f"REFUSED: {len(want) - len(eligible)} applied buyer(s) are not eligible any more — check them, or "
              "pass --skip-ineligible to enrol the rest")
        return 2
    if len(eligible) != expected:
        print(f"REFUSED: {len(eligible)} eligible, not the expected {expected} — nothing enrolled")
        return 2
    if not eligible:
        print("nothing to enrol")
        return 0
    queued = s.exec(select(func.count()).where(CampaignRecipient.campaign_id == c.id,
                                               CampaignRecipient.status.not_in(CAMP.TERMINAL_RECIPIENT))).one()
    if dry_run:
        print(f"[dry-run] would enrol {len(eligible)} buyer(s) after the {queued} already queued. Nothing written.")
        return 0
    top = s.exec(select(func.max(CampaignRecipient.id))).one() or 0
    res = CAMP.enroll(s, c, None, {"request_id": sr.id, "lead_ids": eligible}, expected=expected)
    if res.get("error"):
        s.rollback()
        print(f"REFUSED: {res['error']}")
        return 2
    s.commit()                                    # enroll() commits the recipients but leaves its audit row pending
    new = s.exec(select(CampaignRecipient.id).where(CampaignRecipient.campaign_id == c.id,
                                                    CampaignRecipient.id > top)).all()
    span = f"{min(new)}–{max(new)}" if new else "-"
    print(f"enrolled {res['created']} (skipped: suppressed {res['skipped_suppressed']}, already in "
          f"{res['skipped_existing']}); recipient ids {span}, all above the previous max {top} → sent after the "
          f"{queued} already queued")
    return 0


# --------------------------------------------------------------------------------------------------- cli
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("scan", help="read-only: fetch the sites, print the review CSV on stdout")
    p.add_argument("--request", required=True, help="SR-YYYYMM-NNNN tracking code or request id")
    p.add_argument("--expect-candidates", type=int, default=None, help="refuse unless this many candidates exist")
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--max-pages", type=int, default=5)
    p.add_argument("--pause", type=float, default=1.0, help="seconds between two requests to the same site")
    p.add_argument("--timeout", type=int, default=8)
    p = sub.add_parser("apply", help="write the approved addresses (one transaction, network locked)")
    p.add_argument("file", nargs="?", help="the reviewed CSV (or use --stdin)")
    p.add_argument("--request", required=True)
    p.add_argument("--stdin", action="store_true")
    p.add_argument("--include-auto", action="store_true", help="also apply every 'accept' row not marked approve=n")
    p.add_argument("--mark-misses", action="store_true", help="note every other row as tried (no auto-fill later)")
    p.add_argument("--link-trade-network", action="store_true", help="also add the address as a Trade Network contact")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("enrol", help="add exactly the applied buyers to the request's campaign")
    p.add_argument("--request", required=True)
    p.add_argument("--campaign", type=int, required=True)
    p.add_argument("--expected", type=int, required=True, help="the count you saw in --dry-run")
    p.add_argument("--skip-ineligible", action="store_true",
                   help="enrol the rest when some applied buyers became ineligible (suppressed, duplicate …)")
    p.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    if a.cmd == "scan":
        eng = readonly_engine()
        try:
            return scan(lambda: Session(eng, autoflush=False), a.request, a.expect_candidates, a.offset, a.limit,
                        a.max_pages, a.pause, a.timeout)
        finally:
            eng.dispose()

    lock_network()                                # apply / enrol never touch the network
    if a.cmd == "apply":
        if not a.stdin and not a.file:
            ap.error("give the reviewed CSV file or --stdin")
        try:
            if a.stdin:
                rows = read_rows(sys.stdin.read())
            else:
                with open(a.file, encoding="utf-8") as fh:
                    rows = read_rows(fh.read())
        except ApplyError as e:
            print(f"ERROR — nothing written: {e}")
            return 1
        with Session(engine) as s:
            sr = find_request(s, a.request)
            why = refuse_reason(s, sr)
            if why:
                print(f"REFUSED: {why}")
                return 2
            try:
                res = apply_rows(s, sr, rows, a.include_auto, a.mark_misses, a.link_trade_network)
            except ApplyError as e:
                s.rollback()
                print(f"ERROR — rolled back, nothing written: {e}")
                return 1
            applied = sum(res["applied"].values())
            msg = (f"{sr.tracking_code}: {len(rows)} row(s) · apply {applied} email(s) "
                   f"(reviewed {res['applied']['reviewed']}, auto-accept {res['applied']['auto-accept']}) "
                   f"+ {res['phones']} phone(s) · skipped {sum(res['skipped'].values())}"
                   + "".join(f" · {k}: {v}" for k, v in sorted(res["skipped"].items()))
                   + f" · misses marked {res['misses']}")
            if a.dry_run:
                s.rollback()
                print(f"[dry-run] {msg}. Nothing written.")
                return 0
            s.commit()
            print(msg)
            return 0

    with Session(engine) as s:                    # enrol
        sr = find_request(s, a.request)
        why = refuse_reason(s, sr) or enrol_refusal(s.get(Campaign, a.campaign), sr)
        if why:
            print(f"REFUSED: {why}")
            return 2
        return enrol(s, sr, s.get(Campaign, a.campaign), a.expected, a.dry_run, a.skip_ineligible)


if __name__ == "__main__":
    sys.exit(main())
