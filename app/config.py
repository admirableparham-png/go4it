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
GO4WORLD_ENABLED = bool(GO4WORLD_EMAIL and GO4WORLD_PASSWORD)
