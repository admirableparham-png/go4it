"""Authentication helpers: password hashing (stdlib PBKDF2) and role checks.

PBKDF2-HMAC-SHA256 from the standard library — no native dependency, robust for
a small trusted team. Swappable for argon2 later without touching callers.
"""
import hashlib
import hmac
import os
from base64 import b64decode, b64encode

from .models import User

_ITERATIONS = 200_000
_ROLE_ORDER = {"viewer": 0, "agent": 1, "manager": 2, "admin": 3}


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
    return f"pbkdf2_sha256${_ITERATIONS}${b64encode(salt).decode()}${b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _algo, iterations, salt_b64, dk_b64 = stored.split("$")
        salt = b64decode(salt_b64)
        expected = b64decode(dk_b64)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(iterations))
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


def role_at_least(user, minimum: str) -> bool:
    """True if `user` has at least `minimum` role (viewer<agent<manager<admin)."""
    if user is None:
        return False
    return _ROLE_ORDER.get(user.role, -1) >= _ROLE_ORDER.get(minimum, 99)


def current_user(request, session):
    """Load the logged-in, active User from the signed session cookie, or None."""
    uid = request.session.get("user_id")
    if not uid:
        return None
    user = session.get(User, uid)
    return user if (user and user.active) else None
