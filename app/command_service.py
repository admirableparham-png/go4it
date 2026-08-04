"""Command box — turn a founder's free-text prompt into a real harvest of buyer leads.

Keyless + deterministic (no LLM): a keyword router maps a prompt like "find hotel suppliers
in UAE" to (country, category-slug), then a generalized yellowpages-uae directory harvester
pulls real buyers with public phones and creates go4it Leads (source="command"). Runs as a
FastAPI BackgroundTask; a CommandJob row tracks status for the dashboard to poll.

Honest scope: we have a live buyer directory for the UAE (yellowpages-uae, any category slug).
For countries without a wired directory the job returns a clear note pointing at /research for
market stats rather than silently finding nothing.
"""
import json
import os
import re
import sys
from datetime import datetime

from sqlmodel import Session

from .config import COMMAND_ENRICH_BATCH
from .db import engine
from .enrich_service import run_web_enrichment
from .harvest_lib import (MAX_PAGES, OSM_CAP, harvest_osm_directory, harvest_uae_directory,
                          parse_uae_cards as _parse_cards)
from .lead_service import create_lead
from .models import CommandJob, Lead

# harvest_uae_directory / harvest_osm_directory / _parse_cards now live in app/harvest_lib (shared
# with the line pipeline); imported above so this module stays the command-box INTENT router.

# Reuse the Georgian->Latin transliterator so OSM names come out English (founder req).
_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)
try:
    from geo_en import translit_any  # noqa: E402  (any-script -> Latin: KA/AR/FA/Cyrillic)
except Exception:  # noqa: BLE001
    def translit_any(s):
        return (s or "").strip()

# country token -> (M49 code, ISO2, directory kind: "uae"=yellowpages, "osm"=OpenStreetMap).
# UAE keeps its rich phone directory; every other country is served globally via OSM Overpass.
COUNTRIES = {
    "uae": (784, "AE", "uae"), "u.a.e": (784, "AE", "uae"), "emirates": (784, "AE", "uae"),
    "dubai": (784, "AE", "uae"), "abu dhabi": (784, "AE", "uae"), "sharjah": (784, "AE", "uae"),
    "ajman": (784, "AE", "uae"),
    "georgia": (268, "GE", "osm"), "tbilisi": (268, "GE", "osm"),
    "qatar": (634, "QA", "osm"), "saudi": (682, "SA", "osm"), "saudi arabia": (682, "SA", "osm"),
    "turkey": (792, "TR", "osm"), "oman": (512, "OM", "osm"), "kuwait": (414, "KW", "osm"),
    "bahrain": (48, "BH", "osm"), "iraq": (368, "IQ", "osm"), "armenia": (51, "AM", "osm"),
    "azerbaijan": (31, "AZ", "osm"), "kazakhstan": (398, "KZ", "osm"), "iran": (364, "IR", "osm"),
    "jordan": (400, "JO", "osm"), "egypt": (818, "EG", "osm"), "uzbekistan": (860, "UZ", "osm"),
    "turkmenistan": (795, "TM", "osm"),
}

# category keyword -> OpenStreetMap selectors (buyers = shops/venues that stock our goods).
# An unknown keyword is tried as nwr["shop"="<keyword>"] (OSM shop values are single words).
OSM_TAGS = {
    "furniture": ['nwr["shop"="furniture"]'],
    "hardware": ['nwr["shop"="hardware"]', 'nwr["shop"="doityourself"]'],
    "building materials": ['nwr["shop"="building_materials"]', 'nwr["shop"="doityourself"]'],
    "construction": ['nwr["shop"="building_materials"]', 'nwr["shop"="trade"]'],
    "tiles": ['nwr["shop"="tiles"]', 'nwr["shop"="flooring"]'],
    "flooring": ['nwr["shop"="flooring"]', 'nwr["shop"="tiles"]'],
    "lighting": ['nwr["shop"="lighting"]'],
    "kitchen": ['nwr["shop"="kitchen"]'],
    "bathroom": ['nwr["shop"="bathroom_furnishing"]'],
    "carpet": ['nwr["shop"="carpet"]'],
    "interior": ['nwr["shop"="interior_decoration"]'],
    "decor": ['nwr["shop"="interior_decoration"]', 'nwr["shop"="houseware"]'],
    "home decor": ['nwr["shop"="interior_decoration"]', 'nwr["shop"="houseware"]'],
    "houseware": ['nwr["shop"="houseware"]'],
    "gift": ['nwr["shop"="gift"]'],
    "hotel": ['nwr["tourism"="hotel"]'],
    "restaurant": ['nwr["amenity"="restaurant"]'],
    "cafe": ['nwr["amenity"="cafe"]'],
    "supermarket": ['nwr["shop"="supermarket"]'],
    "grocery": ['nwr["shop"="supermarket"]', 'nwr["shop"="convenience"]'],
    "pharmacy": ['nwr["amenity"="pharmacy"]'],
    "electronics": ['nwr["shop"="electronics"]'],
    "car": ['nwr["shop"="car"]'], "auto parts": ['nwr["shop"="car_parts"]'],
    "clothes": ['nwr["shop"="clothes"]'], "jewelry": ['nwr["shop"="jewelry"]'],
    "perfume": ['nwr["shop"="perfumery"]'],
    "cosmetics": ['nwr["shop"="cosmetics"]', 'nwr["shop"="chemist"]'],
    "sports": ['nwr["shop"="sports"]', 'nwr["leisure"="fitness_centre"]', 'nwr["leisure"="sports_centre"]'],
    "gym": ['nwr["leisure"="fitness_centre"]', 'nwr["leisure"="sports_centre"]'],
    "fitness": ['nwr["leisure"="fitness_centre"]'],
    "stadium": ['nwr["leisure"="stadium"]', 'nwr["leisure"="pitch"]["sport"]'],
    "florist": ['nwr["shop"="florist"]'], "bakery": ['nwr["shop"="bakery"]'],
    "garden": ['nwr["shop"="garden_centre"]'], "paint": ['nwr["shop"="paint"]'],
    "toys": ['nwr["shop"="toys"]'], "stationery": ['nwr["shop"="stationery"]'],
    "books": ['nwr["shop"="books"]'], "shoes": ['nwr["shop"="shoes"]'],
    "textile": ['nwr["shop"="fabric"]', 'nwr["shop"="clothes"]'],
}

# Documented OpenStreetMap shop=* values (wiki "Map Features"). An unknown keyword is only turned
# into nwr["shop"="<word>"] when it's ACTUALLY a valid shop value — otherwise we'd fire a query that
# always returns nothing and looks like "no buyers there". Unmappable keywords get guidance instead.
VALID_OSM_SHOP = {
    "supermarket", "convenience", "grocery", "greengrocer", "butcher", "bakery", "deli",
    "confectionery", "seafood", "cheese", "dairy", "farm", "pasta", "beverages", "alcohol",
    "wine", "coffee", "tea", "spices", "health_food", "chemist", "cosmetics", "perfumery",
    "hairdresser", "beauty", "optician", "medical_supply", "nutrition_supplements",
    "department_store", "general", "kiosk", "mall", "wholesale", "trade", "variety_store",
    "clothes", "shoes", "boutique", "fabric", "fashion_accessories", "jewelry", "watches",
    "bag", "leather", "tailor", "sewing", "furniture", "kitchen", "bathroom_furnishing",
    "bed", "interior_decoration", "curtain", "carpet", "flooring", "tiles", "houseware",
    "household_linen", "lighting", "doityourself", "hardware", "paint", "trade", "glaziery",
    "electrical", "energy", "fireplace", "garden_centre", "florist", "doors", "windows",
    "building_materials", "electronics", "hifi", "computer", "mobile_phone", "telecommunication",
    "camera", "appliance", "vacuum_cleaner", "car", "car_parts", "car_repair", "motorcycle",
    "tyres", "bicycle", "toys", "sports", "outdoor", "fishing", "hunting", "musical_instrument",
    "art", "craft", "frame", "collector", "games", "model", "books", "stationery", "newsagent",
    "gift", "ticket", "lottery", "travel_agency", "copyshop", "photo", "pet", "pet_grooming",
    "florist", "garden_furniture", "agrarian", "trade", "hardware", "tobacco", "e-cigarette",
    "furniture", "antiques", "candles", "party", "fireworks", "toys",
}

# category keyword -> a known-good yellowpages-uae slug (any unknown keyword is slugified + tried)
SLUG_SYNONYMS = {
    "home decor": "home-decor", "decor": "home-decor", "home-decor": "home-decor",
    "homeware": "home-decor", "decoration": "home-decor",
    "tableware": "tableware", "serveware": "tableware", "dinnerware": "tableware",
    "crockery": "crockery", "kitchenware": "kitchenware", "kitchen": "kitchenware",
    "houseware": "housewares", "housewares": "housewares",
    "handicraft": "handicrafts", "handicrafts": "handicrafts",
    "lighting": "lightings", "lightings": "lightings", "lamp": "lightings",
    "lamps": "lightings", "chandelier": "lightings",
    "gift": "gift-items", "gifts": "gift-items", "gift item": "gift-items",
    "gift items": "gift-items", "corporate gift": "gift-items",
    "gift shop": "gift-shops", "gift shops": "gift-shops",
    "hotel": "hotel-supplies", "hotel supply": "hotel-supplies", "hotel supplies": "hotel-supplies",
    "hospitality": "hotel-supplies", "furniture": "furniture", "carpet": "carpets",
    "carpets": "carpets", "curtain": "curtains", "curtains": "curtains",
    "ceramic": "ceramics", "ceramics": "ceramics", "glassware": "glassware",
    "porcelain": "porcelain", "textile": "textiles", "textiles": "textiles",
    "perfume": "perfumes", "perfumes": "perfumes", "cosmetic": "cosmetics",
    "cosmetics": "cosmetics", "foodstuff": "foodstuff", "food": "foodstuff",
    "building material": "building-materials", "building materials": "building-materials",
    "hardware": "hardware", "stationery": "stationery", "toys": "toys", "toy": "toys",
}

# words to strip when isolating the category keyword from the prompt
_STOP = {"find", "get", "search", "me", "some", "a", "an", "the", "all", "list", "of", "for",
         "in", "to", "please", "go", "and", "with", "buyers", "buyer", "suppliers", "supplier",
         "importers", "importer", "dealers", "dealer", "distributors", "distributor",
         "wholesalers", "wholesaler", "companies", "company", "shops", "stores", "market",
         "who", "buy", "sell", "need", "want", "more"}


_CNAME = {"AE": "UAE", "GE": "Georgia", "QA": "Qatar", "SA": "Saudi Arabia", "TR": "Turkey",
          "OM": "Oman", "KW": "Kuwait", "BH": "Bahrain", "IQ": "Iraq", "AM": "Armenia",
          "AZ": "Azerbaijan", "KZ": "Kazakhstan", "IR": "Iran", "JO": "Jordan", "EG": "Egypt",
          "UZ": "Uzbekistan", "TM": "Turkmenistan"}


def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


def _osm_selectors(keyword):
    """Map a category keyword to OSM selectors. Tries the curated OSM_TAGS map (phrase, then each
    word, each in given/singular/plural form); falls back to nwr["shop"="<word>"] ONLY when <word>
    is a documented OSM shop value. Returns [] for an unmappable keyword (caller gives guidance)."""
    for cand in (keyword, keyword.rstrip("s"), keyword + "s"):
        if cand in OSM_TAGS:
            return OSM_TAGS[cand]
    for w in keyword.split():
        for cand in (w, w.rstrip("s"), w + "s"):
            if cand in OSM_TAGS:
                return OSM_TAGS[cand]
    # validated guess: only a REAL shop=* value, never a made-up one
    for cand in (keyword.replace(" ", "_"), keyword.rstrip("s").replace(" ", "_"),
                 (keyword + "s").replace(" ", "_")):
        if cand in VALID_OSM_SHOP:
            return ['nwr["shop"="%s"]' % cand]
    for w in keyword.split():                       # a single valid word inside a phrase
        for cand in (w, w.rstrip("s"), w + "s"):
            if cand in VALID_OSM_SHOP:
                return ['nwr["shop"="%s"]' % cand]
    return []


def parse_command(prompt):
    """Free-text prompt -> {action, country_code, iso, country_name, keyword, slug, selectors, note}.
    UAE routes to the yellowpages directory; any other known country routes to OpenStreetMap."""
    # Accept non-Latin input (Persian/Arabic/Cyrillic): transliterate to Latin first, and match the
    # country in BOTH the raw and transliterated text so Finglish and native script both work.
    raw_lo = " " + (prompt or "").lower().strip() + " "
    tl = translit_any(prompt or "")
    tl_lo = " " + tl.lower().strip() + " "
    country = None
    for token in sorted(COUNTRIES, key=len, reverse=True):   # longest match first
        if f" {token} " in raw_lo or f" {token} " in tl_lo:
            country = token
            break
    words = re.findall(r"[a-z0-9]+", tl.lower()) or re.findall(r"[a-z0-9]+", (prompt or "").lower())
    if country:
        ctoks = set(country.split())
        words = [w for w in words if w not in ctoks]
    keyword = " ".join(w for w in words if w not in _STOP).strip()

    base = {"country_code": None, "iso": "", "country_name": "", "keyword": keyword,
            "slug": "", "selectors": []}
    if not country:
        base["action"] = "unknown"
        base["note"] = ("Couldn't spot a country. Try e.g. \"furniture shops in Georgia\", "
                        "\"hotel suppliers in UAE\", or \"tile shops in Qatar\".")
        return base
    code, iso, kind = COUNTRIES[country]
    cname = _CNAME.get(iso, country.title())
    base.update({"country_code": code, "iso": iso, "country_name": cname})
    if not keyword:
        base["action"] = "unknown"
        base["note"] = f"Add a category too, e.g. \"tableware buyers in {cname}\"."
        return base
    if kind == "uae":
        slug = SLUG_SYNONYMS.get(keyword) or SLUG_SYNONYMS.get(keyword.rstrip("s")) or slugify(keyword)
        base.update({"action": "harvest_uae", "slug": slug,
                     "note": f"Harvesting UAE '{slug}' buyers from the public directory..."})
        return base
    selectors = _osm_selectors(keyword)
    if not selectors:
        base["action"] = "unknown"
        base["note"] = (f"I don't have an OpenStreetMap category for '{keyword}'. Try one like "
                        "furniture, hardware, building materials, tiles, lighting, hotel, restaurant, "
                        "supermarket, pharmacy, sports, perfume, jewelry, or electronics.")
        return base
    base.update({"action": "harvest_osm", "slug": slugify(keyword), "selectors": selectors,
                 "note": f"Harvesting {cname} '{keyword}' businesses from OpenStreetMap..."})
    return base


# --------------------------------------------------------------------------- background job

def run_command_job(job_id):
    """Background task: execute a parsed command and record results on the CommandJob row."""
    with Session(engine) as session:
        job = session.get(CommandJob, job_id)
        if not job:
            return
        job.status = "running"
        job.started_at = datetime.utcnow()
        session.add(job)
        session.commit()

        try:
            p = json.loads(job.params or "{}")
            if job.action == "harvest_uae":
                slug, iso, kw = p["slug"], p["iso"], p.get("keyword") or p["slug"]
                buyers, pages = harvest_uae_directory(slug)
                new = dup = 0
                for b in buyers:
                    lead = Lead(
                        source="command",
                        external_id=f"cmd:{iso}:{slug}:{b['cid']}",
                        product=f"{kw.title()} (UAE buyer)", category=slug,
                        dest_country=iso, dest_city=b.get("city") or "",
                        buyer_company=b.get("company") or "",
                        phone=(b["phones"][0] if b.get("phones") else ""),
                        website=b.get("website") or "",
                        notes=f"via command: {job.prompt}",
                    )
                    if create_lead(session, lead, run=False) is not None:
                        new += 1
                    else:
                        dup += 1
                job.leads_seen = len(buyers)
                job.leads_new = new
                job.leads_duplicate = dup
                job.status = "ok"
                cap = " (capped)" if pages >= MAX_PAGES else ""
                if buyers:
                    job.note = (f"Found {len(buyers)} UAE '{slug}' buyers across {pages} page(s){cap} "
                                f"-> {new} new leads, {dup} already had.")
                else:
                    job.note = (f"No listings for '{slug}' on the UAE directory. Try a different "
                                f"category word (e.g. tableware, hotel, perfume, furniture).")
            elif job.action == "harvest_osm":
                iso, kw, sel = p["iso"], p.get("keyword") or p.get("slug"), p.get("selectors") or []
                cname = p.get("country_name") or iso
                found, capped = harvest_osm_directory(iso, sel)
                new = dup = 0
                for b in found:
                    lead = Lead(
                        source="command",
                        external_id=f"cmd:{iso}:osm:{b['osmid']}",
                        product=f"{kw.title()} ({cname} business)", category=p.get("slug") or kw,
                        dest_country=iso, dest_city=b.get("city") or "",
                        buyer_company=b.get("company") or "",
                        phone=b.get("phone") or "", website=b.get("website") or "",
                        notes=f"via command: {job.prompt}",
                    )
                    if create_lead(session, lead, run=False) is not None:
                        new += 1
                    else:
                        dup += 1
                with_c = sum(1 for b in found if b.get("phone") or b.get("website"))
                job.leads_seen = len(found)
                job.leads_new = new
                job.leads_duplicate = dup
                job.status = "ok"
                if found:
                    capnote = (f" (hit the {OSM_CAP} cap - narrow the category or add a city for more)"
                               if capped else "")
                    job.note = (f"Found {len(found)} {cname} '{kw}' businesses on OpenStreetMap "
                                f"({with_c} with phone/website) -> {new} new leads, {dup} already had"
                                f"{capnote}.")
                else:
                    job.note = (f"No '{kw}' businesses mapped in {cname}. Try a broader category "
                                f"(furniture, hardware, hotel, restaurant, supermarket, pharmacy).")
            else:
                job.status = "ok"          # unknown: note already explains how to phrase it

            # Post-harvest: fill blank contacts on the just-harvested command leads (generic +
            # SSRF-safe web enrich) so a command-box find reaches the same "has email" quality as the
            # curated product lines. Bounded + polite (COMMAND_ENRICH_BATCH, 0 = off).
            if COMMAND_ENRICH_BATCH > 0 and job.status == "ok" and (job.leads_new or 0) > 0:
                try:
                    summ = run_web_enrichment(session, source="command",
                                              limit=COMMAND_ENRICH_BATCH, skip_attempted=True,
                                              log=lambda *a: None)
                    if summ.get("enriched"):
                        job.note = (job.note or "") + \
                            f" +{summ['enriched']} enriched with email/phone from their site."
                except Exception:  # noqa: BLE001
                    pass
        except Exception as exc:           # noqa: BLE001
            job.status = "error"
            job.error = str(exc)[:400]
            job.note = "Harvest failed - see error."

        job.finished_at = datetime.utcnow()
        session.add(job)
        session.commit()
