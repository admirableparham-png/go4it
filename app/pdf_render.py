"""Phase 6 — shared hardened HTML→PDF rendering + document verification.

Reuses the Phase-5 sandboxed generator (catalog_studio.BuiltinProvider — JavaScript disabled, offline, all
network egress blocked, file:// blocked, bounded timeout). Adds an unresolved-template-variable guard and a
sha256 helper used by the quote/contract PDF paths, plus verify_pdf() — the narrow verification seam for the
(deferred) generated-PDF email-attachment path. Never weakens the global attachment lock.
"""
import hashlib
import html as _html
import re


def escape(x) -> str:
    return _html.escape(str(x if x is not None else ""))


def has_unresolved_vars(text: str) -> bool:
    """True if any template placeholder survived rendering (`{{ ... }}` or `{% ... %}`) — a rendered document
    must have none before it can be approved/sent."""
    return bool(re.search(r"\{\{.*?\}\}|\{%.*?%\}", text or ""))


def render_pdf(html_str, out_path, timeout_ms=20000):
    """Render HTML→PDF in the hardened sandbox. Returns (ok, error). Refuses if the HTML still has unresolved
    template variables. Never raises."""
    if has_unresolved_vars(html_str):
        return False, "unresolved template variables remain"
    from .catalog_studio import BuiltinProvider
    ok, err, _ = BuiltinProvider().generate(html_str, out_path, timeout_ms=timeout_ms)
    return ok, err


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# The controlled generated-document verification seam (attachment path is DEFERRED — this is built, not wired).
MAX_PDF_BYTES = 8 * 1024 * 1024


def verify_pdf(path, expected_sha256) -> tuple:
    """(ok, reason). A generated commercial PDF may only be handled if: it exists, the sha256 matches the
    stored hash, it starts with the %PDF- signature, and it is within the size cap. This is the narrow,
    system-generated-only path; it never re-enables general user-supplied attachments."""
    import os
    if not os.path.exists(path):
        return False, "file missing"
    size = os.path.getsize(path)
    if size <= 0 or size > MAX_PDF_BYTES:
        return False, "size out of bounds"
    with open(path, "rb") as f:
        sig = f.read(5)
    if sig != b"%PDF-":
        return False, "not a PDF (bad signature)"
    if not expected_sha256 or sha256_file(path) != expected_sha256:
        return False, "sha256 mismatch"
    return True, ""
