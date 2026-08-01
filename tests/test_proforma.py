"""The public buyer pro-forma must NEVER leak internal cost data (breakdown, EXW, or margin).

Renders the template directly (no DB/network) so a future edit can't silently expose costs.
"""
import json
import re
from datetime import datetime

from app.main import templates
from app.models import Lead, Product, Quote


def _render():
    q = Quote(
        lead_id=1, product_id=1, tracking_code="G4-TEST-Q1", quantity=100, incoterm="DAP",
        dest_border="Sadakhlo (GE)", quote_currency="USD",
        exw_unit=100.0, exw_total=12345.0, delivered_unit=150.0, delivered_total=15000.0,
        margin_pct=25.0, validity_days=14, status="sent", share_token="tok",
        breakdown=json.dumps([{"label": "Margin", "basis": "25%", "per_unit": 30, "amount": 3000},
                              {"label": "EXW cost component", "basis": "factory gate", "per_unit": 100, "amount": 10000}]),
        created_at=datetime(2026, 1, 1),
    )
    product = Product(name="Steel rebar 12mm", unit="ton", spec="A3/B500B",
                      hs_code="7214", origin_region="Isfahan")
    lead = Lead(product="rebar", buyer_company="ACME LLC", contact_name="Bob",
                dest_city="Tbilisi", dest_country="GE")
    html = templates.env.get_template("proforma_public.html").render(
        q=q, lead=lead, product=product, fx={}, expires=None, today=datetime(2026, 1, 2))
    # ignore CSS (the stylesheet legitimately contains the word 'margin')
    return re.sub(r"<style.*?</style>", "", html, flags=re.S)


def test_public_proforma_shows_delivered_price():
    v = _render()
    assert "150.00" in v and "15000.00" in v          # delivered unit + total
    assert "Steel rebar 12mm" in v and "DAP" in v
    assert "ACME LLC" in v                             # buyer name


def test_public_proforma_never_leaks_costs():
    v = _render().lower()
    assert "exw" not in v                              # no factory-gate wording
    assert "margin" not in v                           # no margin wording
    assert "cost component" not in v                   # no internal breakdown label
    assert "factory gate" not in v
    assert "100.00" not in v                           # exw_unit value must not appear
    assert "12345.00" not in v                         # exw_total value must not appear
    assert "3000" not in v                             # margin amount must not appear
