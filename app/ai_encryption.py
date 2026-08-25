"""Phase 9 — dedicated encryption for AI conversation content at rest.

Uses a SEPARATE rotating key set (`AI_DATA_ENCRYPTION_KEYS`) — never SECRET_KEY, never the mailbox
CREDENTIAL_ENCRYPTION_KEYS. keys[0] encrypts new values; a MultiFernet over all keys (newest→oldest) decrypts,
so rotation is: prepend a new key, keep the previous keys as decrypt-only fallbacks, re-wrap lazily. On a public
deployment a dedicated key is REQUIRED (fail-closed); on localhost it falls back to SECRET_KEY for dev only.

Before any content is encrypted+stored, `redact_secrets()` strips anything that looks like a credential, private
key, wallet seed, password or bearer token — those must never be persisted or logged.
"""
import base64
import hashlib
import re

from .config import AI_DATA_ENCRYPTION_KEYS, SECRET_KEY

_DEFAULT_SECRET_KEYS = {"dev-insecure-change-me", "go4it", "change-me", "changeme", "secret", ""}


def _fernet_from_secret(secret_str):
    from cryptography.fernet import Fernet
    key = base64.urlsafe_b64encode(hashlib.sha256((secret_str or "").encode()).digest())
    return Fernet(key)


def _keys():
    """Dedicated AI keys, newest first (keys[0] encrypts). Localhost dev falls back to SECRET_KEY."""
    if AI_DATA_ENCRYPTION_KEYS:
        return list(AI_DATA_ENCRYPTION_KEYS)
    from .config import IS_LOCAL
    if IS_LOCAL and SECRET_KEY:
        return [SECRET_KEY]
    return []


def encryption_ok():
    """(ok, reason). Fail CLOSED on a public deployment unless a dedicated, non-default AI key is set."""
    from .config import IS_LOCAL
    keys = _keys()
    if not keys:
        return False, "no AI data encryption key configured — set AI_DATA_ENCRYPTION_KEYS"
    if not IS_LOCAL:
        if not AI_DATA_ENCRYPTION_KEYS:
            return False, "AI_DATA_ENCRYPTION_KEYS must be set on a public deployment (not the SECRET_KEY)"
        if AI_DATA_ENCRYPTION_KEYS[0] in _DEFAULT_SECRET_KEYS:
            return False, "AI_DATA_ENCRYPTION_KEYS[0] is weak/default — use a strong random key"
    return True, ""


def _current_cipher():
    ok, why = encryption_ok()
    if not ok:
        raise RuntimeError(f"AI data encryption unavailable: {why}")
    return _fernet_from_secret(_keys()[0])


def _decrypt_cipher():
    from cryptography.fernet import MultiFernet
    return MultiFernet([_fernet_from_secret(k) for k in _keys()])


def ai_encrypt(plaintext: str) -> str:
    """Encrypt under the dedicated current key. "" for empty input. Fail-closed if no key (never store plaintext)."""
    if not plaintext:
        return ""
    return _current_cipher().encrypt(plaintext.encode()).decode()


def ai_decrypt(token: str) -> str:
    """Decrypt, trying every dedicated key (rotation). "" for a corrupt/foreign token."""
    if not token:
        return ""
    ok, why = encryption_ok()
    if not ok:
        raise RuntimeError(f"AI data encryption unavailable: {why}")
    try:
        return _decrypt_cipher().decrypt(token.encode()).decode()
    except Exception:  # noqa: BLE001 — corrupt/foreign token, not a key-config problem
        return ""


# --------------------------------------------------------------------- secret redaction (before persistence)
# Patterns for things that must NEVER be stored/logged in a conversation, even encrypted. We drop them entirely.
_SECRET_PATTERNS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "[private key redacted]"),
    (re.compile(r"\b(?:[a-z]+_)?(?:api[_-]?key|secret|token|password|passwd|pwd|seed phrase|mnemonic)\b\s*[:=]\s*\S+", re.I), "[secret redacted]"),
    (re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9]{16,}\b"), "[key redacted]"),               # provider-style keys
    (re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{16,}\b", re.I), "[token redacted]"),
    (re.compile(r"\b(?:[a-z]+\s){0,2}(?:12|24)-word (?:wallet )?seed\b.*", re.I), "[seed redacted]"),
    (re.compile(r"\b[13][a-km-zA-HJ-NP-Z1-9]{25,34}\b"), "[wallet address redacted]"),   # btc-like
]


def redact_secrets(text: str) -> str:
    """Strip anything resembling a credential / private key / wallet seed / password / bearer token BEFORE the
    content is encrypted and stored. Returns the cleaned text."""
    t = text or ""
    for pat, repl in _SECRET_PATTERNS:
        t = pat.sub(repl, t)
    return t


def contains_secret(text: str) -> bool:
    return redact_secrets(text or "") != (text or "")


# --------------------------------------------------------------------- PII minimization (before external send)
# Anything sent to an EXTERNAL AI provider is minimized by default: contact PII (emails, phones, URLs) is
# redacted so a buyer's identifiers never leave the platform in a provider payload or in telemetry.
_PII_PATTERNS = [
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[email]"),
    (re.compile(r"\b(?:https?://|www\.)\S+", re.I), "[url]"),
    (re.compile(r"(?:\+|00)\d[\d\s().\-]{6,}\d|\b\d[\d\s().\-]{8,}\d\b"), "[phone]"),
]


def minimize_pii(text: str) -> str:
    """Redact contact PII (email/phone/url) for anything leaving the platform to an external provider or telemetry.
    Also runs secret redaction first."""
    t = redact_secrets(text or "")
    for pat, repl in _PII_PATTERNS:
        t = pat.sub(repl, t)
    return t


def contains_pii(text: str) -> bool:
    return minimize_pii(text or "") != redact_secrets(text or "")
