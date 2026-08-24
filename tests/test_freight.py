"""Phase 7 — freight requests + offers: missing-critical guard, Decimal totals, expiry, audited selection,
controlled replacement, and seller-safe projections that hide provider identity + all costs."""
from datetime import datetime, timedelta
from decimal import Decimal

from sqlmodel import Session, select

from app import freight as FR
from app.models import AuditLog, Company, FreightOffer, User, WorkItem


def _seller(s):
    return s.exec(select(User).where(User.email == "sellerA@t.local")).one()


def test_missing_critical_fields_raise_task(ops_engine):
    with Session(ops_engine) as s:
        owner = _seller(s)
        fr, missing = FR.create_freight_request(s, tenant_id=owner.id, cargo_description="steel",
                                                quantity="100", unit="tonne", mode="sea",
                                                origin_country="IR", dest_country="GE")  # weight/cbm/hazmat/customs absent
        s.commit()
        assert set(missing) == {"gross_weight_kg", "volume_cbm", "hazardous", "customs_required"}
        wi = s.exec(select(WorkItem).where(WorkItem.type == "freight_request_incomplete")).all()
        assert len(wi) == 1                                # unknowns are surfaced, never assumed


def test_complete_request_has_no_missing(ops_engine):
    with Session(ops_engine) as s:
        owner = _seller(s)
        fr, missing = FR.create_freight_request(
            s, tenant_id=owner.id, mode="sea", origin_country="IR", dest_country="GE",
            gross_weight_kg="100000", volume_cbm="40", hazardous=False, customs_required=True)
        s.commit()
        assert missing == []
        assert fr.reference.startswith("FR-")


def test_offer_total_is_decimal_recomputed(ops_engine):
    with Session(ops_engine) as s:
        owner = _seller(s)
        fr, _ = FR.create_freight_request(s, tenant_id=owner.id, mode="sea", origin_country="IR",
                                          dest_country="GE", gross_weight_kg="1", volume_cbm="1",
                                          hazardous=False, customs_required=False)
        s.commit()
        offer = FR.add_offer(s, fr, currency="USD", base_freight="1000.10", surcharges="200.20",
                             insurance_cost="50.05", customs_cost="0", provider_name_cache="ACME Freight")
        s.commit()
        assert offer.total == "1250.35"                    # exact Decimal sum, 2dp
        assert Decimal(offer.total) == Decimal("1250.35")


def test_expired_offer_is_never_selected(ops_engine):
    with Session(ops_engine) as s:
        owner = _seller(s)
        fr, _ = FR.create_freight_request(s, tenant_id=owner.id, mode="sea", origin_country="IR",
                                          dest_country="GE", gross_weight_kg="1", volume_cbm="1",
                                          hazardous=False, customs_required=False); s.commit()
        offer = FR.add_offer(s, fr, currency="USD", base_freight="100",
                             valid_until=datetime.utcnow() - timedelta(days=1)); s.commit()
        ok, err = FR.select_offer(s, offer, actor=None)
        assert ok is False and "expired" in err
        assert offer.selection_status == "offered"


def test_select_and_controlled_replacement_are_audited(ops_engine):
    with Session(ops_engine) as s:
        owner = _seller(s)
        prov = Company(tenant_id=owner.id, name="Blue Sea Lines", primary_role="freight_provider")
        s.add(prov); s.commit(); s.refresh(prov)
        fr, _ = FR.create_freight_request(s, tenant_id=owner.id, mode="sea", origin_country="IR",
                                          dest_country="GE", gross_weight_kg="1", volume_cbm="1",
                                          hazardous=False, customs_required=False); s.commit()
        o1 = FR.add_offer(s, fr, currency="USD", base_freight="100", provider_company_id=prov.id,
                          valid_until=datetime.utcnow() + timedelta(days=3)); s.commit()
        o2 = FR.add_offer(s, fr, currency="USD", base_freight="90", provider_company_id=prov.id,
                          valid_until=datetime.utcnow() + timedelta(days=3)); s.commit()
        ok, _ = FR.select_offer(s, o1, actor=None); s.commit()
        assert ok and o1.selection_status == "selected"
        # selecting a different offer replaces the first via a controlled replacement event
        ok2, _ = FR.select_offer(s, o2, actor=None, reason="cheaper"); s.commit()
        assert ok2 and o2.selection_status == "selected"
        s.refresh(o1)
        assert o1.selection_status == "replaced" and o1.replaced_by_id == o2.id
        actions = {a.action for a in s.exec(select(AuditLog)).all()}
        assert {"freight_offer_selected", "freight_offer_replaced"} <= actions
        s.refresh(fr)
        assert fr.status == "offer_selected"


def test_seller_view_hides_provider_and_cost(ops_engine):
    with Session(ops_engine) as s:
        owner = _seller(s)
        fr, _ = FR.create_freight_request(s, tenant_id=owner.id, mode="sea", origin_country="IR",
                                          dest_country="GE", gross_weight_kg="1", volume_cbm="1",
                                          hazardous=False, customs_required=False); s.commit()
        offer = FR.add_offer(s, fr, currency="USD", mode="sea", base_freight="9999",
                             route_summary="Bandar Abbas → Poti", lead_time_days=18,
                             provider_name_cache="SECRET Freight Co",
                             provider_reference="INTERNAL-REF-42"); s.commit()
        view = FR.offer_seller_view(offer)
        blob = str(view).lower()
        assert "9999" not in blob and "secret" not in blob and "internal-ref" not in blob
        assert view["mode"] == "sea" and view["timing"] == "~18 days" and view["route"] == "Bandar Abbas → Poti"
