.PHONY: install run seed save clean

# One-time setup: create a virtualenv and install dependencies.
install:
	python3 -m venv .venv
	./.venv/bin/pip install -U pip
	./.venv/bin/pip install -r requirements.txt
	@echo "\n✓ Setup done. Run 'make run' to start go4it on http://localhost:8400"

# Start the app with auto-reload on http://localhost:8400
run:
	./.venv/bin/uvicorn app.main:app --reload --port 8400

# Load demo demands & offers so you can see matching immediately.
seed:
	./.venv/bin/python -m app.seed

# End-of-day save: commit everything and push to GitHub with a status report.
save:
	./save.sh

# Remove the local database (start fresh).
clean:
	rm -f data.db data.db-journal
