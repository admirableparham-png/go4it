"""Phase 6 — e-signature provider abstraction (honest 'Not configured').

No real e-signature integration exists in this codebase. Until one does (with credentials + API docs), the
provider reports 'not_configured' and signing is tracked MANUALLY: an admin records a SignatureEvent and/or
uploads a signed copy (quarantined). A typed name is manual tracking only — NEVER a legally verified signature
(SignatureEvent.verified stays False for manual). The provider interface is here so a real integration can be
dropped in later; it is tested with mocks, never live calls.
"""
import os

ESIGN_PROVIDER = os.getenv("ESIGN_PROVIDER", "").strip()
ESIGN_API_KEY = os.getenv("ESIGN_API_KEY", "").strip()


class ManualProvider:
    name = "manual"

    def status(self):
        return "manual"

    def request_signature(self, contract, party):
        """Manual tracking: there is nothing to send. The admin records signing out-of-band + uploads the
        signed copy. Returns (ok, ref, error)."""
        return True, "", ""


class NotConfiguredProvider:
    name = "provider"

    def status(self):
        return "not_configured"

    def request_signature(self, contract, party):
        return False, "", "e-signature provider not configured"


def get_provider():
    if ESIGN_PROVIDER and ESIGN_API_KEY:
        return NotConfiguredProvider()   # a name is set but no real integration exists yet — honest refusal
    return ManualProvider()


def provider_status():
    return {"provider": (ESIGN_PROVIDER or "none"),
            "state": "not_configured" if not (ESIGN_PROVIDER and ESIGN_API_KEY) else "configured_but_unimplemented",
            "manual_tracking": True}


def record_manual_signature(session, contract, version, *, party_role, signer_name, signer_email, actor=None):
    """Record a MANUAL signature event. verified is always False — a typed name is not a verified signature."""
    from datetime import datetime
    from .models import SignatureEvent
    ev = SignatureEvent(contract_id=contract.id, contract_version_id=version.id, party_role=party_role,
                        method="manual", signer_name=signer_name, signer_email=signer_email,
                        verified=False, signed_at=datetime.utcnow())
    session.add(ev)
    return ev
