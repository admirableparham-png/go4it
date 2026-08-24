"""Phase 7 — remittance: FX snapshot (never live when manual), encrypted account refs, "not configured"
honesty, internal compliance reasons, and a seller view that leaks nothing sensitive."""
import json

from sqlmodel import Session, select

from app import ops_providers as OPSPROV
from app import remittance as REMIT
from app.models import FxRate, RemittanceCase, User, WorkItem


def _admin(s):
    return s.exec(select(User).where(User.email == "admin@t.local")).one()


def test_fx_snapshot_captured_and_not_live_when_manual(ops_engine):
    with Session(ops_engine) as s:
        admin = _admin(s)
        # a manual USD/IRR rate
        s.add(FxRate(base="IRR", quote="USD", rate=0.0000238, kind="manual", source="admin")); s.commit()
        rc = REMIT.create_remittance(s, source_currency="USD", dest_currency="IRR", source_amount="1000",
                                     route_method_category="exchange_house", actor=admin); s.commit()
        assert rc.reference.startswith("RM-") and rc.fx_snapshot
        snap = json.loads(rc.fx_snapshot)
        assert snap.get("kind") == "manual"
        assert REMIT.fx_is_live(rc) is False                     # manual FX is never presented as live


def test_account_ref_is_encrypted(ops_engine):
    with Session(ops_engine) as s:
        admin = _admin(s)
        rc = REMIT.create_remittance(s, source_currency="USD", dest_currency="AED", source_amount="500",
                                     actor=admin); s.commit()
        REMIT.store_account_ref(s, rc, "IBAN AE99 0000 1234 5678", actor=admin); s.commit()
        assert rc.account_ref_enc and "IBAN" not in rc.account_ref_enc      # ciphertext, not plaintext
        assert REMIT.reveal_account_ref(rc) == "IBAN AE99 0000 1234 5678"   # admin can decrypt


def test_compliance_reason_internal_and_review_task(ops_engine):
    with Session(ops_engine) as s:
        admin = _admin(s)
        rc = REMIT.create_remittance(s, source_currency="USD", dest_currency="IRR", source_amount="1000",
                                     tenant_id=admin.id, actor=admin); s.commit()
        REMIT.set_status(s, rc, "compliance_review", compliance_reason="sanctions screening in progress",
                         actor=admin); s.commit()
        assert rc.compliance_reason == "sanctions screening in progress"    # stored internally
        view = REMIT.remittance_seller_view(rc)
        assert "sanctions" not in str(view).lower()              # never surfaced to a seller
        assert s.exec(select(WorkItem).where(WorkItem.type == "remittance_compliance_review")).first()


def test_provider_not_configured(ops_engine):
    st = OPSPROV.remittance_status()
    assert st["configured"] is False and st["message"] == "Provider integration not configured"


def test_seller_view_is_safe(ops_engine):
    with Session(ops_engine) as s:
        admin = _admin(s)
        rc = REMIT.create_remittance(s, source_currency="USD", dest_currency="AED", source_amount="500",
                                     route_method_category="bank", provider_company_id=7, actor=admin)
        s.commit()
        view = REMIT.remittance_seller_view(rc)
        assert set(view.keys()) == {"reference", "status", "amount", "currency", "next_action"}
        assert "7" not in str(view.get("provider", ""))          # no provider identity
