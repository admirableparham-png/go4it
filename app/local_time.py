"""Phase 13 — each buyer gets the email in THEIR business hours, not the server's.

A campaign with `local_hours` (e.g. "09:00-11:00,14:00-16:00") sends a buyer only inside those hours of the buyer's own
clock — country (and, for multi-zone countries, city/state) → IANA time zone — on the buyer's working days (Mon–Fri;
Sun–Thu in most of the Gulf), and never in the afternoon of the last working day of their week (Friday; Thursday in the
Gulf), when B2B mail is read least. The campaign's UTC window/days then don't apply. Which buyers go first is still the
ranked list: campaign_service.local_plan reserves each UTC day's quota for the best-ranked buyers whose hours still come
that day. Everything here is pure (no DB) and deterministic for a given `now`.
"""
import re
from datetime import date, datetime, time, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover — Python < 3.9
    from backports.zoneinfo import ZoneInfo           # type: ignore

UTC = timezone.utc
DEFAULT_HOURS = "09:00-11:00,14:00-16:00"

# the business time zone of each country (the capital's / main commercial centre's zone)
COUNTRY_TZ = {
    # Europe
    "AD": "Europe/Andorra", "AL": "Europe/Tirane", "AT": "Europe/Vienna", "BA": "Europe/Sarajevo",
    "BE": "Europe/Brussels", "BG": "Europe/Sofia", "BY": "Europe/Minsk", "CH": "Europe/Zurich", "CY": "Asia/Nicosia",
    "CZ": "Europe/Prague", "DE": "Europe/Berlin", "DK": "Europe/Copenhagen", "EE": "Europe/Tallinn",
    "ES": "Europe/Madrid", "FI": "Europe/Helsinki", "FR": "Europe/Paris", "GB": "Europe/London", "UK": "Europe/London",
    "GR": "Europe/Athens", "HR": "Europe/Zagreb", "HU": "Europe/Budapest", "IE": "Europe/Dublin",
    "IS": "Atlantic/Reykjavik", "IT": "Europe/Rome", "LI": "Europe/Vaduz", "LT": "Europe/Vilnius",
    "LU": "Europe/Luxembourg", "LV": "Europe/Riga", "MC": "Europe/Monaco", "MD": "Europe/Chisinau",
    "ME": "Europe/Podgorica", "MK": "Europe/Skopje", "MT": "Europe/Malta", "NL": "Europe/Amsterdam",
    "NO": "Europe/Oslo", "PL": "Europe/Warsaw", "PT": "Europe/Lisbon", "RO": "Europe/Bucharest",
    "RS": "Europe/Belgrade", "RU": "Europe/Moscow", "SE": "Europe/Stockholm", "SI": "Europe/Ljubljana",
    "SK": "Europe/Bratislava", "SM": "Europe/San_Marino", "TR": "Europe/Istanbul", "UA": "Europe/Kiev",
    "XK": "Europe/Belgrade",
    # Middle East, North Africa, Caucasus, Central Asia
    "AE": "Asia/Dubai", "BH": "Asia/Bahrain", "DZ": "Africa/Algiers", "EG": "Africa/Cairo", "IL": "Asia/Jerusalem",
    "IQ": "Asia/Baghdad", "IR": "Asia/Tehran", "JO": "Asia/Amman", "KW": "Asia/Kuwait", "LB": "Asia/Beirut",
    "LY": "Africa/Tripoli", "MA": "Africa/Casablanca", "OM": "Asia/Muscat", "PS": "Asia/Hebron", "QA": "Asia/Qatar",
    "SA": "Asia/Riyadh", "SY": "Asia/Damascus", "TN": "Africa/Tunis", "YE": "Asia/Aden", "AM": "Asia/Yerevan",
    "AZ": "Asia/Baku", "GE": "Asia/Tbilisi", "KZ": "Asia/Almaty", "KG": "Asia/Bishkek", "TJ": "Asia/Dushanbe",
    "TM": "Asia/Ashgabat", "UZ": "Asia/Tashkent", "AF": "Asia/Kabul",
    # Sub-Saharan Africa
    "ZA": "Africa/Johannesburg", "NG": "Africa/Lagos", "KE": "Africa/Nairobi", "GH": "Africa/Accra",
    "ET": "Africa/Addis_Ababa", "TZ": "Africa/Dar_es_Salaam", "UG": "Africa/Kampala", "SN": "Africa/Dakar",
    "CI": "Africa/Abidjan", "CM": "Africa/Douala", "AO": "Africa/Luanda", "MZ": "Africa/Maputo", "ZM": "Africa/Lusaka",
    "ZW": "Africa/Harare", "BW": "Africa/Gaborone", "NA": "Africa/Windhoek", "MU": "Indian/Mauritius",
    "SD": "Africa/Khartoum",
    # Asia, Oceania
    "CN": "Asia/Shanghai", "HK": "Asia/Hong_Kong", "TW": "Asia/Taipei", "JP": "Asia/Tokyo", "KR": "Asia/Seoul",
    "IN": "Asia/Kolkata", "PK": "Asia/Karachi", "BD": "Asia/Dhaka", "LK": "Asia/Colombo", "NP": "Asia/Kathmandu",
    "SG": "Asia/Singapore", "MY": "Asia/Kuala_Lumpur", "TH": "Asia/Bangkok", "VN": "Asia/Ho_Chi_Minh",
    "PH": "Asia/Manila", "ID": "Asia/Jakarta", "KH": "Asia/Phnom_Penh", "MM": "Asia/Yangon", "MN": "Asia/Ulaanbaatar",
    "AU": "Australia/Sydney", "NZ": "Pacific/Auckland", "FJ": "Pacific/Fiji", "PG": "Pacific/Port_Moresby",
    # Americas
    "US": "America/New_York", "CA": "America/Toronto", "MX": "America/Mexico_City", "BR": "America/Sao_Paulo",
    "AR": "America/Argentina/Buenos_Aires", "CL": "America/Santiago", "CO": "America/Bogota", "PE": "America/Lima",
    "VE": "America/Caracas", "EC": "America/Guayaquil", "UY": "America/Montevideo", "PY": "America/Asuncion",
    "BO": "America/La_Paz", "CR": "America/Costa_Rica", "PA": "America/Panama", "GT": "America/Guatemala",
    "DO": "America/Santo_Domingo", "PR": "America/Puerto_Rico", "JM": "America/Jamaica",
}
# countries spanning several zones: the buyer's city/state (as written in dest_city) picks the zone
_CITY_TZ = {
    "AU": ((r"perth|\bwa\b|western australia|fremantle|bunbury", "Australia/Perth"),
           (r"adelaide|\bsa\b|south australia", "Australia/Adelaide"),
           (r"darwin|\bnt\b|northern territory", "Australia/Darwin"),
           (r"brisbane|\bqld\b|queensland|gold coast|sunshine coast|cairns|townsville|toowoomba|wacol|murarrie|"
            r"slacks creek", "Australia/Brisbane"),
           (r"hobart|launceston|\btas\b|tasmania", "Australia/Hobart"),
           (r"melbourne|\bvic\b|victoria|geelong|dandenong|keilor|hoppers crossing|carrum", "Australia/Melbourne")),
    "CA": ((r"vancouver|burnaby|surrey|richmond|kelowna|victoria|\bbc\b|british columbia|langley|pitt meadows|"
            r"abbotsford", "America/Vancouver"),
           (r"calgary|edmonton|red deer|lethbridge|\bab\b|alberta", "America/Edmonton"),
           (r"regina|saskatoon|\bsk\b|saskatchewan", "America/Regina"),
           (r"winnipeg|brandon|\bmb\b|manitoba", "America/Winnipeg"),
           (r"halifax|dartmouth|moncton|fredericton|saint john|tracadie|\bns\b|\bnb\b|\bpe\b|nova scotia|"
            r"new brunswick|prince edward", "America/Halifax"),
           (r"st\.? john'?s|\bnl\b|newfoundland", "America/St_Johns")),
    "US": ((r"los angeles|san francisco|san diego|san jose|seattle|portland|las vegas|sacramento|oakland|long beach|"
            r"\bca\b|\bwa\b|\bnv\b|california|washington state|oregon|nevada", "America/Los_Angeles"),
           (r"denver|salt lake|albuquerque|boise|\but\b|\bnm\b|colorado|utah", "America/Denver"),
           (r"phoenix|tucson|\baz\b|arizona", "America/Phoenix"),
           (r"chicago|houston|dallas|austin|san antonio|minneapolis|st\.? louis|kansas city|nashville|new orleans|"
            r"milwaukee|irving|carrollton|\btx\b|\bil\b|\bmn\b|\bmo\b|\bwi\b|texas|illinois", "America/Chicago")),
    "MX": ((r"tijuana|mexicali|ensenada|baja california", "America/Tijuana"),
           (r"cancun|canc[uú]n|quintana roo|chetumal|playa del carmen", "America/Cancun"),
           (r"hermosillo|sonora", "America/Hermosillo"),
           (r"chihuahua|ciudad ju[aá]rez", "America/Chihuahua"),
           (r"mazatl[aá]n|culiac[aá]n|sinaloa|la paz|nayarit|tepic", "America/Mazatlan")),
    "BR": ((r"manaus|amazonas", "America/Manaus"),),
    "RU": ((r"novosibirsk", "Asia/Novosibirsk"), (r"yekaterinburg", "Asia/Yekaterinburg"),
           (r"vladivostok", "Asia/Vladivostok")),
    "ID": ((r"bali|denpasar|makassar", "Asia/Makassar"),),
    "KZ": ((r"aktau|atyrau|aktobe|oral|uralsk", "Asia/Aqtau"),),
}
# weekend days (Mon=0 … Sun=6); everything else is Sat–Sun
_FRI_SAT = (4, 5)
WEEKEND = {iso: _FRI_SAT for iso in ("SA", "KW", "QA", "BH", "OM", "IL", "EG", "JO", "IQ", "YE", "DZ", "LY", "SD", "SY",
                                     "PS")}
WEEKEND.update({"IR": (3, 4), "AF": (3, 4), "NP": (5,)})
_HOURS_RE = re.compile(r"^\s*([01]?\d|2[0-4]):([0-5]\d)\s*-\s*([01]?\d|2[0-4]):([0-5]\d)\s*$")
_ZONES = {}


def parse_hours(text) -> list:
    """'09:00-11:00, 14:00-16:00' → [(540, 660), (840, 960)] (minutes after local midnight), sorted, at most 4, each
    start < end ≤ 24:00. Anything malformed → [] (the caller treats [] as 'not set')."""
    out = []
    for part in [p for p in re.split(r"[,;]", text or "") if p.strip()]:
        m = _HOURS_RE.match(part)
        if not m:
            return []
        a, b = int(m.group(1)) * 60 + int(m.group(2)), int(m.group(3)) * 60 + int(m.group(4))
        if not (0 <= a < b <= 24 * 60):
            return []
        out.append((a, b))
    out.sort()
    if len(out) > 4 or any(out[i][1] > out[i + 1][0] for i in range(len(out) - 1)):
        return []
    return out


def format_hours(hours) -> str:
    return ",".join(f"{a // 60:02d}:{a % 60:02d}-{b // 60:02d}:{b % 60:02d}" for a, b in hours)


def _zone(name):
    if name not in _ZONES:
        try:
            _ZONES[name] = ZoneInfo(name)
        except Exception:  # noqa: BLE001 — an unknown zone name must never stop sending: fall back to UTC
            _ZONES[name] = UTC
    return _ZONES[name]


def buyer_zone(country, city="") -> tuple:
    """(tzinfo, zone name, weekend days) for a buyer. Unknown country → UTC with a Sat–Sun weekend."""
    iso = (country or "").strip().upper()[:2]
    name = COUNTRY_TZ.get(iso, "UTC")
    hay = (city or "").lower()
    for pattern, zone in _CITY_TZ.get(iso, ()):
        if hay and re.search(pattern, hay):
            name = zone
            break
    return (_zone(name) if name != "UTC" else UTC), name, WEEKEND.get(iso, (5, 6))


def _utc(dt):
    return dt.astimezone(UTC).replace(tzinfo=None)


def windows_on_utc_day(tz, weekend, hours, day_start) -> list:
    """[(start, end)] in naive UTC: the buyer's local send windows that START within the UTC day beginning at
    `day_start` (naive UTC midnight). A window is on a local working day; on the last working day of the local week
    only the morning windows (starting before 12:00) count."""
    out = []
    day_end = day_start + timedelta(days=1)
    local_mid = day_start.replace(tzinfo=UTC).astimezone(tz).date()
    for d in (local_mid - timedelta(days=1), local_mid, local_mid + timedelta(days=1)):
        if d.weekday() in weekend:
            continue
        last_day = (d.weekday() + 1) % 7 in weekend
        for a, b in hours:
            if last_day and a >= 12 * 60:
                continue
            start = _utc(datetime.combine(d, time(a // 60, a % 60), tzinfo=tz))
            end = _utc(datetime.combine(d + timedelta(days=b // (24 * 60)), time((b // 60) % 24, b % 60), tzinfo=tz))
            if day_start <= start < day_end:
                out.append((start, end))
    return sorted(out)


def local_now(tz, now) -> datetime:
    """`now` (naive UTC) on the buyer's clock (aware)."""
    return now.replace(tzinfo=UTC).astimezone(tz)


def in_local_hours(tz, weekend, hours, at) -> bool:
    """True if `at` (naive UTC) is inside one of the buyer's send windows."""
    day_start = at.replace(hour=0, minute=0, second=0, microsecond=0)
    return any(s <= at < e for d in (day_start - timedelta(days=1), day_start)
               for s, e in windows_on_utc_day(tz, weekend, hours, d))


def next_window_start(tz, weekend, hours, after, horizon_days=10):
    """The first window start at or after `after` (naive UTC), or None within `horizon_days`; a window already open at
    `after` returns `after`."""
    day = after.replace(hour=0, minute=0, second=0, microsecond=0)
    for k in range(-1, horizon_days):
        for s, e in windows_on_utc_day(tz, weekend, hours, day + timedelta(days=k)):
            if e > after:
                return max(s, after)
    return None


def today_local(tz, now) -> date:
    return local_now(tz, now).date()
