"""Phase 10 — authenticated access-control canary. Isolated (disposable DB), no prod data touched, real app via
TestClient. Seeds one Founder, one Admin/Manager, one restricted internal (Analyst) and two sellers, then proves
the role matrix, seller confidentiality (incl. downloads), cross-tenant isolation, the audit trail, final-founder
protection and disable-revokes-access — and that operational counts are unchanged. PASS/FAIL per check.

    ./.venv/bin/python scripts/access_canary.py
"""
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(tempfile.mkdtemp(prefix="access_canary_"), "c.db")

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, func, select  # noqa: E402

import app.main as main  # noqa: E402
from app import authz, access_service as ACCESS, permissions as P  # noqa: E402
from app.auth import hash_password  # noqa: E402
from app.db import engine, init_db  # noqa: E402
from app.models import (AccessAuditLog, Company, Contact, Lead, ServiceRequest, User, UserProfile)  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" :: {detail}" if detail else ""))


def _mk(s, email, role, ac, rk):
    u = User(email=email, name=email.split("@")[0], role=role, active=True, password_hash=hash_password("pw"))
    s.add(u); s.commit(); s.refresh(u)
    s.add(UserProfile(user_id=u.id, account_class=ac, role_key=rk, scope=P.ROLE_TEMPLATES[rk]["scope"],
                      account_status="active")); s.commit()
    return u


def login(email):
    c = TestClient(main.app)
    r = c.post("/login", data={"email": email, "password": "pw"}, follow_redirects=False)
    assert r.headers.get("location") == "/", f"login failed for {email}"
    return c


init_db()
with Session(engine) as s:
    _mk(s, "founder@c", "admin", "internal", "founder")
    _mk(s, "manager@c", "admin", "internal", "admin_manager")
    _mk(s, "analyst@c", "admin", "internal", "analyst")
    sa = _mk(s, "sa@c", "agent", "seller", "seller")
    _mk(s, "sb@c", "agent", "seller", "seller")
    co = Company(name="ACME BUYER", country="IQ", primary_role="buyer"); s.add(co); s.commit(); s.refresh(co)
    s.add(Contact(company_id=co.id, name="Jane Secret", email="jane@acme.iq", phone="+964"))
    s.add(Lead(product="Zinc", owner_id=None, managed=True, seller_id=sa.id, buyer_company="ACME BUYER",
               contact_name="Jane Secret"))
    s.add(ServiceRequest(request_type="remittance", product="Zinc", market="Iraq", status="approved",
                         owner_id=sa.id, requester_id=sa.id)); s.commit()
    cid = co.id
    sa_req = s.exec(select(ServiceRequest)).first().id
    pre = {t: s.exec(select(func.count()).select_from(m)).one()
           for t, m in (("users", User), ("leads", Lead), ("requests", ServiceRequest))}

founder, manager, analyst, seller_a, seller_b = (login(x) for x in ("founder@c", "manager@c", "analyst@c",
                                                                     "sa@c", "sb@c"))

# Founder — full authority
check("founder manages users", founder.get("/admin/users", follow_redirects=False).status_code == 200)
check("founder views buyer PII", founder.get(f"/companies/{cid}", follow_redirects=False).status_code == 200)
check("founder exports buyer data", founder.get("/export/buyers.csv", follow_redirects=False).status_code == 200)
# Admin/Manager — manages users, but NOT founder-only controls
check("manager manages users", manager.get("/admin/users", follow_redirects=False).status_code == 200)
with Session(engine) as s:
    m, f = _u_m = s.exec(select(User).where(User.email == "manager@c")).one(), \
        s.exec(select(User).where(User.email == "founder@c")).one()
    check("manager CANNOT assign the Founder role", authz.can_assign_role(s, m, "founder") is False)
    check("manager has NO live Claude", authz.has_permission(s, m, "ai.live.use") is False)
# Restricted internal (Analyst)
check("analyst denied user mgmt", analyst.get("/admin/users", follow_redirects=False).status_code == 403)
check("analyst denied buyer PII", analyst.get(f"/companies/{cid}", follow_redirects=False).status_code == 403)
check("analyst denied export", analyst.get("/export/buyers.csv", follow_redirects=False).status_code == 403)
# Sellers — sanitized + isolated
sbody = seller_a.get("/leads").text
check("seller sees no buyer identity", "ACME BUYER" not in sbody and "Jane Secret" not in sbody)
check("seller denied Command/PII/admin", all(
    seller_a.get(p, follow_redirects=False).status_code in (403, 404)
    for p in ("/command", f"/companies/{cid}", "/admin/users", "/export/buyers.csv")))
check("cross-seller isolation (B cannot see A's request)",
      seller_b.get(f"/requests/{sa_req}/thread", follow_redirects=False).status_code == 404)

# Audit + safety + disable
with Session(engine) as s:
    fdr = s.exec(select(User).where(User.email == "founder@c")).one()
    an = s.exec(select(User).where(User.email == "analyst@c")).one()
    ACCESS.set_role(s, fdr, an, "researcher", reason="canary"); s.commit()
    check("access change audited", s.exec(select(AccessAuditLog).where(
        AccessAuditLog.action == "role_changed", AccessAuditLog.target_user_id == an.id)).first() is not None)
    ok_f, _ = ACCESS.set_status(s, fdr, fdr, "disabled")
    check("last Founder cannot be disabled", ok_f is False)
    ACCESS.set_status(s, fdr, s.exec(select(User).where(User.email == "manager@c")).one(), "disabled"); s.commit()
check("disabled manager is logged out", manager.get("/admin/users", follow_redirects=False).status_code in (302, 303))

# cleanup + count-invariance (operational rows never changed; access rows are canary-only + discarded with the DB)
with Session(engine) as s:
    post = {t: s.exec(select(func.count()).select_from(m)).one()
            for t, m in (("users", User), ("leads", Lead), ("requests", ServiceRequest))}
check("operational counts invariant", post == pre, f"{pre} == {post}")
try:
    import shutil
    shutil.rmtree(os.path.dirname(os.environ["DATABASE_URL"].replace("sqlite:///", "")), ignore_errors=True)
    print("  ARCHIVED: disposable DB removed; no production data touched")
except Exception as e:  # noqa: BLE001
    print("  cleanup note:", e)

print(f"\nACCESS-CONTROL CANARY: {sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
