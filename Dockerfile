# go4it — production image (FastAPI under gunicorn + uvicorn workers).
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run unprivileged; give the app a writable data dir (mount a volume here in prod).
RUN useradd -m app && mkdir -p /app/var /app/backups && chown -R app:app /app
USER app

EXPOSE 8400

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8400/api/health || exit 1

# Apply additive migrations, then serve. WEB_CONCURRENCY sets worker count — keep it low
# (1-2) on SQLite (single-writer); raise it once you move DATABASE_URL to Postgres.
CMD ["sh", "-c", "python scripts/migrate.py && exec gunicorn app.main:app -k uvicorn.workers.UvicornWorker -w ${WEB_CONCURRENCY:-2} -b 0.0.0.0:8400 --timeout 120 --access-logfile -"]
