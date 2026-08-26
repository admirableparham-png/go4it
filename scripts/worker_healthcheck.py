#!/usr/bin/env python
"""Docker HEALTHCHECK for the go4it WORKER container.

The worker serves no HTTP, so the app image's `curl /api/health` check never applied to it (it always read
"unhealthy", which could make orchestration restart a perfectly healthy worker). Instead the worker writes a
heartbeat timestamp each loop pass (app.worker._heartbeat); this check passes only if that heartbeat is fresh —
i.e. the background LOOP is actually running — and fails otherwise. Exit 0 = healthy, 1 = unhealthy.
"""
import os
import sys
import time

sys.path.insert(0, "/app")


def main() -> int:
    try:
        from app import ai_provider as p
        from app.config import INGEST_INTERVAL
    except Exception as e:  # noqa: BLE001
        print(f"healthcheck import error: {e}")
        return 1
    hb = os.path.join(p._control_dir(), "worker_heartbeat")
    if not os.path.exists(hb):
        print("worker heartbeat missing")
        return 1
    age = time.time() - os.path.getmtime(hb)
    # Allow ~3 loop intervals + a floor, so a slow pass never false-fails; a truly hung loop still trips it.
    limit = max(INGEST_INTERVAL * 3, 300)
    if age > limit:
        print(f"worker heartbeat stale: {int(age)}s > {int(limit)}s")
        return 1
    print(f"ok: worker heartbeat {int(age)}s old")
    return 0


if __name__ == "__main__":
    sys.exit(main())
