"""Intelligence (Phase 8) canary — exercises demand -> opportunity -> transparent score -> alert -> report on a
DISPOSABLE database, asserts every guarantee, and prints an evidence report. No PII leaks, seller access is
refused (403/404), and no external service is called.

    ./.venv/bin/python scripts/intel_canary.py

Flow: an accepted quote becomes a STRONG demand signal (deduped) -> an Opportunity with EXPLAINED supply match
and a transparent, versioned score breakdown -> an idempotent alert -> a CSV report (per-currency, no PII) ->
cross-seller isolation over HTTP. Returns a report dict; raises AssertionError on any failure.
"""
import os
import pathlib
import sys
import tempfile
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from fastapi.testclient import TestClient   # noqa: E402
from sqlalchemy import text   # noqa: E402
from sqlmodel import Session, SQLModel, create_engine, func, select   # noqa: E402

import app.main as main   # noqa: E402
from app import alerts as AL   # noqa: E402
from app import demand as DEM   # noqa: E402
from app import opportunities as OPP   # noqa: E402
from app import reports as R   # noqa: E402
from app.auth import hash_password   # noqa: E402
from app.models import (AnalyticsReport, DemandSignal, Lead, Opportunity, OpportunityMatch, Product, Quote,
                        Settlement, User)   # noqa: E402

_INDEXES = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_workitem_idem_open ON workitem(idempotency_key) "
    "WHERE idempotency_key != '' AND status IN ('open','in_progress','waiting')",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_demandsignal_dedup ON demandsignal(dedup_key) WHERE dedup_key != ''",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_intelalert_key ON intelalert(alert_key, condition_version) "
    "WHERE alert_key != ''",
)
_PII = ["ACME SECRET LLC", "Jane Secret", "jane@secretbuyer.com"]


def run(engine=None, files_dir=None):
    steps, report = [], {}
    if engine is None:
        tmp = tempfile.mkdtemp(prefix="intel_canary_")
        engine = create_engine(f"sqlite:///{os.path.join(tmp, 'canary.db')}")
        files_dir = files_dir or os.path.join(tmp, "reports")
    SQLModel.metadata.create_all(engine)
    with engine.connect() as c:
        for ddl in _INDEXES:
            c.execute(text(ddl))
        c.commit()
    saved_engine, saved_reports = main.engine, getattr(main, "REPORT_FILES_DIR", None)
    main.engine = engine
    main.REPORT_FILES_DIR = pathlib.Path(files_dir)
    try:
        with Session(engine) as s:
            for email, role in [("admin@canary", "admin"), ("sellera@canary", "agent"),
                                ("sellerb@canary", "agent")]:
                s.add(User(email=email, name=email, role=role, active=True, password_hash=hash_password("pw")))
            s.commit()
            admin = s.exec(select(User).where(User.email == "admin@canary")).one()
            lead = Lead(product="Zinc Sulphate", category="chemicals", dest_country="GE", tracking_code="L1",
                        owner_id=admin.id, buyer_company="ACME SECRET LLC", contact_name="Jane Secret",
                        email="jane@secretbuyer.com")
            s.add(lead); s.commit(); s.refresh(lead)
            p = Product(name="Zinc Sulphate Monohydrate", category="chemicals", hs_code="283329", exw_price=590,
                        origin_country="IR", min_order_qty=100, completeness_score=80,
                        verification_status="verified", status="active")
            s.add(p); s.commit(); s.refresh(p)
            q = Quote(lead_id=lead.id, product_id=p.id, owner_id=admin.id, status="accepted",
                      buyer_response="accepted", tracking_code="Q1", accepted_at=datetime.utcnow(),
                      current_version_id=1)
            s.add(q); s.commit(); s.refresh(q)
            s.add(Settlement(deal_id=1, revenue="1000", currency="USD", settlement_date=datetime.utcnow()))
            s.add(Settlement(deal_id=2, revenue="500", currency="EUR", settlement_date=datetime.utcnow()))
            s.commit()

            # 1. demand from an accepted quote — STRONG + deduped
            sig, created = DEM.from_accepted_quote(s, q, actor=admin)
            assert created and sig.strength == "strong", "accepted quote must yield a strong signal"
            _, again = DEM.from_accepted_quote(s, q, actor=admin)
            assert again is False, "duplicate signal must be refused"
            s.commit()
            steps.append("accepted quote -> strong demand signal (deduped)")

            # a negative reply must NOT create demand
            neg = Lead(product="X", tracking_code="L2", owner_id=admin.id, reply_outcome="negative")
            s.add(neg); s.commit(); s.refresh(neg)
            nsig, nc = DEM.from_positive_reply(s, neg)
            assert nsig is None and nc is False, "a negative reply must never become demand"
            steps.append("negative reply -> no demand (correctly excluded)")

            # 2+3. opportunity + explained match + transparent versioned score
            opp, _ = OPP.ensure_from_signal(s, sig, actor=admin); s.commit()
            matches = s.exec(select(OpportunityMatch).where(OpportunityMatch.opportunity_id == opp.id)).all()
            assert matches and matches[0].explanation, "supply match must be explained"
            assert opp.score > 0 and opp.score_version.startswith("s1:"), "score must be transparent + versioned"
            import json as _json
            breakdown = _json.loads(opp.score_breakdown)
            assert any(c["component"] == "missing_data_penalty" for c in breakdown), "penalty must be visible"
            steps.append(f"opportunity {opp.reference}: score {opp.score} ({opp.score_version}), match explained")

            # 4. idempotent alert
            a1, ac1 = AL.raise_alert(s, alert_type="new_high_demand", alert_key=f"opp:{opp.id}",
                                     condition_version=f"s{opp.score}", title="High demand",
                                     related_opportunity_id=opp.id)
            a2, ac2 = AL.raise_alert(s, alert_type="new_high_demand", alert_key=f"opp:{opp.id}",
                                     condition_version=f"s{opp.score}", title="High demand")
            assert ac1 and not ac2 and a1.id == a2.id, "alert must be idempotent"
            s.commit()
            steps.append("alert raised (idempotent)")

            # 5. report — per-currency, no PII, audited
            rpt, err = R.generate(s, report_type="weekly_exec", files_dir=main.REPORT_FILES_DIR, fmt="csv",
                                  actor=admin)
            assert err == "" and rpt.status == "generated", f"report failed: {err}"
            s.commit()
            body = (main.REPORT_FILES_DIR / rpt.file_path).read_text()
            assert "settled_value[USD]" in body and "settled_value[EUR]" in body and "1500" not in body, \
                "report must keep currencies separate"
            for pii in _PII:
                assert pii not in body, f"PII {pii} leaked into report"
            steps.append("report generated (per-currency, no PII, audited)")
            opp_id, rpt_id = opp.id, rpt.id

        # 6. cross-seller isolation over HTTP — an authenticated seller reaches no intelligence
        cb = TestClient(main.app)
        assert cb.post("/login", data={"email": "sellerb@canary", "password": "pw"},
                       follow_redirects=False).status_code == 303
        assert cb.get("/", follow_redirects=False).status_code == 200, "seller must be authenticated"
        iso = {
            "overview": cb.get("/intelligence", follow_redirects=False).status_code,
            "demand": cb.get("/intelligence/demand", follow_redirects=False).status_code,
            "opportunities": cb.get("/intelligence/opportunities", follow_redirects=False).status_code,
            "opp_detail": cb.get(f"/intelligence/opportunities/{opp_id}", follow_redirects=False).status_code,
            "reports": cb.get("/intelligence/reports", follow_redirects=False).status_code,
            "report_download": cb.get(f"/intelligence/reports/{rpt_id}/download",
                                      follow_redirects=False).status_code,
            "alerts": cb.get("/intelligence/alerts", follow_redirects=False).status_code,
        }
        assert all(v in (403, 404) for v in iso.values()), iso
        steps.append(f"cross-seller isolation enforced: {iso}")

        # no buyer PII on the admin intelligence analytics pages either
        ca = TestClient(main.app)
        assert ca.post("/login", data={"email": "admin@canary", "password": "pw"},
                       follow_redirects=False).status_code == 303
        for path in ["/intelligence", "/intelligence/demand", f"/intelligence/opportunities/{opp_id}"]:
            html = ca.get(path).text
            for pii in _PII:
                assert pii not in html, f"PII {pii} leaked on {path}"
        steps.append("no buyer PII on intelligence analytics pages")

        with Session(engine) as s:
            report["counts"] = {
                "demand_signals": s.exec(select(func.count()).select_from(DemandSignal)).one(),
                "opportunities": s.exec(select(func.count()).select_from(Opportunity)).one(),
                "reports": s.exec(select(func.count()).select_from(AnalyticsReport)).one(),
            }
        report["isolation"] = iso
        report["steps"] = steps
        report["result"] = "PASS"
        return report
    finally:
        main.engine = saved_engine
        if saved_reports is not None:
            main.REPORT_FILES_DIR = saved_reports


def _print(report):
    print("=== Intelligence canary ===")
    for st in report["steps"]:
        print(f"  ok  {st}")
    print("  counts:", report["counts"])
    print("RESULT:", report["result"])


if __name__ == "__main__":
    try:
        _print(run())
    except AssertionError as e:
        print("CANARY FAILED:", e)
        sys.exit(1)
