.PHONY: install run seed test save clean worker ingest ingest-portal

# One-time setup: create a virtualenv and install dependencies.
install:
	python3 -m venv .venv
	./.venv/bin/pip install -U pip
	./.venv/bin/pip install -r requirements.txt
	@printf "\n✓ Setup done. Run 'make run' to start go4it on http://localhost:8400\n"

# Start the app with auto-reload on http://localhost:8400
run:
	./.venv/bin/uvicorn app.main:app --reload --port 8400

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

# Remove the local database (start fresh).
clean:
	rm -f data.db data.db-journal data.db-wal data.db-shm
