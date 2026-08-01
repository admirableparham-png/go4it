.PHONY: install run prod seed test save backup db-migrate clean worker ingest ingest-portal

# One-time setup: create a virtualenv and install dependencies.
install:
	python3 -m venv .venv
	./.venv/bin/pip install -U pip
	./.venv/bin/pip install -r requirements.txt
	@printf "\n✓ Setup done. Run 'make run' to start go4it on http://localhost:8400\n"

# Start the app with auto-reload on http://localhost:8400
run:
	./.venv/bin/uvicorn app.main:app --reload --port 8400

# Production server: migrate, then gunicorn + uvicorn workers (no reload). WEB_CONCURRENCY sets workers.
prod:
	./.venv/bin/python scripts/migrate.py
	./.venv/bin/gunicorn app.main:app -k uvicorn.workers.UvicornWorker -w $${WEB_CONCURRENCY:-2} -b 0.0.0.0:8400 --timeout 120

# Load demo demands & offers so you can see matching immediately.
seed:
	./.venv/bin/python -m app.seed

# Run the test suite.
test:
	./.venv/bin/python -m pytest -q

# Ingest go4worldbusiness CSVs once (drop exports into ./inbox first).
ingest:
	./.venv/bin/python -m app.worker --once

# Run the background ingestion worker (inbox often + go4world portal hourly).
worker:
	./.venv/bin/python -m app.worker

# Portal-only pass: log into go4worldbusiness and capture the pages to ./debug
# (needs GO4WORLD_EMAIL/PASSWORD in .env). Use this for first-run selector tuning.
ingest-portal:
	./.venv/bin/python -m app.worker --portal

# End-of-day save: commit everything and push to GitHub with a status report.
save:
	./save.sh

# Apply lightweight idempotent schema migrations (adds any missing columns).
db-migrate:
	./.venv/bin/python scripts/migrate.py

# Timestamped SQLite backup into ./backups (online-consistent, keeps last 14).
backup:
	./.venv/bin/python scripts/backup_db.py

# Remove the local database (start fresh).
clean:
	rm -f data.db data.db-journal data.db-wal data.db-shm
