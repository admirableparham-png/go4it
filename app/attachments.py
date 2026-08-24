"""Outreach attachments — DISABLED in Phase 4 for security.

Phase 4 ships no private attachment store, no authenticated admin-only download, no AV scan/quarantine and no
seller-safe publishing gate — so rather than ship partial, unprotected handling, attachments are turned OFF at
one choke point. Every outreach compose/send path funnels through here and is refused. The UI shows the reason.

When secure handling lands, flip ATTACHMENTS_ENABLED and finish validate_attachment(): private storage OUTSIDE
public/static, authenticated admin-only downloads, tenant + record-ownership checks, safe generated filenames
(original kept only as metadata), size limits, an explicit extension/MIME allow-list with mismatch rejection,
path-traversal protection, a quarantine state + malware-scan hook, audit logging, no inline auto-open, and NO
seller-facing attachment unless explicitly published through the existing seller-safe deliverable process.
"""
import os

ATTACHMENTS_ENABLED = os.getenv("OUTREACH_ATTACHMENTS_ENABLED", "").lower() in ("1", "true", "yes", "on")
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
    return ATTACHMENTS_ENABLED


def validate_attachment(filename="", content_type="", size=0):
    """(ok, reason). While disabled this ALWAYS refuses — the single refusal point the UI + send paths share.
    The secure branch below is the future seam (path-traversal, dangerous-format, extension/MIME mismatch,
    oversize) and stays inactive until ATTACHMENTS_ENABLED is turned on."""
    if not ATTACHMENTS_ENABLED:
        return False, DISABLED_MESSAGE
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
    # extension/MIME agreement (mismatch rejection)
    if content_type and content_type.lower() == "application/pdf" and ext != ".pdf":
        return False, "extension/MIME mismatch"
    if size and int(size) > MAX_BYTES:
        return False, "file too large"
    return True, ""


def reject_if_present(files) -> None:
    """Raise 400 with the disabled message if any attachment reaches a send path while disabled."""
    if files and not ATTACHMENTS_ENABLED:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=DISABLED_MESSAGE)
