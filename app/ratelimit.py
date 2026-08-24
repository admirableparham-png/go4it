"""Phase 6 — a minimal in-process sliding-window rate limiter for the public buyer-portal routes.

Not a distributed limiter (single-process dev/prod-worker scope); enough to blunt token-guessing and
repeat-submit abuse on the auth-exempt `/q/` and `/p/` routes. Keyed by (client-ip, bucket).
"""
import time
from collections import defaultdict, deque

_BUCKETS = defaultdict(deque)


def allow(key: str, limit: int = 30, window: int = 60, now=None) -> bool:
    """True if this key is under `limit` hits in the last `window` seconds; records the hit when allowed."""
    now = now if now is not None else time.time()
    dq = _BUCKETS[key]
    cutoff = now - window
    while dq and dq[0] <= cutoff:
        dq.popleft()
    if len(dq) >= limit:
        return False
    dq.append(now)
    return True


def reset():
    _BUCKETS.clear()


def client_key(request, bucket: str) -> str:
    ip = getattr(getattr(request, "client", None), "host", "") or "?"
    return f"{bucket}:{ip}"
