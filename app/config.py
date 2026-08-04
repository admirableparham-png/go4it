"""Central configuration, loaded from the environment / .env file."""
import os

from dotenv import load_dotenv

load_dotenv()

# Where data lives. Default: a single local SQLite file (zero setup).
# Swap to Postgres later with one line: postgresql+psycopg://user:pass@host/db
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///data.db")

# Telegram alerts (optional). Leave blank to disable — the app still works.
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Matching sensitivity (0-100). A demand/offer pair scoring >= this is a match.
MATCH_THRESHOLD = float(os.getenv("MATCH_THRESHOLD", "60"))

# Signed-cookie session secret. CHANGE THIS in production (set SECRET_KEY in .env).
SECRET_KEY = os.getenv("SECRET_KEY", "dev-insecure-change-me")

# Base URL used to build deep links in Telegram alerts.
BASE_URL = os.getenv("BASE_URL", "http://localhost:8400").rstrip("/")

# Lead ingestion: folder watched for go4worldbusiness CSV exports, and poll interval.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INBOX_DIR = os.getenv("INBOX_DIR", os.path.join(_PROJECT_ROOT, "inbox"))
INGEST_INTERVAL = int(os.getenv("INGEST_INTERVAL", "120"))
DEBUG_DIR = os.path.join(_PROJECT_ROOT, "debug")

# Background worker: periodically fill blank contacts on new website-bearing leads (app/enrich_service).
# OFF by default (0). Set ENRICH_INTERVAL to e.g. 3600 (hourly) to keep harvested leads outreach-ready.
ENRICH_INTERVAL = int(os.getenv("ENRICH_INTERVAL", "0"))    # seconds between enrich passes; 0 = disabled
ENRICH_BATCH = int(os.getenv("ENRICH_BATCH", "40"))         # max leads scraped per pass (stay polite)
# After a command-box harvest, immediately web-enrich that many of the just-found leads (fill blank
# email/phone from their own site) so a "find X in Y" reaches the same quality as the curated lines.
COMMAND_ENRICH_BATCH = int(os.getenv("COMMAND_ENRICH_BATCH", "40"))   # 0 = off

# Inbound buyer email (IMAP): the worker threads replies into the Conversation panel (app/inbound_email).
# OFF by default. Set IMAP_HOST/USER/PASSWORD (e.g. imap.gmail.com + an App Password) + IMAP_INTERVAL>0.
IMAP_HOST = os.getenv("IMAP_HOST", "").strip()
IMAP_PORT = int(os.getenv("IMAP_PORT", "993"))
IMAP_USER = os.getenv("IMAP_USER", "").strip()
IMAP_PASSWORD = os.getenv("IMAP_PASSWORD", "").strip()
IMAP_ENABLED = bool(IMAP_HOST and IMAP_USER and IMAP_PASSWORD)
IMAP_INTERVAL = int(os.getenv("IMAP_INTERVAL", "0"))        # seconds between inbox polls; 0 = disabled

# --- go4worldbusiness authenticated portal scraper (browser bot) ---
# Credentials for YOUR OWN paid account. Set in .env (gitignored) — never in code.
# Automated access may conflict with go4worldbusiness Terms / risk the account.
# The source is disabled unless both email and password are present.
GO4WORLD_EMAIL = os.getenv("GO4WORLD_EMAIL", "").strip()
GO4WORLD_PASSWORD = os.getenv("GO4WORLD_PASSWORD", "").strip()
GO4WORLD_LOGIN_URL = os.getenv("GO4WORLD_LOGIN_URL", "https://www.go4worldbusiness.com/login")
GO4WORLD_LEAD_URLS = [u.strip() for u in os.getenv(
    "GO4WORLD_LEAD_URLS",
    "https://www.go4worldbusiness.com/buyers/georgia/ceramic-tiles.html,"
    "https://www.go4worldbusiness.com/buyers/georgia/bricks.html",
).split(",") if u.strip()]
GO4WORLD_HEADLESS = os.getenv("GO4WORLD_HEADLESS", "true").lower() != "false"
GO4WORLD_INTERVAL = int(os.getenv("GO4WORLD_INTERVAL", "3600"))   # hourly
# SAFETY: the portal scraper stays OFF unless explicitly enabled. go4worldbusiness
# actively blocks bots ("Too many requests"); auto-running it can flag the account.
GO4WORLD_PORTAL_ENABLED = os.getenv("GO4WORLD_PORTAL_ENABLED", "false").strip().lower() == "true"
GO4WORLD_ENABLED = bool(GO4WORLD_EMAIL and GO4WORLD_PASSWORD and GO4WORLD_PORTAL_ENABLED)

# Key the in-browser helper (Tampermonkey userscript) uses to POST captured leads
# to /api/leads/raw. Change it in .env for anything beyond local use.
INGEST_API_KEY = os.getenv("GO4IT_INGEST_KEY", "go4it-local-key")

# --- Outreach email (optional SMTP) ---
# Leave blank to use click-to-email (mailto) + click-to-WhatsApp only; set these to send
# real email from inside go4it. For Gmail use an App Password, host smtp.gmail.com port 587.
SMTP_HOST = os.getenv("SMTP_HOST", "").strip()
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "").strip()
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "").strip()
SMTP_FROM = os.getenv("SMTP_FROM", "").strip() or SMTP_USER
SMTP_ENABLED = bool(SMTP_HOST and SMTP_USER and SMTP_PASSWORD)

# --- Production safety -------------------------------------------------------
# Refuse to boot on a PUBLIC BASE_URL while still using the shipped default secrets (a forgeable
# admin session / open ingest key). Local dev (localhost/127.0.0.1) is exempt so nothing changes there.
IS_LOCAL = any(h in BASE_URL for h in ("localhost", "127.0.0.1", "0.0.0.0"))
_DEFAULT_SECRETS = {"SECRET_KEY": ("dev-insecure-change-me", SECRET_KEY),
                    "GO4IT_INGEST_KEY": ("go4it-local-key", INGEST_API_KEY)}


def insecure_default_secrets():
    """Names of secrets still left at their shipped default (empty list = all overridden)."""
    return [name for name, (default, val) in _DEFAULT_SECRETS.items() if val == default]


# CORS origins allowed to call the browser-capture API. Default covers the go4world capture helper +
# localhost dev; set a comma-separated CORS_ORIGINS in prod to lock it down (avoid "*" behind a domain).
CORS_ORIGINS = [o.strip() for o in os.getenv(
    "CORS_ORIGINS",
    "https://www.go4worldbusiness.com,https://go4worldbusiness.com,http://localhost:8400"
).split(",") if o.strip()]
