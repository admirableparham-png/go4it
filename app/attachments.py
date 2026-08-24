"""Outreach attachments — HARD-DISABLED in Phase 4 for security.

Secure private storage + malware scanning are NOT implemented, so attachments are unavailable and cannot be
turned on by configuration. `OUTREACH_ATTACHMENTS_ENABLED=1` does NOT activate the incomplete path: because
the secure storage backend is not built (`_SECURE_STORAGE_IMPLEMENTED = False`), `attachments_enabled()` is
always False and every accept/store/preview/download/send path is refused at one choke point. If the flag is
set we continue with attachments forcibly disabled and log a prominent security warning (uptime over a hard
startup crash). Even if a validator returns OK, that never lets an attachment proceed — the send/store paths
gate on `attachments_enabled()`, not on validation.

When secure handling is built (private storage OUTSIDE public/static, authenticated admin-only downloads,
tenant + record-ownership checks, safe generated filenames, size limit, extension/MIME allow-list with
mismatch rejection, path-traversal protection, quarantine + AV hook, audit logging, no inline auto-open, no
seller-facing attachment except via the seller-safe deliverable process), set `_SECURE_STORAGE_IMPLEMENTED`
True in code (not via env) and finish `_secure_validate`.
"""
import logging
import os

logger = logging.getLogger("go4it")

# The ONLY switch that can enable attachments — a CODE constant, never an env var. It stays False until the
# secure storage + AV backend actually exists, so no configuration can activate the incomplete path.
_SECURE_STORAGE_IMPLEMENTED = False

# Whether an operator REQUESTED attachments via env (does NOT enable them; only drives the warning below).
_FLAG_REQUESTED = os.getenv("OUTREACH_ATTACHMENTS_ENABLED", "").lower() in ("1", "true", "yes", "on")

DISABLED_MESSAGE = "Attachments are temporarily disabled for security."

# The allow-list the eventual secure path will enforce (documented now; inactive while disabled).
ALLOWED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".csv", ".xlsx", ".docx", ".txt"}
ALLOWED_MIME = {"application/pdf", "image/png", "image/jpeg", "text/csv", "text/plain",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
DANGEROUS_EXTENSIONS = {".exe", ".bat", ".cmd", ".sh", ".js", ".jar", ".scr", ".com", ".msi", ".ps1",
                        ".vbs", ".dll", ".php", ".html", ".htm", ".svg"}
MAX_BYTES = 10 * 1024 * 1024


def attachments_enabled() -> bool:
    """The single source of truth. False unless the secure backend is BUILT (a code constant) AND requested.
    No env var alone can make this True."""
    return _SECURE_STORAGE_IMPLEMENTED and _FLAG_REQUESTED


def config_warning():
    """A prominent warning string if attachments were requested but cannot be safely enabled — else ''."""
    if _FLAG_REQUESTED and not _SECURE_STORAGE_IMPLEMENTED:
        return ("SECURITY: OUTREACH_ATTACHMENTS_ENABLED is set but secure attachment storage/AV is NOT "
                "implemented — attachments remain FORCIBLY DISABLED and no file will be accepted, stored, "
                "previewed, downloaded or sent.")
    return ""


def _secure_validate(filename="", content_type="", size=0):
    """PURE future-path validation (path-traversal, dangerous format, extension/MIME mismatch, oversize).
    Present for future work and unit-tested — but a PASS here still never lets an attachment through while
    `attachments_enabled()` is False, because the send/store paths gate on that, not on this result."""
    name = (filename or "").strip()
    if not name or ".." in name or "/" in name or "\\" in name or name.startswith("."):
        return False, "unsafe filename"
    ext = os.path.splitext(name)[1].lower()
    if ext in DANGEROUS_EXTENSIONS:
        return False, "dangerous file type rejected"
    if ext not in ALLOWED_EXTENSIONS:
        return False, "extension not allowed"
    if content_type and content_type.lower() not in ALLOWED_MIME:
        return False, "MIME type not allowed"
    if content_type and content_type.lower() == "application/pdf" and ext != ".pdf":
        return False, "extension/MIME mismatch"
    if size and int(size) > MAX_BYTES:
        return False, "file too large"
    return True, ""


def validate_attachment(filename="", content_type="", size=0):
    """(ok, reason). Refuses unless attachments are actually enabled (secure backend built). While disabled it
    ALWAYS refuses regardless of how clean the file looks."""
    if not attachments_enabled():
        return False, DISABLED_MESSAGE
    return _secure_validate(filename, content_type, size)


def reject_if_present(files) -> None:
    """Raise 400 with the disabled message if any attachment reaches a send/store path while disabled. Gates
    on the hard `attachments_enabled()`, so a set env flag cannot bypass it."""
    if files and not attachments_enabled():
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=DISABLED_MESSAGE)


if config_warning():
    logger.warning(config_warning())
