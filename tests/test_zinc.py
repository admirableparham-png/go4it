"""Tests for the ZINC SULPHATE outreach copy + the product-neutral follow-up branch."""
from app.models import Lead
from app.outreach import followup_message, honey_message, zinc_message


def _zinc_lead(**kw):
    return Lead(product="Zinc Sulphate Monohydrate", source="iran-export-zinc-sulfate", **kw)


def test_zinc_message_greeting_specs_and_no_invented_price():
    lead = _zinc_lead(buyer_company="Herat Agro Traders", dest_country="AF", dest_city="Herat")
    subject, body = zinc_message(lead)
    assert body.startswith("Dear Sir/Madam,")          # generic, no guessed name (founder pref)
    assert "Nick" not in body
    assert "33% Zn" in body                             # the actual grade/spec
    assert "ISO-17025" in body                          # lab-tested trust claim (CoAs)
    assert "25 kg" in body                              # packaging
    assert "Herat" in body                              # CPT place filled from the lead
    assert "USD" not in body and "$" not in body        # price-free cold email (Iran isn't cheapest)


def test_zinc_subject_names_the_market():
    _, _ = zinc_message(_zinc_lead(dest_country="UZ"))
    subject, _ = zinc_message(_zinc_lead(dest_country="UZ"))
    assert subject.startswith("KIMIEL - zinc sulphate")
    assert "(Uzbekistan)" in subject


def test_zinc_message_handles_missing_destination():
    subject, body = zinc_message(_zinc_lead(dest_country="", dest_city=""))
    assert "your market" in body and "your destination" in body   # graceful fallbacks
    assert subject == "KIMIEL - zinc sulphate monohydrate supply offer"


def test_followup_message_zinc_variant():
    lead = _zinc_lead(dest_country="AF")
    s1, b1 = followup_message(lead, 1, "KIMIEL - zinc sulphate monohydrate supply offer")
    assert s1 == "Re: KIMIEL - zinc sulphate monohydrate supply offer"
    assert "zinc sulphate" in b1.lower() and "following up" in b1.lower()
    _, b2 = followup_message(lead, 2, "")
    assert "final follow-up" in b2.lower() and "zinc sulphate" in b2.lower()


def test_followup_message_still_honey_by_default():
    honey = Lead(product="Honey", source="iran-export-honey-royaljelly", dest_country="IQ")
    _, b = followup_message(honey, 1, "")
    assert "honey" in b.lower() and "zinc" not in b.lower()   # non-zinc leads keep honey wording


def test_honey_message_unaffected():
    lead = Lead(product="Honey", source="iran-export-honey-royaljelly", dest_country="AE")
    subject, body = honey_message(lead)
    assert body.startswith("Dear Sir/Madam,") and "honey" in subject.lower()
