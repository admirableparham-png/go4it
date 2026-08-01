"""Website -> contact enrichment for leads that have a site but no email.

Many harvested buyers (OpenStreetMap shops, UAE directory listings, research finds) arrive with a
company name + website but no reachable contact. Their OWN site almost always exposes a role
mailbox (info@ / sales@ / export@) on the homepage or a /contact page. This module fetches that
site, extracts the best email (and any tel: phone), and writes it back onto the Lead so the
outreach flow built in app/outreach.py has someone to reach — with an Activity note for provenance
and an IngestionRun row for observability. No third-party credits: it reads each buyer's own site.

Used by scripts/enrich_leads.py (CLI/cron) and can be wired to a lead-detail button.
"""
import ipaddress
import re
import socket
import time
import urllib.parse
import urllib.request
from typing import Optional

from sqlmodel import Session, or_, select

from .models import Activity, IngestionRun, Lead

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) go4it-enrich"
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

# Contact pages worth trying, in priority order, after the homepage. Georgian sites often use
# /kontakti; Gulf/EN sites use /contact(-us). We stop as soon as we find an email.
CONTACT_PATHS = ("", "/contact", "/contact-us", "/contacts", "/kontakti",
                 "/en/contact", "/about", "/about-us", "/company")

# Hosts that are never the buyer's own site (socials, marketplaces, asset/CDN hosts). A website
# field pointing at one of these can't be scraped for a company mailbox.
_JUNK_HOST = (
    "facebook.", "instagram.", "twitter.", "x.com", "t.me", "telegram.", "youtube.", "youtu.be",
    "linkedin.", "tiktok.", "pinterest.", "wa.me", "whatsapp.", "maps.google", "google.com/maps",
    "goo.gl", "yellowpages", "tradeindia", "go4worldbusiness", "alibaba.", "made-in-china",
    "gstatic", "cloudflare", "w3.org", "schema.org", "gmpg.org",
)
# Mailboxes that are noise, not a real inbox to pitch.
_JUNK_EMAIL = ("sentry", "wixpress", "example.", "@sentry", "godaddy", "@2x", "domain.com",
               "email.com", "yourdomain", "@sentry.io", ".png", ".jpg", ".jpeg", ".gif",
               ".webp", ".svg", ".css", ".js")
# Role mailboxes we prefer, best first — a generic company inbox beats a random personal one.
_ROLE_RANK = ("sales", "export", "info", "contact", "office", "commercial", "trade",
              "hello", "order", "orders", "purchase", "procurement", "admin")


# --- SSRF guard -------------------------------------------------------------------------------
# Lead `website` values come from world-editable sources (OpenStreetMap tags, directories), so a
# malicious entry could point the scraper at an internal address (169.254.169.254 metadata,
# 127.0.0.1, 10.x ...). We block non-public IPs. clean_site does the cheap offline IP-LITERAL
# check (keeps it pure/test-offline); the DNS-resolving check + redirect validation run at fetch.

def _ip_blocked(addr: ipaddress._BaseAddress) -> bool:
    return (addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved
            or addr.is_multicast or addr.is_unspecified)


def _host_blocked(hostname: str, resolve: bool = False) -> bool:
    """True if the host is a non-public IP. resolve=False only checks IP LITERALS (no DNS, offline);
    resolve=True also resolves hostnames (used at fetch time, where network is already in play)."""
    hostname = (hostname or "").strip().strip("[]").rstrip(".")
    if not hostname:
        return True
    try:
        return _ip_blocked(ipaddress.ip_address(hostname))    # literal IP
    except ValueError:
        pass
    if not resolve:
        return False
    try:
        for info in socket.getaddrinfo(hostname, None):
            if _ip_blocked(ipaddress.ip_address(info[4][0].split("%")[0])):
                return True
    except (OSError, ValueError):
        return False        # can't resolve -> the fetch will just fail; not an SSRF target
    return False


class _NoSSRFRedirect(urllib.request.HTTPRedirectHandler):
    """Re-validate every redirect hop so a benign domain can't 302 us onto an internal address."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            parts = urllib.parse.urlsplit(newurl)
        except ValueError:
            return None
        if parts.scheme not in ("http", "https") or _host_blocked(parts.hostname, resolve=True):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_NoSSRFRedirect)


def clean_site(website: str) -> str:
    """Normalise a messy `website` field to a fetchable base URL, or '' if unusable.

    Handles 'peaniltd.com (shop.peaniltd.com)', 'llcprogress.ge', 'http://x/', bare hosts; rejects
    social/marketplace/asset hosts, non-http(s) schemes, and internal/loopback IP literals (SSRF).
    """
    raw = (website or "").strip().strip('"\'')
    if not raw:
        return ""
    # take the first token (drop trailing '(mirror)' notes and stray whitespace)
    raw = re.split(r"[\s(]", raw, 1)[0].strip().strip("/")
    if not raw:
        return ""
    if "://" not in raw:
        raw = "http://" + raw
    try:
        parts = urllib.parse.urlsplit(raw)
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https"):      # no file://, gopher://, etc.
        return ""
    host = (parts.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if "." not in host or " " in host or any(j in host for j in _JUNK_HOST):
        return ""
    if _host_blocked(parts.hostname):              # offline: blocks internal IP LITERALS
        return ""
    return f"{parts.scheme}://{parts.netloc}"


def _fetch(url: str, timeout: int = 8) -> str:
    # SSRF guard: resolve the host and refuse internal/loopback targets before connecting.
    if _host_blocked(urllib.parse.urlsplit(url).hostname, resolve=True):
        return ""
    # Slow/dead buyer sites aren't worth waiting on across hundreds of leads: short timeout, and
    # only retry the homepage-style first hit (attempt 0) once. _OPENER re-checks every redirect.
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with _OPENER.open(req, timeout=timeout) as r:
                if r.status and r.status >= 400:
                    return ""
                return r.read(1_500_000).decode("utf-8", "ignore")
        except Exception:  # noqa: BLE001 - any network/decoding failure => treat as empty
            if attempt == 0:
                time.sleep(0.5)
    return ""


def valid_email(addr: str) -> bool:
    """Reject the version-string / asset-param false positives EMAIL_RE happily matches
    ('magnific-popup@1.1.0', 'wght@100..900', 'rspack@1.6.8', 'dx@h.e'). A real address has an
    alphabetic TLD (>=2 chars) and no purely-numeric domain labels."""
    a = (addr or "").strip().lower()
    if a.count("@") != 1 or ".." in a:
        return False
    local, dom = a.split("@")
    labels = dom.split(".")
    if not local or len(labels) < 2:
        return False
    if not re.fullmatch(r"[a-z]{2,24}", labels[-1]):        # TLD must be alphabetic
        return False
    return all(re.fullmatch(r"[a-z0-9-]+", lb) and not lb.isdigit() for lb in labels)


def _same_domain(addr: str, host: str) -> bool:
    """True if the email's domain is the site host or a sub/parent of it — on a DOT boundary, so
    'sales@notacme.ge' is NOT same-domain as host 'acme.ge' (the old endswith check mis-matched)."""
    dom = addr.partition("@")[2]
    return bool(host) and (dom == host or dom.endswith("." + host) or host.endswith("." + dom))


def _emails_from(text: str, host: str) -> list:
    """Extract usable mailboxes from page text, ranked: same-domain first, then role mailboxes."""
    out, seen = [], set()
    for e in EMAIL_RE.findall(text or ""):
        e = e.strip(".").lower()
        if e in seen or any(j in e for j in _JUNK_EMAIL) or not valid_email(e):
            continue
        seen.add(e)
        out.append(e)

    def rank(addr: str):
        local, _, dom = addr.partition("@")
        same_domain = 0 if _same_domain(addr, host) else 1
        role = next((i for i, r in enumerate(_ROLE_RANK) if local.startswith(r)), len(_ROLE_RANK))
        noreply = 1 if ("noreply" in local or "no-reply" in local or "donotreply" in local) else 0
        return (noreply, same_domain, role, len(addr))

    return sorted(out, key=rank)


def _phones_from(text: str) -> list:
    """High-precision phones only: from tel: hrefs (loose text regex has too many false hits)."""
    out, seen = [], set()
    for p in re.findall(r"tel:\s*([+0-9][\d\-\s()]{6,}\d)", text or ""):
        norm = re.sub(r"[^\d+]", "", p)
        if 7 <= len(norm.lstrip("+")) <= 15 and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


def scrape_site(website: str, max_pages: int = 3, pause: float = 0.3) -> dict:
    """Fetch a buyer's own site (homepage + a couple of contact pages) and pull contacts.

    Returns {site, email, emails, phone, pages}. `email`/`phone` are the best single picks (or '').
    """
    base = clean_site(website)
    result = {"site": base, "email": "", "emails": [], "phone": "", "pages": 0}
    if not base:
        return result
    host = urllib.parse.urlsplit(base).netloc.lower().replace("www.", "")
    emails: list = []
    phones: list = []
    for path in CONTACT_PATHS:
        if result["pages"] >= max_pages:
            break
        html = _fetch(base + path)
        if not html:
            continue
        result["pages"] += 1
        for e in _emails_from(html, host):
            if e not in emails:
                emails.append(e)
        for p in _phones_from(html):
            if p not in phones:
                phones.append(p)
        # got a same-domain mailbox already? no need to keep crawling this site.
        if emails and _same_domain(emails[0], host):
            break
        if path != CONTACT_PATHS[-1]:
            time.sleep(pause)
    result["emails"] = emails
    result["email"] = emails[0] if emails else ""
    result["phone"] = phones[0] if phones else ""
    return result


def enrich_lead(session: Session, lead: Lead, apply: bool = True, pause: float = 0.3) -> dict:
    """Scrape one lead's website and (optionally) write back email/phone it was missing.

    Only fills BLANK fields — never overwrites a human-entered contact. Records an Activity note
    for provenance. Returns {lead_id, status, email, phone, site}.
      status: nosite | nohit | enriched | skipped(existing email)
    """
    res = {"lead_id": lead.id, "status": "nosite", "email": "", "phone": "", "site": ""}
    if (lead.email or "").strip():
        res["status"] = "skipped"
        return res
    if not clean_site(lead.website):
        return res
    found = scrape_site(lead.website, pause=pause)
    res["site"] = found["site"]
    if not found["email"] and not found["phone"]:
        res["status"] = "nohit"
        if apply:            # record the attempt so the scheduler won't re-scrape this dead site
            session.add(Activity(lead_id=lead.id, kind="enrichment",
                                 body=f"Web-enrich: no contact found on {found['site']}"))
        return res

    added = []
    if found["email"] and not (lead.email or "").strip():
        res["email"] = found["email"]
        added.append(f"email {found['email']}")
        if apply:
            lead.email = found["email"]
    if found["phone"] and not (lead.phone or "").strip():
        res["phone"] = found["phone"]
        added.append(f"phone {found['phone']}")
        if apply:
            lead.phone = found["phone"]

    if not added:
        res["status"] = "nohit"
        if apply:
            session.add(Activity(lead_id=lead.id, kind="enrichment",
                                 body=f"Web-enrich: no new contact found on {found['site']}"))
        return res
    res["status"] = "enriched"
    if apply:
        session.add(lead)
        session.add(Activity(
            lead_id=lead.id, kind="enrichment",
            body=f"Web-enrich: found {', '.join(added)} from {found['site']}",
        ))
    return res


def candidate_leads(session: Session, source: Optional[str] = None, limit: Optional[int] = None,
                    skip_attempted: bool = False):
    """Leads worth scraping: have a website, missing an email. Newest-command finds first.

    skip_attempted=True excludes leads that already have an 'enrichment' Activity — so the scheduled
    worker attempts each lead once (hit or miss) instead of re-hammering permanent no-hits forever.
    The manual CLI leaves it False so a human can force a re-scan.
    """
    stmt = select(Lead).where(
        Lead.website != "",
        Lead.website.is_not(None),
        or_(Lead.email == "", Lead.email.is_(None)),
    )
    if source:
        stmt = stmt.where(Lead.source == source)
    if skip_attempted:
        stmt = stmt.where(Lead.id.not_in(
            select(Activity.lead_id).where(Activity.kind == "enrichment")))
    stmt = stmt.order_by(Lead.id.desc())
    # Filter unscrapeable (social/marketplace/junk) websites BEFORE applying the limit, so the batch
    # window counts only real candidates — otherwise a run of junk-website leads at the top would
    # fill the window every pass and starve the enrichable leads below them (they never get scraped
    # and, having no website we can use, never get an 'enrichment' Activity to be skipped).
    scrapeable = (l for l in session.exec(stmt).all() if clean_site(l.website))
    if limit:
        out = []
        for lead in scrapeable:
            out.append(lead)
            if len(out) >= limit:
                break
        return out
    return list(scrapeable)


def run_web_enrichment(session: Session, *, source: Optional[str] = None,
                       limit: Optional[int] = None, apply: bool = True,
                       pause: float = 0.3, skip_attempted: bool = False, log=print) -> dict:
    """Enrich a batch of website-bearing, email-less leads. Logs an IngestionRun for observability.

    Returns a summary dict. Commits per-lead when apply=True so a mid-run interrupt keeps progress.
    """
    leads = candidate_leads(session, source=source, limit=limit, skip_attempted=skip_attempted)
    run = IngestionRun(source="enrich-web", leads_seen=len(leads), status="running")
    if apply:
        session.add(run)
        session.commit()
        session.refresh(run)
    log(f"web-enrich: {len(leads)} candidate lead(s)"
        + (f" (source={source})" if source else "") + (" [DRY-RUN]" if not apply else ""))

    enriched = nohit = 0
    for i, lead in enumerate(leads, 1):
        r = enrich_lead(session, lead, apply=apply, pause=pause)
        if r["status"] == "enriched":
            enriched += 1
            log(f"  [{i}/{len(leads)}] +{r['email'] or r['phone']}  "
                f"<- {(lead.buyer_company or '?')[:38]}")
            if apply:
                session.commit()
        else:
            nohit += 1
        if i % 25 == 0:
            log(f"  ...{i}/{len(leads)}  (+{enriched} enriched)")

    if apply:
        run.leads_new = enriched
        run.finished_at = _utcnow()
        run.status = "ok"
        session.add(run)
        session.commit()
    summary = {"candidates": len(leads), "enriched": enriched, "nohit": nohit}
    log(f"web-enrich done: +{enriched} enriched / {nohit} no-hit / {len(leads)} tried")
    return summary


def _utcnow():
    from datetime import datetime
    return datetime.utcnow()
