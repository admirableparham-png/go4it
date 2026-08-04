"""Generic buyer-harvest library — the single home for the directory scrapers that used to be
copy-pasted across scripts/harvest_uae_*.py and app/command_service.py.

Two keyless backends, both returning plain dicts the line pipeline (scripts/harvest_line.py) and the
command box (app/command_service.py) share:

  * yellowpages-uae directory  -> harvest_uae_directory(slug) / harvest_uae_lines(slugs_cfg)
  * OpenStreetMap Overpass     -> harvest_osm_directory(iso, selectors)  (any country, global)

Plus detail_contacts() — the yellowpages detail-page email/website/phone scrape reused by enrichment.
No LLM, no keys. Names come out Latin (OSM path transliterates via geo_en.translit_any).
"""
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

# Reuse the any-script -> Latin transliterator (KA/AR/FA/Cyrillic) for OSM names (founder req).
_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)
try:
    from geo_en import translit_any  # noqa: E402
except Exception:  # noqa: BLE001
    def translit_any(s):
        return (s or "").strip()

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) go4it-intel"
MAX_PAGES = 10           # yellowpages page cap (bound each UAE harvest)
OSM_CAP = 2000           # OSM element cap per country query (reported, not silent)
# Public Overpass instances get overloaded (504s) - try mirrors in order until one answers.
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
EMIRATES = ["Dubai", "Sharjah", "Abu Dhabi", "Ajman", "Ras Al Khaimah",
            "Umm Al Quwain", "Fujairah", "Al Ain"]


def http_get(url, tries=3, timeout=30):
    """GET a URL as text with a few retries. Returns "" on failure (never raises)."""
    for i in range(tries):
        try:
            return urllib.request.urlopen(
                urllib.request.Request(url, headers={"User-Agent": UA}), timeout=timeout
            ).read().decode("utf-8", "ignore")
        except Exception:  # noqa: BLE001
            if i == tries - 1:
                return ""
            time.sleep(1.5)
    return ""


def city_from(text):
    for e in EMIRATES:
        if e.lower() in (text or "").lower():
            return e
    return ""


# ------------------------------------------------------------------- yellowpages-uae directory

def parse_uae_cards(h):
    """Yield card dicts {cid, company, location, city, phones, established, website, profile}
    from a yellowpages-uae list page. Superset of every previous copy of this parser."""
    anchors = list(re.finditer(r'title="([^"]+)"[^>]*href="(/[a-z0-9-]+-(\d{4,7}))\?p=', h))
    out = []
    for idx, m in enumerate(anchors):
        seg = h[m.end(): anchors[idx + 1].start() if idx + 1 < len(anchors) else m.end() + 1600]
        loc = re.search(r'Location\s*:\s*</span><span[^>]*>([^<]+)', seg)
        loc = html.unescape(loc.group(1)).strip() if loc else ""
        phones = []
        for p in re.findall(r'tel:([+\d]{7,})', seg):
            if p not in phones:
                phones.append(p)
        est = re.search(r'Established:</span>\s*(\d{4})', seg)
        web = re.search(r'href="(https?://(?!www\.yellowpages-uae)[^"]+)"[^>]*>\s*(?:website|visit)', seg, re.I)
        out.append({
            "cid": m.group(3), "company": html.unescape(m.group(1)).strip(),
            "location": loc, "city": city_from(loc), "phones": phones[:3],
            "established": est.group(1) if est else "",
            "website": web.group(1) if web else "", "profile": m.group(2),
        })
    return out


def harvest_uae_directory(slug, max_pages=MAX_PAGES):
    """Pull unique UAE buyer cards for one category slug (bounded). Returns (cards, pages_fetched)."""
    seen, buyers, pages = set(), [], 0
    for page in range(1, max_pages + 1):
        url = f"https://www.yellowpages-uae.com/uae/{slug}" + (f"?page={page}" if page > 1 else "")
        cards = parse_uae_cards(http_get(url))
        pages = page
        if not cards:
            break
        page_new = 0
        for c in cards:
            if c["cid"] in seen:
                continue
            seen.add(c["cid"])
            page_new += 1
            buyers.append(c)
        time.sleep(0.4)
        if page_new == 0:          # site repeats page 1 past the last real page -> stop
            break
    return buyers, pages


def harvest_uae_lines(slugs_cfg, log=None):
    """Harvest a whole product line across many category slugs, deduped by company id, collecting
    each buyer's categories + which families fit. `slugs_cfg` = {slug: {label, fit, max_pages}}.
    Returns the buyers-file dict {source, dest, total, with_phone, with_website, by_category, buyers}.
    This replaces the per-product scripts/harvest_uae_*.py run() bodies."""
    buyers = {}   # cid -> record
    for slug, cfg in slugs_cfg.items():
        label = cfg.get("label") or slug
        fit = cfg.get("fit") or ""
        maxp = int(cfg.get("max_pages") or MAX_PAGES)
        got, slug_seen = 0, set()
        for page in range(1, maxp + 1):
            url = f"https://www.yellowpages-uae.com/uae/{slug}" + (f"?page={page}" if page > 1 else "")
            cards = parse_uae_cards(http_get(url))
            if not cards:
                break
            page_new = 0
            for c in cards:
                if c["cid"] in slug_seen:
                    continue
                slug_seen.add(c["cid"])
                page_new += 1
                got += 1
                rec = buyers.get(c["cid"])
                if rec is None:
                    rec = dict(c)
                    rec["categories"] = []
                    rec["fits"] = set()
                    rec["needs_enrichment"] = not c["website"]
                    buyers[c["cid"]] = rec
                else:
                    if not rec["website"] and c["website"]:
                        rec["website"] = c["website"]
                        rec["needs_enrichment"] = False
                    for p in c["phones"]:
                        if p not in rec["phones"]:
                            rec["phones"].append(p)
                if label not in rec["categories"]:
                    rec["categories"].append(label)
                rec["fits"].add(fit)
            time.sleep(0.4)
            if page_new == 0:
                break
        if log:
            log(f"   {slug:24} -> {got} listings")

    recs = list(buyers.values())
    for r in recs:
        r["fits"] = sorted(r.pop("fits"))
        r.pop("cid", None)
    labels = [cfg.get("label") or s for s, cfg in slugs_cfg.items()]
    return {
        "source": "yellowpages-uae.com", "dest": "UAE",
        "total": len(recs),
        "with_phone": sum(1 for r in recs if r["phones"]),
        "with_website": sum(1 for r in recs if r["website"]),
        "by_category": {label: sum(1 for r in recs if label in r["categories"])
                        for label in dict.fromkeys(labels)},
        "buyers": recs,
    }


# ----------------------------------------------------------------- OpenStreetMap (any country)

def harvest_osm_directory(iso, selectors, cap=OSM_CAP):
    """Pull named businesses of the given category in a country from OpenStreetMap Overpass
    (free, global, no key). Returns (rows, capped) where each row is
    {osmid, company, city, phone, website} and `capped` is True if the query hit `cap` (more exist).
    Names come out Latin (name:en / int_name, else transliteration); a non-Latinizable name is
    skipped rather than leaking non-Latin into the CRM."""
    parts = "".join(f"{s}(area.a);" for s in selectors)
    ql = ('[out:json][timeout:60];area["ISO3166-1"="%s"][admin_level=2]->.a;(%s);out center tags %d;'
          % (iso, parts, cap))
    data = urllib.parse.urlencode({"data": ql}).encode()
    els = []
    for endpoint in OVERPASS_ENDPOINTS:      # fall through mirrors on 504/timeout
        try:
            raw = urllib.request.urlopen(
                urllib.request.Request(endpoint, data=data, headers={"User-Agent": UA}),
                timeout=90).read()
            els = json.loads(raw).get("elements", []) or []
            if els:
                break
        except Exception:  # noqa: BLE001
            time.sleep(1.5)
            continue
    capped = len(els) >= cap
    out, seen = [], set()
    for e in els:
        t = e.get("tags", {})
        name = translit_any(t.get("name:en") or t.get("int_name") or t.get("name:latin")
                            or t.get("name") or "").strip()
        key = name.lower()
        if not name or key in seen:
            continue
        seen.add(key)
        phone = t.get("phone") or t.get("contact:phone") or t.get("contact:mobile") or ""
        web = t.get("website") or t.get("contact:website") or t.get("url") or ""
        city = t.get("addr:city") or t.get("addr:place") or ""
        out.append({"osmid": f"{(e.get('type') or 'x')[:1]}{e.get('id')}", "company": name,
                    "city": translit_any(city).title() if city else "",
                    "phone": phone, "website": web})
    return out, capped


# ------------------------------------------------------------------- detail-page enrichment

# third-party / asset hosts that are never the buyer's own site
JUNK = ["yellowpages-uae", "google", "facebook", "instagram", "twitter", "x.com", "threads",
        "youtube", "whatsapp", "pinterest", "w3.org", "gstatic", "cloudflare", "cdnjs",
        "jquery", "schema.org", "dmca", "undefined", "linkedin", "tiktok", "clarity.ms", "clarity",
        "bing.com", "microsoft", "gtag", "googletag", "hotjar", "sentry", "fontawesome",
        "bootstrapcdn", "unpkg", "jsdelivr", "gravatar", "gmpg.org", "doubleclick", "amazonaws",
        "cloudfront", "apple.com", "play.google", "itunes", "wp.com", "cdn.", "static.", "assets.", "ajax."]
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def real_site(u):
    """A URL is a usable company website only if it's not a third-party/asset URL."""
    u = (u or "").strip().strip('\\"\'')
    if not u or "\\" in u or " " in u:
        return ""
    lo = u.lower()
    if any(j in lo for j in JUNK):
        return ""
    if any(x in lo for x in ("/tag/", "/badge", "/pixel", ".js", ".css", ".min.", "/ajax/", "/embed")):
        return ""
    return u


def detail_contacts(profile):
    """Scrape a yellowpages-uae detail page for (email, website, [phones]). "" / [] when not found."""
    h = http_get(f"https://www.yellowpages-uae.com{profile}", tries=2, timeout=25)
    if not h:
        return "", "", []
    emails = [e for e in EMAIL_RE.findall(h)
              if not e.lower().endswith((".png", ".jpg", ".webp", ".css", ".js"))
              and not any(j in e.lower() for j in ("yellowpages", "sentry", "example", "wixpress"))]
    email = emails[0] if emails else ""
    website = ""
    for u in re.findall(r'https?://[^"\s<>\\]+', h):
        cleaned = real_site(u)
        if cleaned:
            website = cleaned
            break
    phones = []
    for p in re.findall(r"tel:([+\d]{7,})", h):
        if p not in phones:
            phones.append(p)
    return email, website, phones
