# Production migration — Phase 4 crash-safe send layer

Explicit, idempotent procedure. Do **not** rely on ORM `create_all` alone in production.

## Deployment order (credential key FIRST — mandatory)
The application must **never** be deployed to production without its dedicated credential-encryption key
already configured. Follow this order exactly:

1. **Pause the campaign + email workers** (stop `worker.py --campaigns` / any send loop) so nothing sends or
   reads credentials mid-migration.
2. **Generate a strong dedicated key:** `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
3. **Store it in the production secret manager / environment** as `CREDENTIAL_ENCRYPTION_KEYS`. **Never commit
   it** to Git or a tracked `.env`. (A leaked key defeats the encryption.)
4. **Back up + integrity-check** the production database: `./.venv/bin/python scripts/backup_db.py`.
5. **Deploy the production-gate code.**
6. **Run migrations + credential rewrapping** (steps below).
7. **Restart the workers only after verification** (migration verified + credential dry-run reviewed).

## Migration steps (each is idempotent)
```bash
./.venv/bin/python scripts/backup_db.py            # dated snapshot + integrity check (REQUIRED)
./.venv/bin/python scripts/migrate.py              # additive columns (incl. campaignsend.rfc_message_id,
                                                   #   mailaccount.cred_enc_version)
./.venv/bin/python scripts/migrate_gate.py --dry-run   # preview: pending ops + pre counts, no changes
./.venv/bin/python scripts/migrate_gate.py             # apply: campaignsend table + unique constraint +
                                                       #   lease/retry/status/rfc indexes; verifies + asserts
                                                       #   operational counts invariant
./.venv/bin/python scripts/encrypt_credentials.py --dry-run   # OBSERVE the plaintext count (see below)
```
`migrate_gate.py` runs a `PRAGMA integrity_check` first, prints PRE/POST operational counts, aborts if any
operational count changes, and verifies the indexes/constraint exist before reporting success. Re-running any
step performs zero duplicate operations.

## Credential rewrapping — the dry-run count is an OBSERVED result
Do **not** assume production has zero plaintext credentials. Treat the `--dry-run` output as an observation:
- **If the plaintext count is 0:** record and verify it (spot-check that a stored credential decrypts), then
  proceed.
- **If plaintext credentials exist:** encrypt and verify them **before** starting the workers —
  `./.venv/bin/python scripts/encrypt_credentials.py` (or `--rewrap --apply` to move onto the dedicated key).
  The tool verifies each new ciphertext round-trips **before** replacing the old value, so the original is
  never deleted or overwritten until encrypted persistence + decryption are confirmed.
- Credentials are **never printed** in migration output.
Only after the credential state is verified do you restart the workers (step 7 above).

## What the gate creates
- `campaignsend` table
- unique `uq_campaignsend_crvs (campaign_id, recipient_id, sequence_version, step_index)` — the send-idempotency
- `ix_campaignsend_lease_expires_at`, `ix_campaignsend_next_attempt_at` — recovery/retry scans
- `ix_campaignsend_status`, `ix_campaignsend_rfc_message_id`, `ix_campaignsend_campaign_id`,
  `ix_campaignsend_recipient_id`
- `campaignsend.rfc_message_id` — durable RFC Message-ID
- `mailaccount.cred_enc_version` — encryption-scheme metadata

## Rollback / recovery
- **Pre-go-live rollback** (nothing has sent yet): restore the dated backup from `backups/` — e.g.
  `cp backups/data-YYYYMMDD-HHMMSS.db data.db`. The schema additions are non-destructive, so simply not using
  them is also safe.
- **Post-go-live recovery**: the gate is **purely additive** (new table / columns / indexes) — recovery never
  needs to drop them. For the legacy-outreach data backfill use `scripts/backfill_outreach.py --recover`
  (removes only untouched inferred groups; never a group with recipients). For credentials use
  `scripts/encrypt_credentials.py --rewrap` (key rotation; unreadable tokens are left untouched).
- **Never deleted by any path:** suppression, bounce, outreach, reply, or campaign history.

## Verified on a disposable copy
`tests/test_migrate_gate.py`: dry-run changes nothing · apply creates table+constraint+indexes+columns ·
re-run is a no-op · operational counts + history preserved. Manually proven on a copy of the live dev DB
(dry-run → apply → idempotent re-run; leads 4581 / outreach 197 / requests 1 unchanged; app starts).
