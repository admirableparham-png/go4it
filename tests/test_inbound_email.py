"""Tests for the inbound-email → conversation-thread path (match / dedupe / skip), no live mailbox."""
import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.inbound_email import handle_inbound, parse_email
from app.lead_service import find_lead_by_contact
from app.models import Lead, Outreach


@pytest.fixture
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _lead(s, **kw):
    base = dict(product="tiles", email="", phone="")
    base.update(kw)
    lead = Lead(**base)
    s.add(lead)
    s.commit()
    s.refresh(lead)
    return lead


def test_find_lead_by_email_is_case_insensitive(session):
    lead = _lead(session, email="Buyer@Acme.ge")
    assert find_lead_by_contact(session, email="buyer@acme.ge").id == lead.id
    assert find_lead_by_contact(session, email="nobody@x.com") is None


def test_find_lead_by_phone_tail(session):
    lead = _lead(session, phone="+971 55 984 3424")
    assert find_lead_by_contact(session, phone="0559843424").id == lead.id   # last-9 digits match
    assert find_lead_by_contact(session, phone="12345") is None              # too short / no match


def test_handle_inbound_threads_and_stamps_reply(session):
    lead = _lead(session, email="buyer@acme.ge", buyer_company="ACME")
    assert handle_inbound(session, "buyer@acme.ge", "Re: tiles", "Yes, send samples", "<m1@acme.ge>") == "threaded"
    msgs = session.exec(select(Outreach).where(Outreach.lead_id == lead.id)).all()
    assert len(msgs) == 1 and msgs[0].direction == "in" and msgs[0].channel == "email"
    session.refresh(lead)
    assert lead.buyer_replied_at is not None


def test_handle_inbound_dedupes_on_message_id(session):
    _lead(session, email="b@x.ge")
    handle_inbound(session, "b@x.ge", "s", "body", "<dup@x>")
    assert handle_inbound(session, "b@x.ge", "s", "body", "<dup@x>") == "duplicate"
    assert len(session.exec(select(Outreach)).all()) == 1


def test_handle_inbound_unmatched_is_skipped(session):
    assert handle_inbound(session, "stranger@nowhere.com", "hi", "body", "<x@y>") == "unmatched"
    assert session.exec(select(Outreach)).all() == []      # not threaded, not auto-created


def test_parse_email_extracts_fields():
    raw = (b"From: Buyer <buyer@acme.ge>\r\nSubject: Re: tiles\r\n"
           b"Message-ID: <abc@acme.ge>\r\nContent-Type: text/plain\r\n\r\nHello there\r\n")
    frm, subj, body, mid = parse_email(raw)
    assert frm == "buyer@acme.ge" and subj == "Re: tiles"
    assert "Hello there" in body and mid == "<abc@acme.ge>"
