"""Mailbox credential encryption — migration + key rotation (Phase 4 hardening).

Mailbox SMTP/IMAP credentials are stored ENCRYPTED at rest with Fernet (AES-128-CBC + HMAC-SHA256),
keyed off SECRET_KEY. New installs already store ciphertext (mail_encrypt on save), so a dry-run here
normally reports ZERO to migrate — this tool exists to (a) prove no plaintext is present, (b) safely
encrypt any legacy plaintext, and (c) rotate the key. Secrets are NEVER printed.

    ./.venv/bin/python scripts/encrypt_credentials.py --dry-run   # count plaintext creds; change nothing
    ./.venv/bin/python scripts/encrypt_credentials.py             # encrypt any plaintext (verify-before-persist)
    GO4IT_OLD_SECRET_KEY=<old> ./.venv/bin/python scripts/encrypt_credentials.py --rotate            # re-key (dry)
    GO4IT_OLD_SECRET_KEY=<old> ./.venv/bin/python scripts/encrypt_credentials.py --rotate --apply    # re-key

KEY ROTATION PROCEDURE (zero credential loss):
  1) backup_db.py                                  # dated snapshot first
  2) keep the CURRENT SECRET_KEY as GO4IT_OLD_SECRET_KEY, set the NEW one as SECRET_KEY in .env
  3) run --rotate (dry) to see how many tokens re-key, then --rotate --apply
  4) each token is decrypted with OLD and re-encrypted with NEW, verified to round-trip BEFORE the row is
     written; the old value is only replaced once the new one is proven decryptable. Transactional: any
     failure rolls the whole batch back — no half-rotated table.
  5) remove GO4IT_OLD_SECRET_KEY once verified.
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from sqlmodel import Session, select   # noqa: E402

from app.config import SECRET_KEY   # noqa: E402
from app.db import engine, init_db   # noqa: E402
from app.models import MailAccount   # noqa: E402
from app.outreach import _encryption_key_ok, _fernet, _fernet_from_secret   # noqa: E402

FIELDS = ("smtp_password_enc", "imap_password_enc")


def _state(cipher, value):
    """'empty' | 'encrypted' | 'plaintext' — never returns or logs the value itself."""
    if not value:
        return "empty"
    try:
        cipher.decrypt(value.encode())
        return "encrypted"
    except Exception:  # noqa: BLE001
        return "plaintext"


def scan(apply=False):
    ok, why = _encryption_key_ok()
    if not ok:
        print(f"ABORT (fail-closed): {why}")
        sys.exit(2)
    init_db()
    cipher = _fernet()
    counts = {"accounts": 0, "empty": 0, "encrypted": 0, "plaintext": 0, "migrated": 0}
    with Session(engine) as s:
        rows = s.exec(select(MailAccount)).all()
        counts["accounts"] = len(rows)
        for m in rows:
            for field in FIELDS:
                val = getattr(m, field, "") or ""
                st = _state(cipher, val)
                counts[st] += 1
                if st == "plaintext":
                    new_tok = cipher.encrypt(val.encode()).decode()
                    if cipher.decrypt(new_tok.encode()).decode() != val:   # verify BEFORE replacing
                        raise RuntimeError(f"verify failed for account {m.id} {field}")
                    if apply:
                        setattr(m, field, new_tok)
                        s.add(m)
                        counts["migrated"] += 1
        if apply:
            s.commit()
    mode = "APPLIED" if apply else "DRY-RUN"
    print(f"[{mode}] accounts={counts['accounts']} fields: encrypted={counts['encrypted']} "
          f"empty={counts['empty']} plaintext={counts['plaintext']} "
          f"{'migrated=' + str(counts['migrated']) if apply else 'would-migrate=' + str(counts['plaintext'])}")
    print("(secrets are never displayed)")
    return counts


def rotate(apply=False):
    old_key = os.getenv("GO4IT_OLD_SECRET_KEY", "")
    if not old_key:
        print("ABORT: set GO4IT_OLD_SECRET_KEY to the PREVIOUS key to rotate.")
        sys.exit(2)
    if old_key == SECRET_KEY:
        print("ABORT: GO4IT_OLD_SECRET_KEY equals the current SECRET_KEY — nothing to rotate.")
        sys.exit(2)
    ok, why = _encryption_key_ok()
    if not ok:
        print(f"ABORT (fail-closed): {why}")
        sys.exit(2)
    init_db()
    old_c, new_c = _fernet_from_secret(old_key), _fernet()
    counts = {"rotated": 0, "already_new": 0, "empty": 0, "unreadable": 0}
    with Session(engine) as s:
        for m in s.exec(select(MailAccount)).all():
            for field in FIELDS:
                val = getattr(m, field, "") or ""
                if not val:
                    counts["empty"] += 1
                    continue
                try:
                    plain = old_c.decrypt(val.encode()).decode()
                except Exception:  # noqa: BLE001 — not encrypted under the OLD key
                    counts["already_new" if _state(new_c, val) == "encrypted" else "unreadable"] += 1
                    continue
                new_tok = new_c.encrypt(plain.encode()).decode()
                if new_c.decrypt(new_tok.encode()).decode() != plain:   # verify BEFORE replacing
                    raise RuntimeError(f"rotation verify failed for account {m.id} {field}")
                if apply:
                    setattr(m, field, new_tok); s.add(m)
                counts["rotated"] += 1
        if apply:
            s.commit()
    mode = "APPLIED" if apply else "DRY-RUN"
    print(f"[{mode}] rotate: rotated={counts['rotated']} already_new={counts['already_new']} "
          f"empty={counts['empty']} unreadable={counts['unreadable']}")
    print("(secrets are never displayed)")
    return counts


if __name__ == "__main__":
    if "--rotate" in sys.argv:
        rotate(apply="--apply" in sys.argv)
    else:
        scan(apply="--dry-run" not in sys.argv)
