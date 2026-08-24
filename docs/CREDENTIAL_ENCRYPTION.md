# Mailbox credential encryption (Phase 4)

Mailbox SMTP/IMAP credentials (app-passwords) are the only reversible secrets Go4it stores. They are
encrypted **at rest** and never rendered, logged, exported, or returned by any API.

## How it works
- **Cipher:** Fernet — AES-128-CBC + HMAC-SHA256. Authenticated (tamper-evident) and **reversible**
  (unlike a password *hash*; we must be able to decrypt to log in to the mail provider).
- **Key:** derived from the app secret `SECRET_KEY` (env). Not hard-coded. `docker-compose`/`.env` supplies it.
- **Write path:** `app.outreach.mail_encrypt()` on every save. **Read path:** `mail_decrypt()` only inside the
  SMTP/IMAP send/fetch code — a decrypted secret never reaches a template, response, log line, or audit record.
- **Fail closed:** on a **public** deployment (`BASE_URL` not localhost) still using the shipped-default
  `SECRET_KEY`, `mail_encrypt`/`mail_decrypt` **refuse to operate** (`_encryption_key_ok()`), and the app
  refuses to boot at all (`app/main.py` startup guard). Localhost dev is exempt.

## Verify there is no plaintext / migrate legacy plaintext
```bash
./.venv/bin/python scripts/encrypt_credentials.py --dry-run   # counts plaintext creds; changes nothing
./.venv/bin/python scripts/encrypt_credentials.py             # encrypts any plaintext, verify-before-persist
```
New installs already store ciphertext, so the dry-run normally reports `plaintext=0`. The migration is
**idempotent** (re-run → 0), **verifies** each new token round-trips **before** replacing the old value, and
is **transactional** (any failure rolls the whole batch back).

## Key rotation (zero credential loss)
```bash
./.venv/bin/python scripts/backup_db.py                       # 1) dated snapshot
# 2) in .env: keep the CURRENT key as GO4IT_OLD_SECRET_KEY, set the NEW key as SECRET_KEY
GO4IT_OLD_SECRET_KEY=<old> ./.venv/bin/python scripts/encrypt_credentials.py --rotate           # 3) dry-run
GO4IT_OLD_SECRET_KEY=<old> ./.venv/bin/python scripts/encrypt_credentials.py --rotate --apply    # 4) apply
# 5) remove GO4IT_OLD_SECRET_KEY once verified
```
Each token is decrypted with the **old** key and re-encrypted with the **new** key, proven decryptable
**before** the row is written. Secrets are never printed by any command.

## Guarantees under test (`tests/test_credentials.py`)
Round-trip; ciphertext never contains the plaintext; the raw DB column holds no plaintext; fail-closed on a
public deployment with the default key; dry-run detects planted plaintext; apply encrypts + is idempotent;
rotation re-keys and the old key no longer decrypts.
