# CLAUDE.md — go4it

go4it is a B2B trade-brokerage platform: Iranian/UAE supply → foreign B2B buyers. Go4it mediates all
buyer contact; sellers never see buyer identities. **Read `docs/HANDOFF.md` first** — it holds the current
state, what is deployed, and the next tasks.

## Commands

```bash
make install        # venv + deps (Python 3.9 .venv already exists)
make run            # dev server → http://localhost:8400
make test           # ./.venv/bin/python -m pytest -q   (617 tests at Phase 10)
make db-migrate     # scripts/migrate.py (idempotent)
make backup         # scripts/backup_db.py (WAL-safe online backup + integrity_check)
./save.sh           # daily save: DB backup → git add -A → commit → push current branch
```

Local DB = `data.db` (gitignored). Secrets = `.env` (gitignored). Never commit either.

## Production

- Live at **https://g4it.vip** on the Hetzner box `167.233.138.214` (shared with tradesitter), code at
  `/opt/go4it`, containers `go4it-app` + `go4it-worker` on network `go4it-net`, served by the tradesitter
  Caddy (`pulse3300_caddy`). Prod secrets only in `/opt/go4it/.env`.
- Deploy = `git archive <tag>` tar-over-ssh to `/opt/go4it` → `docker compose -f docker-compose.coexist.yml up -d --build`.
  Always `backup_db.py` first; run migration gates with `--dry-run` before applying.
- Every prod command targets `go4it-app`, never a tradesitter container.
- Deploys are the founder's decision: prepare, then wait for an explicit "deploy".

## Hard rules

- **Buyer confidentiality:** buyer/contact PII is admin-only. Sellers get the sanitized projection only
  (`app/pipeline.py`, `app/authz.py`). No permission, override, export or route may bypass this.
- **Buyer reports** (every one): Part 1 = buyers who posted an RFQ/request (2025–2026); Part 2 = potential
  bulk/wholesale buyers; bulk rows in a different colour; website always shown; **source never in the client
  report** (separate `_ADMIN` csv); no retail/junk; deep-sweep B2B RFQ platforms, not LLM guesses.
- **Email:** only from a connected Go4it mailbox; respect the suppression list; small batches; canary first.
- Tags are immutable — never move a tag. Don't modify `main` without the founder's say-so.
- Reply to the founder short and direct, in English.
