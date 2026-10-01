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
# Phase 11: the password may be left out — the poller then uses the App Password already stored (encrypted) on the
# connected mailbox with that address, so the secret never has to be typed into .env.
IMAP_ENABLED = bool(IMAP_HOST and IMAP_USER)
IMAP_INTERVAL = int(os.getenv("IMAP_INTERVAL", "0"))        # seconds between inbox polls; 0 = disabled
# Phase 11: the poller reads the last N days (read-only, never marks mail as read) and remembers what it handled
IMAP_LOOKBACK_DAYS = int(os.getenv("IMAP_LOOKBACK_DAYS", "3"))

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
# Signature appended to outreach emails. Override in .env with OUTREACH_SIGNATURE (use \n for line breaks).
OUTREACH_SIGNATURE = os.getenv("OUTREACH_SIGNATURE", "").replace("\\n", "\n").strip()

# --- Telegram alert volume ---
# Routine "email sent / follow-up N sent" pings are OFF by default: the founder only wants to be pinged
# when a REAL buyer REPLIES (money-emoji alert), not on every outgoing email. Flip to true in .env to
# watch a batch go out live. Buyer-reply / bounce / needs-call alerts are unaffected by this switch.
TELEGRAM_NOTIFY_SENDS = os.getenv("TELEGRAM_NOTIFY_SENDS", "false").strip().lower() == "true"

# --- Auto follow-up sequence (processed by the worker) ---
# Master switch: OFF by default so nothing auto-sends until you arm it. The system only ever follows up
# leads whose FIRST email you sent yourself; it never cold-emails a buyer on its own.
FOLLOWUP_ENABLED = os.getenv("FOLLOWUP_ENABLED", "false").strip().lower() == "true"
FOLLOWUP_INTERVAL = int(os.getenv("FOLLOWUP_INTERVAL", "1800"))    # seconds between follow-up sweeps; 0=off
FOLLOWUP_DAYS_1 = int(os.getenv("FOLLOWUP_DAYS_1", "3"))           # calendar days: main email -> follow-up 1
FOLLOWUP_BDAYS_2 = int(os.getenv("FOLLOWUP_BDAYS_2", "5"))         # business days: follow-up 1 -> follow-up 2
FOLLOWUP_GRACE_BDAYS = int(os.getenv("FOLLOWUP_GRACE_BDAYS", "3"))  # business days: follow-up 2 -> "call" nudge

# --- Concierge request reminders (worker pings the founder about stale 'submitted' requests) ---
REQUEST_REMINDER_HOURS = int(os.getenv("REQUEST_REMINDER_HOURS", "12"))       # a request pending this long -> ping
REQUEST_REMINDER_INTERVAL = int(os.getenv("REQUEST_REMINDER_INTERVAL", "3600"))  # sweep cadence seconds; 0=off

# --- Mailbox credential encryption (dedicated key, separate from SECRET_KEY) -
# Authenticated-encryption keys for mailbox SMTP/IMAP credentials at rest. Comma-separated: the FIRST key
# encrypts all new/rotated values; the rest are decrypt-only fallbacks for zero-downtime rotation. Kept
# SEPARATE from the session SECRET_KEY so rotating one never touches the other. Required on a public deploy
# (fail-closed); on localhost we fall back to SECRET_KEY so dev keeps working. Never hard-code a real key.
CREDENTIAL_ENCRYPTION_KEYS = [k.strip() for k in os.getenv("CREDENTIAL_ENCRYPTION_KEYS", "").split(",")
                              if k.strip()]

# --- Production canary (founder-only, allowlist-gated) ----------------------
# The live canary is OFF unless CANARY_ENABLED is set AND an allowlist of permitted test addresses is given.
# It refuses to send to anything outside the allowlist and never uses a production campaign audience.
CANARY_ENABLED = os.getenv("CANARY_ENABLED", "").lower() in ("1", "true", "yes", "on")
CANARY_ALLOWLIST = [e.strip().lower() for e in os.getenv("CANARY_ALLOWLIST", "").split(",") if e.strip()]
CANARY_TEST_RECIPIENT = os.getenv("CANARY_TEST_RECIPIENT", "").strip().lower()   # must be inside the allowlist
CANARY_BAD_RECIPIENT = os.getenv("CANARY_BAD_RECIPIENT", "").strip().lower()     # known-invalid addr for bounce test

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

# Phase 8 — opportunity scoring weights (points on a 0-100 scale). CONFIGURABLE + AUDITED: each score records a
# version derived from these weights, so changing a weight produces a NEW score_version and never silently
# rewrites a historical snapshot. Every component is visible in the score breakdown — no hidden weights.
OPP_SCORE_WEIGHTS = {
    "demand_strength": float(os.getenv("OPP_W_DEMAND_STRENGTH", "22")),
    "signal_quality": float(os.getenv("OPP_W_SIGNAL_QUALITY", "14")),
    "signal_freshness": float(os.getenv("OPP_W_SIGNAL_FRESHNESS", "10")),
    "independent_sources": float(os.getenv("OPP_W_INDEPENDENT_SOURCES", "10")),
    "supply_available": float(os.getenv("OPP_W_SUPPLY", "16")),
    "supplier_readiness": float(os.getenv("OPP_W_SUPPLIER_READINESS", "8")),
    "quote_deal_evidence": float(os.getenv("OPP_W_QUOTE_DEAL", "12")),
    "feasibility": float(os.getenv("OPP_W_FEASIBILITY", "8")),
    "missing_data_penalty": float(os.getenv("OPP_W_MISSING_PENALTY", "20")),   # subtracted, not added
}

# --- AI Command (Phase 9) — provider-neutral copilot config ------------------------------------------
# No API key ever lives in source. Credentials come from the environment / a secret manager. The copilot works
# DETERMINISTICALLY (internal search + metric lookup + citations + proposals) even with NO provider configured;
# an LLM provider is an OPTIONAL orchestration/phrasing layer. A paid/live provider is NEVER called in tests.
AI_PROVIDER = os.getenv("AI_PROVIDER", "").strip().lower()          # "" | mock | anthropic | openai
AI_MODEL = os.getenv("AI_MODEL", "").strip()
AI_API_KEY = os.getenv("AI_API_KEY", "").strip()                    # read only; NEVER rendered or logged
AI_API_BASE = os.getenv("AI_API_BASE", "").strip()                 # optional custom endpoint (no invented ones)
AI_MODEL_ALLOWLIST = [m.strip() for m in os.getenv("AI_MODEL_ALLOWLIST", "").split(",") if m.strip()]
AI_ENABLED = os.getenv("AI_ENABLED", "false").strip().lower() == "true"
AI_TIMEOUT_S = float(os.getenv("AI_TIMEOUT_S", "30"))
AI_MAX_OUTPUT_TOKENS = int(os.getenv("AI_MAX_OUTPUT_TOKENS", "1500"))
AI_MAX_TOOL_STEPS = int(os.getenv("AI_MAX_TOOL_STEPS", "6"))
AI_MAX_RETRIES = int(os.getenv("AI_MAX_RETRIES", "1"))              # read/draft work only — never retry a write
AI_DAILY_TOKEN_LIMIT = int(os.getenv("AI_DAILY_TOKEN_LIMIT", "500000"))     # per-admin/day
AI_CONVERSATION_TOKEN_LIMIT = int(os.getenv("AI_CONVERSATION_TOKEN_LIMIT", "80000"))
AI_TENANT_DAILY_BUDGET_USD = float(os.getenv("AI_TENANT_DAILY_BUDGET_USD", "10"))
AI_CONCURRENCY = int(os.getenv("AI_CONCURRENCY", "4"))
# Per-request/per-conversation cost ceiling (USD). A request is refused before calling the provider if its
# projected cost would exceed the per-request cap; the deterministic answer is always still available.
AI_MAX_REQUEST_COST_USD = float(os.getenv("AI_MAX_REQUEST_COST_USD", "0.50"))
# Rough per-model USD price per 1K tokens (input, output). Used only for cost estimation/limits — not billing.
# Current Claude lineup from Anthropic's model overview (per-MTok /1000): sonnet-5 $2/$10, opus-5 $5/$25,
# haiku-4.5 $1/$5. Claude 3.5 is DEPRECATED — kept only so a pinned legacy deployment still costs correctly.
AI_MODEL_PRICES = {
    "claude-sonnet-5": (0.002, 0.010), "claude-opus-5": (0.005, 0.025), "claude-fable-5": (0.010, 0.050),
    "claude-haiku-4-5": (0.001, 0.005), "claude-haiku-4-5-20251001": (0.001, 0.005),
    "claude-sonnet-4-6": (0.003, 0.015), "claude-sonnet-4-5": (0.003, 0.015),   # legacy, still available
    "claude-3-5-haiku": (0.0008, 0.004), "claude-3-5-sonnet": (0.003, 0.015),   # deprecated
    "gpt-4o-mini": (0.00015, 0.0006), "gpt-4o": (0.0025, 0.01), "mock-1": (0.0, 0.0),
}
AI_PRICE_DEFAULT = (float(os.getenv("AI_PRICE_IN_PER_1K", "0.003")),
                    float(os.getenv("AI_PRICE_OUT_PER_1K", "0.015")))
# Where cross-process AI control flags (Pause-All + per-conversation cancel) live. Empty => derived: the DB
# directory on a public sqlite deployment (a shared volume, so every gunicorn worker AND the worker container
# observe the same flag), else the system temp dir (dev/tests). Shared state, never process-local memory.
AI_CONTROL_DIR = os.getenv("AI_CONTROL_DIR", "").strip()
# A dedicated, rotating key set for encrypting AI conversation content at rest — kept SEPARATE from SECRET_KEY
# and CREDENTIAL_ENCRYPTION_KEYS. keys[0] encrypts; the rest are decrypt-only rotation fallbacks.
AI_DATA_ENCRYPTION_KEYS = [k.strip() for k in os.getenv("AI_DATA_ENCRYPTION_KEYS", "").split(",") if k.strip()]
# Live-provider allowlist — only these admin emails may use a REAL provider (the live canary gate).
AI_LIVE_ALLOWLIST = [e.strip().lower() for e in os.getenv("AI_LIVE_ALLOWLIST", "").split(",") if e.strip()]
