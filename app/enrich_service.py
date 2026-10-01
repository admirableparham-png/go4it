"""Website -> contact enrichment for leads that have a site but no email.

Many harvested buyers (OpenStreetMap shops, UAE directory listings, research finds) arrive with a
company name + website but no reachable contact. Their OWN site almost always exposes a role
mailbox (info@ / sales@ / export@) on the homepage or a /contact page. This module fetches that
site, extracts the best email (and any tel: phone), and writes it back onto the Lead so the
outreach flow built in app/outreach.py has someone to reach — with an Activity note for provenance
and an IngestionRun row for observability. No third-party credits: it reads each buyer's own site.

Polite by construction (Phase 12): robots.txt is honoured, the User-Agent is neutral (it lands in BUYERS' web-server
logs, so it never names the platform), an HTTP error is never asked twice and one site gets at most MAX_FETCHES page
requests. Confidential managed buyers are never auto-filled from here — their addresses go through the reviewed flow
in scripts/enrich_managed_buyers.py.

Used by scripts/enrich_leads.py (CLI/cron), the worker, the bounce path and a lead-detail button.
"""
import html as htmllib
import ipaddress
import re
import socket
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from html.parser import HTMLParser
from typing import Optional

from sqlmodel import Session, or_, select

from .models import Activity, IngestionRun, Lead

# Neutral, honest crawler identity — it shows up in buyers' server logs, so it must never say g4it/go4it.
UA = "Mozilla/5.0 (compatible; contact-check/1.0)"
ROBOTS_AGENT = "contact-check"            # the product token robots.txt groups are matched against
_HEADERS = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5"}
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")       # pages are scanned with _email_hits (same hits, linear)

# Contact pages worth trying, in priority order, after the homepage and the contact/impressum/about links the
# homepage itself names. Georgian sites often use /kontakti; Gulf/EN sites /contact(-us); DE/AT/CH /kontakt and
# /impressum; IT /contatti; ES/PT /contacto. We stop as soon as a same-domain role mailbox turns up.
CONTACT_PATHS = ("", "/contact", "/contact-us", "/contacts", "/kontakt", "/kontakti", "/contatti", "/contacto",
                 "/impressum", "/contact.html", "/pages/contact", "/en/contact", "/about", "/about-us", "/company")
MAX_FETCHES = 8                     # page requests per site however many 404 (robots.txt not counted)
MAX_CRAWL_DELAY = 30                # a robots.txt Crawl-delay above this (seconds): leave the site alone

# Hosts that are never the buyer's own site (socials, marketplaces, B2B directories, asset/CDN hosts). A website field
# pointing at one of these can't be scraped for a company mailbox — and a directory PROFILE URL would shrink to the
# directory's homepage, whose own mailbox would then look same-domain. Matched on a DOT boundary: 'x.com' and
# 'sub.x.com' are junk, 'fedex.com' / 'intex.com.au' / 'kraft.metals.ca' are not.
_JUNK_HOST = ("x.com", "t.me", "wa.me", "youtu.be", "goo.gl", "g.page", "bit.ly", "linktr.ee", "w3.org",
              "schema.org", "gmpg.org", "dnb.com")
# brands that are junk on ANY TLD or subdomain ('facebook.com', 'm.facebook.de', 'acme.en.ec21.com', maps.google.*)
_JUNK_BRAND_RE = re.compile(r"(?:^|\.)(?:facebook|instagram|twitter|telegram|youtube|linkedin|tiktok|pinterest|"
                            r"whatsapp|google|alibaba|kompass|ec21|tradekey|amazon|ebay|gstatic|cloudflare)\.")
# directory names distinctive enough to match anywhere in the host ('yellowpages-uae.com', 'dir.indiamart.com')
_JUNK_WORD = ("yellowpages", "tradeindia", "go4worldbusiness", "made-in-china", "europages", "exporthub",
              "tradewheel", "eworldtrade", "indiamart", "zoominfo", "opencorporates")
# Mailboxes that are noise, not a real inbox to pitch.
_JUNK_EMAIL = ("sentry", "wixpress", "example.", "@sentry", "godaddy", "@2x", "domain.com",
               "email.com", "yourdomain", "@sentry.io", ".png", ".jpg", ".jpeg", ".gif",
               ".webp", ".svg", ".css", ".js")
# Function inboxes nobody buys from — DROPPED, not down-ranked (a page whose only address is webmaster@ or privacy@
# has no usable contact). The whole local part or its prefix before a separator/digit: 'hr@', 'hr.uk@' go, 'hrvoje@'
# stays.
_FUNCTION_RE = re.compile(
    r"^(?:webmaster|postmaster|hostmaster|abuse|privacy|gdpr|dpo|rodo|iod|datenschutz|dataprotection|legal|jobs?|"
    r"careers?|kariera|praca|rekrutacja|recruitment|recruiting|hr|bewerbung|press|media|newsletter|unsubscribe|"
    r"invoices?|faktury|faktura|billing|accounts|accounting)(?:$|[._+\-0-9])")
_NOREPLY_RE = re.compile(r"no[-_.]?reply|do[-_.]?not[-_.]?reply|mailer-daemon|^bounces?(?:$|[._+\-])")
# Template placeholders ('info@yourcompany.com', 'john@doe.com', 'name@') — never a real inbox.
_PLACEHOLDER_LOCAL = {"name", "user", "your", "you", "yourname", "your.name", "your-name", "username", "firstname",
                      "lastname", "first.last", "firstname.lastname", "name.surname", "john.doe", "jane.doe",
                      "johndoe", "someone", "somebody", "test", "example"}
_PLACEHOLDER_DOMAIN = {"company.com", "domain.com", "email.com", "website.com", "site.com", "mysite.com",
                       "mycompany.com", "doe.com", "sample.com", "test.com"}
_PLACEHOLDER_LABEL = {"example", "test", "sample", "yourcompany", "yourdomain", "yoursite", "your-company",
                      "your-domain", "companyname", "company-name", "domainname"}
# Role mailboxes we prefer, best first — a generic company inbox beats a random personal one.
_ROLE_RANK = ("sales", "export", "info", "contact", "office", "commercial", "trade",
              "hello", "order", "orders", "purchase", "procurement")
_ROLE_STOP_RE = re.compile(r"^(?:%s)(?:$|[._\-0-9])" % "|".join(_ROLE_RANK))


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


def _bare_host(url: str) -> str:
    """'https://www.Acme.ge:443/x' -> 'acme.ge' (lowercase, no port, no leading www.)."""
    try:
        h = (urllib.parse.urlsplit(url if "://" in (url or "") else "http://" + (url or "")).hostname or "")
    except ValueError:
        return ""
    h = h.lower().rstrip(".")
    return h[4:] if h.startswith("www.") else h


def _origin(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc.lower()}"


def _is_junk_host(host: str) -> bool:
    host = (host or "").lower().strip(".")
    if host.startswith("www."):
        host = host[4:]
    return (any(host == h or host.endswith("." + h) for h in _JUNK_HOST) or bool(_JUNK_BRAND_RE.search(host))
            or any(w in host for w in _JUNK_WORD))


def clean_site(website: str) -> str:
    """Normalise a messy `website` field to a fetchable base URL, or '' if unusable.

    Handles 'peaniltd.com (shop.peaniltd.com)', 'llcprogress.ge', 'http://x/', bare hosts; rejects
    social/marketplace/directory/asset hosts (on a dot boundary), non-http(s) schemes, and internal/loopback IP
    literals (SSRF).
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
    if "." not in host or " " in host or _is_junk_host(parts.hostname or host):
        return ""
    if _host_blocked(parts.hostname):              # offline: blocks internal IP LITERALS
        return ""
    return f"{parts.scheme}://{parts.netloc}"


_CHARSET_RE = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?([\w-]+)""", re.I)


def _decode(body: bytes, ctype: str = "") -> str:
    """Bytes -> text in the page's declared charset (Content-Type, else <meta charset>), utf-8 otherwise — so a
    windows-1250 title still reads 'Śruba', not 'ruba', for the buyer-name check."""
    m = re.search(r"charset=[\"']?([\w-]+)", ctype or "") or _CHARSET_RE.search(body[:4096])
    cs = m.group(1) if m else "utf-8"
    try:
        return body.decode(cs.decode() if isinstance(cs, bytes) else cs, "ignore")
    except LookupError:
        return body.decode("utf-8", "ignore")


def _fetch_page(url: str, timeout: int = 8) -> tuple:
    """GET one page -> (html, final_url, error). error is '' on success; 'http-<code>' when the server answered
    with an error — never asked again, a 404/403 won't change on a second request; 'network' after ONE retry
    (DNS/connect/timeout/TLS); 'blocked' for an internal address; 'not-html' for a file/image body."""
    # SSRF guard: resolve the host and refuse internal/loopback targets before connecting.
    if _host_blocked(urllib.parse.urlsplit(url).hostname, resolve=True):
        return "", "", "blocked"
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers=_HEADERS)
            with _OPENER.open(req, timeout=timeout) as r:      # _OPENER re-checks every redirect hop
                ctype = (r.headers.get("Content-Type") or "").lower()
                if ctype and "html" not in ctype and "text" not in ctype:
                    return "", r.geturl() or url, "not-html"
                return _decode(r.read(1_500_000), ctype), r.geturl() or url, ""
        except urllib.error.HTTPError as e:
            return "", "", f"http-{e.code}"
        except Exception:  # noqa: BLE001 - any network/decoding failure: one more try, then give up
            if attempt == 0:
                time.sleep(0.5)
    return "", "", "network"


def _fetch(url: str, timeout: int = 8) -> str:
    """The page body, or '' on any failure (kept for callers that only need the text)."""
    return _fetch_page(url, timeout)[0]


def _robots(origin: str, timeout: int = 8) -> tuple:
    """(rules, error) for a site's robots.txt. A missing file (404/410…) allows everything and 401/403 allows
    nothing (the stdlib convention); a 5xx or a network failure gives rules=None — stay away, as crawlers should."""
    rp = urllib.robotparser.RobotFileParser()
    url = origin.rstrip("/") + "/robots.txt"
    if _host_blocked(urllib.parse.urlsplit(url).hostname, resolve=True):
        return None, "blocked"
    try:
        with _OPENER.open(urllib.request.Request(url, headers=_HEADERS), timeout=timeout) as r:
            rp.parse(r.read(500_000).decode("utf-8", "ignore").splitlines())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            rp.disallow_all = True
        elif 400 <= e.code < 500:
            rp.allow_all = True
        else:
            return None, f"http-{e.code}"
    except Exception:  # noqa: BLE001
        return None, "network"
    return rp, ""


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


def _is_placeholder(addr: str) -> bool:
    local, _, dom = addr.partition("@")
    return (local in _PLACEHOLDER_LOCAL or dom in _PLACEHOLDER_DOMAIN
            or any(lb in _PLACEHOLDER_LABEL for lb in dom.split(".")))


def is_function_inbox(local: str) -> bool:
    """noreply / webmaster / privacy / jobs / invoices … — an inbox no buyer conversation belongs in."""
    local = (local or "").lower()
    return bool(_FUNCTION_RE.match(local) or _NOREPLY_RE.search(local))


_UESC_RE = re.compile(r"\\u([0-9a-fA-F]{4})")
_MAILTO_RE = re.compile(r"mailto:([^\"'<>\s]+)", re.I)
_CF_RE = re.compile(r"(?:data-cfemail=[\"']|/cdn-cgi/l/email-protection#)([0-9a-fA-F]{6,})")
_OBF_MARK = re.compile(r"[\[({]\s{0,3}(?:at|@)\s{0,3}[\])}]", re.I)          # '[at]' '(at)' '{at}' '[@]' '(@)'
_OBF_DOT = r"\s{0,3}(?:\[\s{0,3}dot\s{0,3}\]|\(\s{0,3}dot\s{0,3}\)|\{\s{0,3}dot\s{0,3}\}|\.)\s{0,3}"
_OBF_DOMAIN = re.compile(r"\s{0,3}([\w-]+(?:" + _OBF_DOT + r"[\w-]+)+)", re.I)
_LOCAL_TAIL = re.compile(r"[\w.+-]{1,64}\Z")           # \Z, not $: '$' also matches before a final newline
_DOMAIN_HEAD = re.compile(r"[\w-]+\.[\w.-]+")
_SLD = ("co", "com", "net", "org", "ac", "gov", "edu")


def _plain_text(text: str) -> str:
    """Undo the encodings that hide or garble addresses in raw HTML before the regex runs: entities ('&#64;'),
    JSON escapes ('\\u003e' would glue on as 'u003einfo@') and URL-encoded mailto targets ('%20info@' -> '20info@')."""
    t = htmllib.unescape(text or "")
    t = _UESC_RE.sub(lambda m: chr(int(m.group(1), 16)), t)
    t = t.replace("%20", " ").replace("%40", "@")
    return _MAILTO_RE.sub(lambda m: "mailto: " + urllib.parse.unquote(m.group(1)) + " ", t)


def _cf_decode(hexstr: str) -> str:
    """Cloudflare 'email protection' (data-cfemail / #hex): first byte is the XOR key for the rest."""
    try:
        key = int(hexstr[:2], 16)
        return "".join(chr(int(hexstr[i:i + 2], 16) ^ key) for i in range(2, len(hexstr) - 1, 2))
    except ValueError:
        return ""


def _clean_addr(raw: str, hosts=()) -> str:
    """One regex hit -> a usable lowercase inbox, or ''. A TLD glued to the next word on the page
    ('sales@acme.co.uk.We are…') is cut back to the site's own host."""
    local, _, dom = raw.strip(".-").lower().partition("@")
    local = re.sub(r"^u00[0-9a-f]{2}(?=.)", "", local)          # a JSON escape whose backslash was lost
    for h in hosts:
        extra = dom[len(h) + 1:] if h and dom.startswith(h + ".") else ""
        # 'acme.co.uk' + '.we' is glue; 'acme.com' + '.au' is a real sibling domain, so leave that one alone
        if extra and "." not in extra and not (len(extra) == 2 and h.rsplit(".", 1)[-1] in _SLD):
            dom = h
            break
    addr = f"{local}@{dom}"
    if not valid_email(addr) or any(j in addr for j in _JUNK_EMAIL) or _is_placeholder(addr) \
            or is_function_inbox(local):
        return ""
    return addr


def _email_hits(t: str) -> list:
    """What EMAIL_RE.findall(t) finds, anchored on each '@' with bounded windows (a local part is <= 64 chars, a
    domain <= 255) — linear on any page. The plain regex is quadratic on a long unbroken run (a minified bundle or an
    inline blob: 40k chars took seconds), and this also runs inside the worker's bounce handling."""
    out, last = [], 0
    i = t.find("@")
    while i != -1:
        local = _LOCAL_TAIL.search(t, max(last, i - 64), i)
        dom = _DOMAIN_HEAD.match(t, i + 1, i + 256)
        if local and dom:
            out.append(local.group(0) + "@" + dom.group(0))
            last = dom.end()                  # like findall: no overlapping hits
        i = t.find("@", i + 1)
    return out


def _obfuscated_hits(t: str) -> list:
    """'info [at] acme [dot] com' / 'sales(at)acme.com' forms, anchored on the [at] marker (linear, as above)."""
    out = []
    for m in _OBF_MARK.finditer(t):
        j = m.start()
        while j > 0 and m.start() - j < 3 and t[j - 1].isspace():
            j -= 1
        local = _LOCAL_TAIL.search(t, max(0, j - 64), j)
        dom = _OBF_DOMAIN.match(t, m.end(), m.end() + 256)
        if local and dom:
            out.append(local.group(0) + "@" + re.sub(_OBF_DOT, ".", dom.group(1), flags=re.I))
    return out


def _page_emails(text: str, hosts=()) -> tuple:
    """(plain, obfuscated): the usable addresses on one page, in page order. `obfuscated` holds '[at]'/'(dot)'
    forms — deliberately hidden addresses a person should look at, never used automatically."""
    raw = text or ""
    t = _plain_text(raw)
    plain, seen = [], set()
    for hit in _email_hits(t) + [_cf_decode(h) for h in _CF_RE.findall(raw)]:
        e = _clean_addr(hit, hosts) if "@" in hit else ""
        if e and e not in seen:
            seen.add(e)
            plain.append(e)
    hidden = []
    for hit in _obfuscated_hits(t):
        e = _clean_addr(hit, hosts)
        if e and e not in seen:
            seen.add(e)
            hidden.append(e)
    return plain, hidden


def _rank(addr: str, host: str, alt_hosts=()) -> tuple:
    """Sort key over ALL of a site's candidates: the site's own domain, then the domain it redirects to, then
    off-domain; within each, role mailboxes in _ROLE_RANK order; shorter first; then alphabetical (stable)."""
    local = addr.partition("@")[0]
    if _same_domain(addr, host):
        dom = 0
    elif any(_same_domain(addr, h) for h in alt_hosts):
        dom = 1
    else:
        dom = 2
    role = next((i for i, r in enumerate(_ROLE_RANK) if local.startswith(r)), len(_ROLE_RANK))
    return (dom, role, len(addr), addr)


def _emails_from(text: str, host: str) -> list:
    """Extract usable mailboxes from page text, ranked: same-domain first, then role mailboxes. Noise, function
    inboxes (noreply/webmaster/privacy/jobs…) and template placeholders are dropped."""
    return sorted(_page_emails(text, (host,))[0], key=lambda e: _rank(e, host))


def _phones_from(text: str) -> list:
    """High-precision phones only: from tel: hrefs (loose text regex has too many false hits)."""
    out, seen = [], set()
    for p in re.findall(r"tel:\s*([+0-9][\d\-\s()]{6,}\d)", text or ""):
        norm = re.sub(r"[^\d+]", "", p)
        if 7 <= len(norm.lstrip("+")) <= 15 and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


# --- page structure: titles, parked domains, contact links ----------------------------------
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_OG_SITE_RE = re.compile(r"<meta\b[^>]*\bproperty\s*=\s*[\"']og:site_name[\"'][^>]*>", re.I)
_CONTENT_RE = re.compile(r"\bcontent\s*=\s*[\"']([^\"']*)[\"']", re.I)
_PARKED_RE = re.compile(r"domain (?:name )?(?:is|may be) for sale|buy this domain|this domain (?:is|has been) "
                        r"(?:parked|registered)|parked (?:free|domain)|domain parking|hugedomains|sedoparking|"
                        r"parkingcrew|afternic|\bdan\.com\b", re.I)
_PARKING_HOSTS = ("sedoparking.com", "parkingcrew.net", "bodis.com", "hugedomains.com", "afternic.com", "dan.com",
                  "sedo.com", "domainmarket.com", "undeveloped.com", "parklogic.com")
# contact / impressum / about links, any language, best first (matched on the link path and its text)
_LINK_PRIORITY = (
    re.compile(r"contact|kontakt|contat|yhteys|kapcsolat|elerhetoseg|צור\s*קשר|יצירת\s*קשר|اتصل|تواصل|επικοινων",
               re.I),
    re.compile(r"impressum|imprint|legal-notice|mentions-legales", re.I),
    re.compile(r"about|o-nas|o-firmie|ueber-uns|uber-uns|chi-siamo|quienes-somos|om-oss|om-os|meista|rolunk", re.I),
)
_FILE_RE = re.compile(r"\.(?:pdf|jpe?g|png|gif|webp|svg|zip|rar|docx?|xlsx?|pptx?|mp4)$", re.I)


def _fold(s: str) -> str:
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()


def _page_names(page: str) -> tuple:
    """(<title>, og:site_name) of a page — what the site calls itself, for the buyer-name check."""
    m = _TITLE_RE.search(page or "")
    title = " ".join(htmllib.unescape(m.group(1)).split())[:200] if m else ""
    og = _OG_SITE_RE.search(page or "")
    c = _CONTENT_RE.search(og.group(0)) if og else None
    return title, (" ".join(htmllib.unescape(c.group(1)).split())[:200] if c else "")


def _is_parking_host(host: str) -> bool:
    return any(host == h or host.endswith("." + h) for h in _PARKING_HOSTS)


class _LinkParser(HTMLParser):
    """Collects (href, visible text) for every <a> on a page."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links, self._href, self._text = [], None, []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href, self._text = dict(attrs).get("href") or "", []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join("".join(self._text).split())[:80]))
            self._href = None


def _contact_links(page: str, page_url: str, hosts) -> list:
    """Same-site links the page itself names as contact / impressum / about (any language), best first."""
    p = _LinkParser()
    try:
        p.feed(page or "")
    except Exception:  # noqa: BLE001 - malformed markup: what was parsed so far is still usable
        pass
    best = {}
    for href, text in p.links:
        href = (href or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        url = urllib.parse.urljoin(page_url, href).split("#")[0]
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https") or _bare_host(url) not in hosts or _FILE_RE.search(parts.path):
            continue
        hay = urllib.parse.unquote(parts.path) + " " + text
        pri = next((i for i, rx in enumerate(_LINK_PRIORITY) if rx.search(hay) or rx.search(_fold(hay))), None)
        if pri is not None and pri < best.get(url, 99):
            best[url] = pri
    return [u for u, _ in sorted(best.items(), key=lambda kv: kv[1])]


def _explicit_scheme(website: str) -> bool:
    raw = re.split(r"[\s(]", (website or "").strip().strip('"\''), 1)[0]
    return "://" in raw


def scrape_site(website: str, max_pages: int = 3, pause: float = 0.3, *, timeout: int = 8,
                robots: bool = True) -> dict:
    """Fetch a buyer's own site and pull contacts: the homepage, then the contact/impressum/about links the homepage
    names (same site only), then the guessed CONTACT_PATHS — at most `max_pages` loaded pages and MAX_FETCHES page
    requests. robots.txt is honoured (its Crawl-delay too); `pause` seconds between page requests.

    Returns {site, final_url, email, emails, email_pages, obfuscated, phone, pages, fetches, title, site_name,
    parked, blocked}. `emails` is ranked across EVERY page fetched (same-domain first, then role mailbox), so an
    agency address on the homepage never beats info@own-domain on /contact; `email`/`phone` are the best single
    picks (or ''); `email_pages` maps each address to the first page it was seen on; `final_url` is where the
    homepage really lives after redirects; `blocked` says why nothing could be read (robots.txt, homepage error).
    """
    base = clean_site(website)
    result = {"site": base, "final_url": "", "email": "", "emails": [], "email_pages": {}, "obfuscated": [],
              "phone": "", "pages": 0, "fetches": 0, "title": "", "site_name": "", "parked": False, "blocked": ""}
    if not base:
        return result
    host = _bare_host(base)
    hosts = [host]
    rules = {}
    attempts = 0

    def robots_for(url):                 # (rules, error) per scheme://host, read once
        key = _origin(url)
        if key not in rules:
            result["fetches"] += 1
            rules[key] = _robots(key, timeout)
        return rules[key]

    def get(url):
        nonlocal attempts
        attempts += 1
        result["fetches"] += 1
        return _fetch_page(url, timeout)

    # the homepage first (a bare host is tried on https, then http): it says where the site really lives and which
    # pages it calls 'contact'. Nothing else is fetched if robots.txt can't be read or the homepage fails.
    origins = [base] if _explicit_scheme(website) else ["https://" + base.split("://", 1)[1], base]
    home = page = final = err = ""
    for origin in origins:
        home = origin + "/"
        if robots:
            rp, rerr = robots_for(home)
            if rerr == "network" and origin != origins[-1]:
                continue                                    # nothing answers on this scheme: try the next one
            if rp is None:
                result["blocked"] = "site unreachable" if rerr == "network" else f"robots.txt unreadable ({rerr})"
                return result
            if not rp.can_fetch(ROBOTS_AGENT, home):
                result["blocked"] = "robots.txt"
                return result
            if float(rp.crawl_delay(ROBOTS_AGENT) or 0) > MAX_CRAWL_DELAY:
                result["blocked"] = "robots.txt crawl-delay"
                return result
        page, final, err = get(home)
        if page or err != "network":
            break
    if not page:
        result["blocked"] = f"homepage {err or 'empty'}"
        return result
    rp = robots_for(home)[0] if robots else None
    delay = max(pause, float((rp.crawl_delay(ROBOTS_AGENT) if rp is not None else 0) or 0))

    result["final_url"] = final or home
    fh = _bare_host(result["final_url"])
    if fh and fh not in hosts:
        hosts.append(fh)
    result["title"], result["site_name"] = _page_names(page)
    result["parked"] = bool(_PARKED_RE.search(page)) or _is_parking_host(fh)
    root = _origin(result["final_url"])
    links = _contact_links(page, result["final_url"], hosts)
    queue = links + [root + p for p in CONTACT_PATHS[1:]]
    found, hidden, phones = {}, [], []

    def take(url, html):
        result["pages"] += 1
        plain, obf = _page_emails(html, hosts)
        for e in plain:
            found.setdefault(e, url)
        hidden.extend(e for e in obf if e not in hidden)
        phones.extend(p for p in _phones_from(_plain_text(html)) if p not in phones)

    def done():       # a same-domain role mailbox is all we came for
        return any(_ROLE_STOP_RE.match(e.partition("@")[0]) and any(_same_domain(e, h) for h in hosts)
                   for e in found)

    take(result["final_url"], page)
    tried = {home.rstrip("/"), result["final_url"].rstrip("/")}
    while queue and not done() and result["pages"] < max_pages and attempts < MAX_FETCHES:
        url = queue.pop(0)
        if url.rstrip("/") in tried:
            continue
        tried.add(url.rstrip("/"))
        if robots:
            rp = robots_for(url)[0]
            if rp is None or not rp.can_fetch(ROBOTS_AGENT, url):
                continue
        time.sleep(delay)
        html, got, _err = get(url)
        if html:
            take(got or url, html)
    ranked = sorted(found, key=lambda e: _rank(e, host, hosts[1:]))
    result.update(emails=ranked, email=ranked[0] if ranked else "", email_pages={e: found[e] for e in ranked},
                  obfuscated=[e for e in hidden if e not in found], phone=phones[0] if phones else "")
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
                    skip_attempted: bool = False, include_managed: bool = False):
    """Leads worth scraping: have a website, missing an email. Newest-command finds first.

    skip_attempted=True excludes leads that already have an 'enrichment' Activity — so the scheduled
    worker attempts each lead once (hit or miss) instead of re-hammering permanent no-hits forever.
    The manual CLI leaves it False so a human can force a re-scan. Confidential managed buyers are left out
    unless include_managed: an address written onto one is enrolment-eligible at once, so theirs are reviewed
    first (scripts/enrich_managed_buyers.py).
    """
    stmt = select(Lead).where(
        Lead.website != "",
        Lead.website.is_not(None),
        or_(Lead.email == "", Lead.email.is_(None)),
    )
    if not include_managed:
        stmt = stmt.where(or_(Lead.managed == False, Lead.managed.is_(None)))   # noqa: E712
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
                       pause: float = 0.3, skip_attempted: bool = False, include_managed: bool = False,
                       log=print) -> dict:
    """Enrich a batch of website-bearing, email-less leads. Logs an IngestionRun for observability.

    Returns a summary dict. With apply=True it commits after EVERY lead (hit or miss): a mid-run interrupt keeps
    progress, and no write is ever left open across the next site's network I/O — an open SQLite write lock would
    stall the worker's campaign sends and the IMAP poll ('database is locked').
    """
    leads = candidate_leads(session, source=source, limit=limit, skip_attempted=skip_attempted,
                            include_managed=include_managed)
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
        if apply:
            session.commit()                 # per lead, hit or miss — before the next site is scraped
        if r["status"] == "enriched":
            enriched += 1
            log(f"  [{i}/{len(leads)}] +{r['email'] or r['phone']}  "
                f"<- {(lead.buyer_company or '?')[:38]}")
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
