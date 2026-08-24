# Mailbox credential encryption (Phase 4)

Mailbox SMTP/IMAP credentials (app-passwords) are the only reversible secrets Go4it stores. They are
encrypted **at rest** under a **dedicated key** and never rendered, logged, exported, or returned by any API.

## How it works
- **Cipher:** Fernet — AES-128-CBC + HMAC-SHA256. Authenticated (tamper-evident) and **reversible** (we must
  decrypt to log in to the mail provider — this is not a password *hash*).
- **Dedicated key, separate from `SECRET_KEY`:** `CREDENTIAL_ENCRYPTION_KEYS` (env, comma-separated).
  - The **first** key encrypts all new/rotated values.
  - The remaining keys are **decrypt-only fallbacks** for zero-downtime rotation.
  - The session `SECRET_KEY` is **never** used to encrypt new values; it remains only as a final
    **decrypt-only** fallback so pre-migration ciphertext still reads until it is re-wrapped.
- **Not hard-coded:** keys come from the environment only.
- **Write path:** `mail_encrypt()` (current dedicated key). **Read path:** `mail_decrypt()` tries every
  dedicated key, then the legacy `SECRET_KEY`. A decrypted secret never reaches a template, response, log, or
  audit record.
- **Fail closed:** on a **public** deployment (`BASE_URL` not localhost) `mail_encrypt`/`mail_decrypt`
  **refuse to operate** unless `CREDENTIAL_ENCRYPTION_KEYS[0]` is set to a strong (non-default) value.
  Localhost dev is exempt and may fall back to `SECRET_KEY` so development keeps working.

## Generate a key
```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
# put it in .env:  CREDENTIAL_ENCRYPTION_KEYS=<that value>
```

## Verify there is no plaintext / encrypt legacy plaintext
```bash
./.venv/bin/python scripts/encrypt_credentials.py --dry-run   # counts plaintext creds; changes nothing
./.venv/bin/python scripts/encrypt_credentials.py             # encrypts any plaintext, verify-before-persist
```
Idempotent (re-run → 0), verifies each new token round-trips **before** replacing the old value, transactional.

## One-time migration off SECRET_KEY, and key rotation (same command)
```bash
./.venv/bin/python scripts/backup_db.py                       # 1) dated snapshot
# 2) in .env, set the dedicated key list:
#    migrating off SECRET_KEY:  CREDENTIAL_ENCRYPTION_KEYS=<new-key>     (SECRET_KEY is an automatic fallback)
#    rotating a dedicated key:  CREDENTIAL_ENCRYPTION_KEYS=<new-key>,<old-key>
./.venv/bin/python scripts/encrypt_credentials.py --rewrap            # 3) dry-run preview
./.venv/bin/python scripts/encrypt_credentials.py --rewrap --apply    # 4) apply
# 5) once every value is under the new key, drop <old-key> from CREDENTIAL_ENCRYPTION_KEYS
```
`--rewrap` decrypts each token with whatever key still opens it (dedicated fallbacks or the legacy
`SECRET_KEY`) and re-encrypts under the **current** key, proven decryptable **before** the row is written. A
token no key can open is **left untouched** (never destroyed). Idempotent; secrets and keys are never printed.

## Guarantees under test (`tests/test_credentials.py`)
No plaintext in the DB · new writes use the dedicated current key · an old dedicated key still decrypts during
rotation · SECRET_KEY-era ciphertext migrates · missing key fails closed in production · a wrong key never
destroys data · repeated migration is a no-op · no secret/key appears in logs or errors.
