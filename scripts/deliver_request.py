"""Deliver a trader's buyer-search request: load the researched buyers into the REQUESTER's own account
(owned by them, tagged req-<id>), close the request as done, and notify them.

    ./.venv/bin/python scripts/deliver_request.py <request_id> <buyers.json>

<buyers.json> is the same shape scripts/load_line.py consumes: {"buyers":[{company, city, dest_iso,
email, phones|phone, website, source_url, match_score, buys|wants, ...}]}. Run AFTER you've done the
research (e.g. the Claude buyer-hunt Workflow) for an approved/running request. Because the leads are
stamped with owner_id = the requester, Phase-0 scoping means ONLY that trader (and admin) ever see them.
"""
import json
import os
import re
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlmodel import Session  # noqa: E402

from app.db import engine  # noqa: E402
from app.lead_service import create_lead  # noqa: E402
from app.models import Lead, ServiceRequest, User  # noqa: E402
from app.telegram import notify_request_update  # noqa: E402


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:60]


def run(req_id, buyers_path):
    if not os.path.exists(buyers_path):
        print(f"{buyers_path} not found")
        return
    data = json.load(open(buyers_path, encoding="utf-8"))
    buyers = data.get("buyers", data if isinstance(data, list) else [])
    with Session(engine) as s:
        sr = s.get(ServiceRequest, int(req_id))
        if not sr:
            print(f"request #{req_id} not found")
            return
        if sr.status not in ("approved", "running"):
            print(f"refusing: request {sr.tracking_code} is '{sr.status}', not approved/running")
            return
        if sr.request_type != "buyer_hunt":
            print(f"refusing: {sr.tracking_code} is a '{sr.request_type}' request — deliver non-buyer "
                  f"services from the admin Requests page, not this script")
            return
        tag = sr.result_source_tag or f"req-{sr.id}"
        new = dup = 0
        for b in buyers:
            company = (b.get("company") or "").strip()
            if not company:
                continue
            phones = b.get("phones") or ([b["phone"]] if b.get("phone") else [])
            buys = ", ".join(b.get("buys", []) or ([b["wants"]] if b.get("wants") else []))
            lead = Lead(
                source=tag, external_id=f"{tag}:{slug(company)}",
                product=sr.product or "Buyer", category=f"req-{sr.request_type}",
                owner_id=sr.owner_id,                         # attaches the buyer to the REQUESTER
                spec=buys[:300],
                dest_country=(b.get("dest_iso") or "").strip().upper(),
                dest_city=b.get("city") or "",
                buyer_company=company, phone=(phones[0] if phones else ""),
                email=b.get("email") or "", website=b.get("website") or "",
                source_url=b.get("source_url") or "",
                notes=f"delivered for {sr.tracking_code} | match {b.get('match_score', '')}"[:600],
            )
            if create_lead(s, lead, run=False) is not None:  # run=False: no auto-quote/alert spam
                new += 1
            else:
                dup += 1
        sr.leads_delivered = (sr.leads_delivered or 0) + new       # additive: re-runs never zero the count
        sr.result = f"Delivered {sr.leads_delivered} buyers for {sr.product}" + (f" in {sr.market}" if sr.market else "")
        sr.status = "done"
        sr.done_at = datetime.utcnow()
        s.add(sr)
        s.commit()
        s.refresh(sr)
        try:
            notify_request_update(sr, s.get(User, sr.requester_id))
        except Exception:  # noqa: BLE001
            pass
        code, owner = sr.tracking_code, sr.owner_id
    print(f"[{code}] delivered {new} buyers ({dup} already present) to owner #{owner} as source={tag}. "
          f"Request marked done.")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: deliver_request.py <request_id> <buyers.json>")
        sys.exit(1)
    run(sys.argv[1], sys.argv[2])
