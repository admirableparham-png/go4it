"""Phase 9 (A) — structured search (no arbitrary SQL, allowlisted, safe projection) + the tool registry
(read-only tools run; unregistered/write tools are refused; metrics cite the registry; negatives are not demand)."""
from datetime import datetime

from sqlmodel import Session, select

from app import ai_search as SEARCH
from app import ai_tools as TOOLS
from app.models import Lead, MailAccount, User


def _admin(s):
    return s.exec(select(User).where(User.email == "admin@t.local")).one()


def test_search_is_allowlisted_and_safe(ops_engine):
    with Session(ops_engine) as s:
        s.add(Lead(product="Zinc", category="chem", dest_country="GE", tracking_code="L1", owner_id=1,
                   status="new")); s.commit()
        r = SEARCH.search(s, "leads", query="zinc", user=_admin(s))
        assert r["total"] == 1
        # unregistered entity (e.g. a credential-bearing table) is rejected — never searchable
        assert "users" not in SEARCH.entities() and "mailaccount" not in SEARCH.entities()
        for bad in ("users", "mailaccount", "quoteaccesstoken", "portalsession"):
            try:
                SEARCH.search(s, bad)
                assert False, f"{bad} should not be searchable"
            except SEARCH.SearchError:
                pass
        # non-allowlisted filter/sort fields are refused (no arbitrary querying)
        for kw in ({"filters": {"owner_id": 1}}, {"sort": "password_hash"}):
            try:
                SEARCH.search(s, "leads", **kw)
                assert False
            except SEARCH.SearchError:
                pass


def test_projection_never_leaks_secret_columns(ops_engine):
    # even a registered entity only returns curated fields — shipments never expose booking/tracking refs
    from app.models import Shipment
    with Session(ops_engine) as s:
        s.add(Shipment(reference="SH-1", mode="sea", booking_reference="SECRET-BK",
                       container_reference="MSKU-999", carrier_name_cache="Maersk internal",
                       current_milestone="in_transit", status="active")); s.commit()
        row = SEARCH.search(s, "shipments", user=_admin(s))["rows"][0]
        blob = str(row).lower()
        assert "secret-bk" not in blob and "msku-999" not in blob and "maersk" not in blob


def test_tool_registry_read_only_and_refuses_unknown(ops_engine):
    with Session(ops_engine) as s:
        # read-only tools run without approval
        out = TOOLS.run_tool(s, "get_metric", {"key": "positive_replies"}, _admin(s))
        assert out["ok"] and "value" in out["result"] and out["citations"]
        # an unregistered tool (arbitrary SQL/shell) is denied
        for bad in ("exec_sql", "run_shell", "read_env", "fetch_url"):
            r = TOOLS.run_tool(s, bad, {}, _admin(s))
            assert r["ok"] is False and "unknown tool" in r["error"]
        # there is NO tool for shell/sql/url/file/env/payment/credential
        for forbidden in ("exec_sql", "shell", "read_file", "http_get", "read_env", "send_payment"):
            assert forbidden not in TOOLS.tool_names()


def test_metric_tool_states_definition_and_no_cross_currency(ops_engine):
    from app.models import Settlement
    with Session(ops_engine) as s:
        s.add(Settlement(deal_id=1, revenue="1000", currency="USD", settlement_date=datetime.utcnow()))
        s.add(Settlement(deal_id=2, revenue="500", currency="EUR", settlement_date=datetime.utcnow()))
        s.commit()
        out = TOOLS.run_tool(s, "get_metric", {"key": "settled_value"}, _admin(s))
        v = out["result"]["value"]
        assert v.get("USD") == "1000.00" and v.get("EUR") == "500.00" and "1500" not in str(v)
        assert out["citations"][0]["meta"]["metric_version"]        # cites the registry version


def test_negative_reply_not_positive_demand(ops_engine):
    with Session(ops_engine) as s:
        s.add(Lead(product="x", tracking_code="N", owner_id=1, reply_outcome="negative",
                   buyer_replied_at=datetime.utcnow()))
        s.add(Lead(product="y", tracking_code="P", owner_id=1, reply_outcome="positive",
                   buyer_replied_at=datetime.utcnow())); s.commit()
        out = TOOLS.run_tool(s, "get_metric", {"key": "positive_replies"}, _admin(s))
        assert out["result"]["value"] == 1                          # the negative reply is excluded
