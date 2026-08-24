"""Mailbox credential encryption — migration + key rotation (Phase 4 production-gate).

Mailbox SMTP/IMAP credentials are stored ENCRYPTED at rest with Fernet (AES-128-CBC + HMAC-SHA256), keyed off
a DEDICATED CREDENTIAL_ENCRYPTION_KEYS list (separate from the session SECRET_KEY). The first key encrypts new
values; the rest are decrypt-only fallbacks for rotation. This tool (a) proves no plaintext is present,
(b) encrypts any legacy plaintext, and (c) re-wraps everything under the current key — which covers BOTH the
one-time migration off SECRET_KEY-encrypted ciphertext AND ongoing key rotation. Secrets/keys are NEVER printed.

    ./.venv/bin/python scripts/encrypt_credentials.py --dry-run   # count plaintext creds; change nothing
    ./.venv/bin/python scripts/encrypt_credentials.py             # encrypt any plaintext (verify-before-persist)
    ./.venv/bin/python scripts/encrypt_credentials.py --rewrap             # re-wrap under current key (dry)
    ./.venv/bin/python scripts/encrypt_credentials.py --rewrap --apply     # re-wrap under current key (apply)

ONE-TIME MIGRATION off SECRET_KEY, and KEY ROTATION (same command):
  1) backup_db.py                                    # dated snapshot first
  2) set CREDENTIAL_ENCRYPTION_KEYS="<new-key>,<previous-key-or-nothing>" in .env
     - migrating off SECRET_KEY: the legacy SECRET_KEY is an automatic decrypt-only fallback, so just set the
       new dedicated key as CREDENTIAL_ENCRYPTION_KEYS and run --rewrap.
     - rotating a dedicated key: put the NEW key first and the OLD key second, run --rewrap, then drop the OLD.
  3) --rewrap (dry) to preview, then --rewrap --apply. Each token is decrypted with whatever key still opens it
     and re-encrypted under the CURRENT key, verified to round-trip BEFORE the row is written. A token that no
     key can open is left UNTOUCHED (never destroyed). Transactional + idempotent.
  4) once every value is under the new key, remove the old key from CREDENTIAL_ENCRYPTION_KEYS.
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from sqlmodel import Session, select   # noqa: E402

from app.db import engine, init_db   # noqa: E402
from app.models import MailAccount   # noqa: E402
from app.outreach import _current_cipher, _decrypt_cipher, _encryption_key_ok   # noqa: E402

FIELDS = ("smtp_password_enc", "imap_password_enc")


def _guard():
    ok, why = _encryption_key_ok()
    if not ok:
        print(f"ABORT (fail-closed): {why}")
        sys.exit(2)


def _under_current(cur, value):
    """True if `value` already decrypts under the CURRENT key (so it needs no re-wrap)."""
    try:
        cur.decrypt(value.encode())
        return True
    except Exception:  # noqa: BLE001
        return False


def scan(apply=False):
    """Encrypt any PLAINTEXT credential under the current dedicated key (verify-before-persist, idempotent)."""
    _guard()
    init_db()
    cur, multi = _current_cipher(), _decrypt_cipher()
    counts = {"accounts": 0, "empty": 0, "encrypted": 0, "plaintext": 0, "migrated": 0}
    with Session(engine) as s:
        rows = s.exec(select(MailAccount)).all()
        counts["accounts"] = len(rows)
        for m in rows:
            for field in FIELDS:
                val = getattr(m, field, "") or ""
                if not val:
                    counts["empty"] += 1
                    continue
                try:
                    multi.decrypt(val.encode())          # decryptable by SOME configured key → already encrypted
                    counts["encrypted"] += 1
                    continue
                except Exception:  # noqa: BLE001
                    pass
                counts["plaintext"] += 1
                new_tok = cur.encrypt(val.encode()).decode()
                if cur.decrypt(new_tok.encode()).decode() != val:      # verify BEFORE replacing
                    raise RuntimeError(f"verify failed for account {m.id} {field}")
                if apply:
                    setattr(m, field, new_tok); s.add(m); counts["migrated"] += 1
        if apply:
            s.commit()
    mode = "APPLIED" if apply else "DRY-RUN"
    print(f"[{mode}] accounts={counts['accounts']} encrypted={counts['encrypted']} empty={counts['empty']} "
          f"plaintext={counts['plaintext']} "
          f"{'migrated=' + str(counts['migrated']) if apply else 'would-migrate=' + str(counts['plaintext'])}")
    print("(secrets are never displayed)")
    return counts


def rewrap(apply=False):
    """Re-encrypt every credential under the CURRENT key — covers the one-time SECRET_KEY→dedicated migration
    AND dedicated-key rotation. Decrypts with any key that still opens the token (dedicated fallbacks + legacy
    SECRET_KEY), verifies the new ciphertext round-trips, then replaces. Unreadable tokens are left untouched
    (never destroyed). Idempotent: tokens already under the current key are skipped."""
    _guard()
    init_db()
    cur, multi = _current_cipher(), _decrypt_cipher()
    counts = {"rewrapped": 0, "already_current": 0, "empty": 0, "unreadable": 0}
    with Session(engine) as s:
        for m in s.exec(select(MailAccount)).all():
            for field in FIELDS:
                val = getattr(m, field, "") or ""
                if not val:
                    counts["empty"] += 1
                    continue
                if _under_current(cur, val):
                    counts["already_current"] += 1
                    continue
                try:
                    plain = multi.decrypt(val.encode()).decode()       # any configured/legacy key
                except Exception:  # noqa: BLE001 — no key opens it → DO NOT destroy
                    counts["unreadable"] += 1
                    continue
                new_tok = cur.encrypt(plain.encode()).decode()
                if cur.decrypt(new_tok.encode()).decode() != plain:    # verify BEFORE replacing
                    raise RuntimeError(f"rewrap verify failed for account {m.id} {field}")
                if apply:
                    setattr(m, field, new_tok); s.add(m)
                counts["rewrapped"] += 1
        if apply:
            s.commit()
    mode = "APPLIED" if apply else "DRY-RUN"
    print(f"[{mode}] rewrap: rewrapped={counts['rewrapped']} already_current={counts['already_current']} "
          f"empty={counts['empty']} unreadable={counts['unreadable']}")
    print("(secrets and keys are never displayed)")
    return counts


if __name__ == "__main__":
    if "--rewrap" in sys.argv:
        rewrap(apply="--apply" in sys.argv)
    else:
        scan(apply="--dry-run" not in sys.argv)
