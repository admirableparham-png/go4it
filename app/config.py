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
