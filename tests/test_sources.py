"""Tests for the go4worldbusiness CSV lead source + raw->lead mapping."""
from app.ingest import raw_to_lead
from app.lead_service import content_hash
from app.sources.go4world_csv import parse_text

CSV = (
    "Inquiry ID,Company,Contact Person,Email,Country,Product,Quantity,Budget,Currency,Details\n"
    "GW-1,Tbilisi Steel,N. G,buyer@x.ge,Georgia,Steel rebar 12mm,250,690,USD,B500B\n"
    "GW-2,Batumi,M. K,,Turkey,Bitumen 60/70,400,500,USD,pen grade\n"
    ",NoId Co,,,Georgia,Portland cement,100,80,USD,\n"
)


def test_parse_maps_aliases_and_country_codes():
    leads = parse_text(CSV)
    assert len(leads) == 3
    a = leads[0]
    assert a.external_id == "GW-1"
    assert a.buyer_company == "Tbilisi Steel"
    assert a.product == "Steel rebar 12mm"
    assert a.quantity == 250.0 and a.target_price == 690.0
    assert a.dest_country == "GE"          # Georgia -> GE
    assert leads[1].dest_country == "TR"    # Turkey -> TR


def test_missing_id_gets_synthetic_stable_id():
    noid = parse_text(CSV)[2]
    assert noid.external_id.startswith("h:")   # so re-imports still dedup


def test_rows_without_product_are_skipped():
    leads = parse_text("Company,Product\nX,\nY,Rebar\n")
    assert len(leads) == 1 and leads[0].product == "Rebar"


def test_raw_to_lead_maps_fields():
    lead = raw_to_lead(parse_text(CSV)[0], "go4world")
    assert lead.source == "go4world"
    assert lead.external_id == "GW-1"
    assert lead.dest_country == "GE"
    assert lead.quantity == 250.0


def test_dedup_hash_is_stable_and_distinguishing():
    leads = parse_text(CSV)
    h1 = content_hash(raw_to_lead(leads[0], "go4world"))
    h1_again = content_hash(raw_to_lead(leads[0], "go4world"))
    h2 = content_hash(raw_to_lead(leads[1], "go4world"))
    assert h1 == h1_again          # same content -> same hash (idempotent re-import)
    assert h1 != h2                # different leads -> different hash
