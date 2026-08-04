# go4it — go-live checklist (founder-gated steps)

Everything in the code is done and tested. These are the steps only you can do (they need real
accounts, a server, and decisions). The app now **refuses to boot on a public URL with default
secrets**, so it will actively remind you about the critical ones.

## 1. Secrets (required — the app enforces this)
Set these in `.env` on the server. On a public `BASE_URL` the app will not start until they differ
from the shipped defaults:
```
SECRET_KEY=<64 random hex chars>          # python -c "import secrets;print(secrets.token_hex(32))"
GO4IT_INGEST_KEY=<another random string>  # the browser-capture API key
BASE_URL=https://yourdomain.com           # public host (used in pro-forma links + alerts)
```

## 2. Host + domain + TLS
Deployment scaffolding is ready (`Dockerfile`, `docker-compose.yml`, `Makefile`, `docs/PRODUCTION.md`).
- Provision a small server, point a domain at it.
- Put TLS in front (Caddy or nginx — `docs/PRODUCTION.md` has configs).
- `make build && make prod` (or `docker compose up -d`). Keep `WEB_CONCURRENCY` at 1–2 (SQLite).
- Run the worker as **one** process for ingest/enrichment/inbound-email.

## 3. Real admin + remove demo logins
```
# create your real admin (see docs/PRODUCTION.md for the snippet)
# then delete the demo seed users: admin@go4it.local / sara@ / ali@
```

## 4. Migrations
After any pull that adds columns, run before starting:
```
make db-migrate        # idempotent ALTER TABLE ADD COLUMN (scripts/migrate.py)
```

## 5. Turn on two-way email (optional but high-value)
Follow **docs/EMAIL_SETUP.md** — set `SMTP_*` + `IMAP_*` + `IMAP_INTERVAL>0`, run the worker. Then the
Conversation panel sends + receives, and buyers can Accept/Request-changes on the pro-forma.

## 6. Lock down CORS (behind a public domain)
Default allows the go4world capture helper + localhost. If you don't use the browser helper in prod,
set `CORS_ORIGINS` to just your domain:
```
CORS_ORIGINS=https://yourdomain.com
```

## 7. Backups offsite
`backups/` is written locally; schedule an offsite copy (cron + rclone/S3). `data.db` and `deal_docs/`
hold everything.

## 8. Business data (not code — your numbers)
- Real cost params / freight rate cards / FX in **/rates** (placeholders ship in).
- Real product catalog in **/catalog** (CSV import).
- Add more product lines: drop a spec in `docs/research/lines/<slug>.json` and run
  `harvest_line -> enrich_line -> demand_scout -> load_line -> export_line` (see any existing spec as a
  template). No code changes.

---
Done in code (no action needed): the whole spec-driven buyer pipeline + generic `/lines` hub, the
demand scout, the "where should I sell?" recommender, on-platform buyer acceptance + won-gate,
compliance-doc uploads, header-threaded inbound email, the secret guard, and CORS allowlist.
