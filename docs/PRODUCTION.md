# Deploying go4it to production

go4it is a single FastAPI app. It runs fine on one small server. This is the short path from
the `--reload` dev server to something a team can use safely.

## 0. Before anything — secrets
Copy `.env.example` to `.env` and set at minimum:

```
SECRET_KEY=<python -c "import secrets; print(secrets.token_urlsafe(48))">
GO4IT_INGEST_KEY=<a random string>
BASE_URL=https://your-domain
```

Optional but recommended: `SMTP_*` (to send outreach email from the app) and `TELEGRAM_*`.
**Never commit `.env`** — it is gitignored.

## 1. Run it — Docker (simplest)

```bash
docker compose up -d --build
```

This builds the image, runs `scripts/migrate.py`, and serves under **gunicorn + uvicorn workers**
on `:8400`. The SQLite DB + inbox persist in the `go4it-data` volume; DB snapshots land in
`./backups`. Tune workers with `WEB_CONCURRENCY` (keep it 1–2 on SQLite; raise it on Postgres).

### Full production stack (app + worker + HTTPS) — for a live domain
`docker-compose.prod.yml` runs the app, the background **worker** (inbound email / follow-ups /
request reminders) and **Caddy** (automatic Let's Encrypt HTTPS) together. In `.env` set
`BASE_URL=https://your-domain`, `DOMAIN=your-domain`, `CORS_ORIGINS=https://your-domain`, then:

```bash
make deploy        # = docker compose -f docker-compose.prod.yml up -d --build
```

Persistent volumes cover the DB (`/app/var`), uploaded compliance docs (`/app/deal_docs`) **and**
delivered concierge files (`/app/request_files`) — all survive redeploys. The bundled `Caddyfile`
fronts the app at `$DOMAIN`; the manual reverse-proxy in §2 is only needed if you run your own.

## 1b. Run it — bare metal

```bash
make install          # venv + deps
make db-migrate       # apply additive schema changes
WEB_CONCURRENCY=2 make prod
```

`make prod` runs gunicorn (no reload). Put a reverse proxy in front for TLS (below).

## 2. HTTPS + a domain (reverse proxy)
Terminate TLS at a proxy and forward to the app on `:8400`. **Caddy** is the least effort
(automatic certificates). Minimal `Caddyfile`:

```
your-domain {
    reverse_proxy 127.0.0.1:8400
}
```

`caddy run` (or the Caddy Docker image alongside the app). nginx + certbot works too.

## 3. First run — create your admin (do NOT ship demo creds)
The seed users (`admin123` …) are for local demos only. On a fresh production DB, create a real
admin instead of running `make seed`:

```bash
docker compose exec app python - <<'PY'
from sqlmodel import Session
from app.db import engine, init_db
from app.auth import hash_password
from app.models import User
init_db()
with Session(engine) as s:
    s.add(User(email="you@company.com", name="You", role="admin",
               password_hash=hash_password("<a strong password>"), active=True))
    s.commit()
print("admin created")
PY
```

## 4. Postgres (when SQLite gets tight)
SQLite is single-writer; for a real team, move to Postgres:

```bash
pip install "psycopg[binary]"
# .env:
DATABASE_URL=postgresql+psycopg://user:pass@host:5432/go4it
```

`init_db()` creates the tables on first boot. Then you can raise `WEB_CONCURRENCY`.

## 5. Backups
- SQLite: `make backup` (online snapshot → `./backups`, keeps 14). `save.sh` runs it before each
  commit. Schedule it (cron/launchd) and sync `./backups` offsite (rclone/S3).
- Postgres: use `pg_dump` on a schedule instead.

## 6. Migrations
Additive schema changes live in `scripts/migrate.py` (idempotent `ALTER TABLE ADD COLUMN`).
Run `make db-migrate` after pulling changes; the Docker image runs it automatically on boot.
For complex migrations (renames/drops) graduate to Alembic.

## Checklist
- [ ] `SECRET_KEY` set (not the default) · `.env` not committed
- [ ] real admin user created · demo seed users removed/disabled
- [ ] HTTPS in front · `BASE_URL` = your https URL
- [ ] backups scheduled + synced offsite
- [ ] `WEB_CONCURRENCY` matches the DB (1–2 SQLite, more on Postgres)
