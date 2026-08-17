"""Send the KIMIEL zinc-sulphate first-touch to emailable zinc buyers. SAFE BY DEFAULT: a dry-run that
prints the batch + a sample email and sends NOTHING. Emails go out only with the explicit --send flag.

    ./.venv/bin/python scripts/send_zinc.py                       # dry-run: show the batch, send nothing
    ./.venv/bin/python scripts/send_zinc.py --markets AF,IQ,UZ    # dry-run, only those markets
    ./.venv/bin/python scripts/send_zinc.py --csv out.csv         # also write the list to CSV
    ./.venv/bin/python scripts/send_zinc.py --send                # ACTUALLY send to all emailable
    ./.venv/bin/python scripts/send_zinc.py --send --markets AF,IQ,UZ,KZ,TM,TJ,KG,AZ,AM,GE   # tier-A first

Each buyer gets zinc_message(lead) (destination auto-filled per lead — never hardcoded), the KIMIEL
signature at send, an Outreach row, and an armed follow-up (+FOLLOWUP_DAYS_1) so the sequence runs.
Sends are paced ~2s apart. One summary Telegram at the end (per-send pings stay muted).
"""
import argparse
import os
import sys
import time
from collections import Counter
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlmodel import Session, select  # noqa: E402

from app.config import FOLLOWUP_DAYS_1  # noqa: E402
from app.db import engine  # noqa: E402
from app.models import Lead, Outreach  # noqa: E402
from app.outreach import SMTP_ENABLED, build_parts, send_email, zinc_message  # noqa: E402
from app.telegram import send_message  # noqa: E402

SRC = "iran-export-zinc-sulfate"
DELAY = 2.0
RANK = {"AF": 0, "IQ": 1, "UZ": 2, "KZ": 3, "TM": 4, "TJ": 5, "KG": 6, "AZ": 7, "AM": 8, "GE": 9,
        "PK": 10, "TR": 11, "AE": 12, "SA": 13, "OM": 14, "QA": 15, "KW": 16, "BH": 17}


def pick(markets):
    with Session(engine) as s:
        leads = s.exec(select(Lead).where(Lead.source == SRC, Lead.email != "")).all()
    if markets:
        want = {m.strip().upper() for m in markets.split(",") if m.strip()}
        leads = [L for L in leads if (L.dest_country or "") in want]
    # dedup by email, keep highest priority market first then by lead id
    seen, out = set(), []
    for L in sorted(leads, key=lambda L: (RANK.get(L.dest_country or "", 99), L.id)):
        em = (L.email or "").strip().lower()
        if em and em not in seen:
            seen.add(em)
            out.append(L)
    return out


def write_csv(leads, path):
    import csv
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["#", "country", "city", "company", "email", "type", "match", "phone", "website"])
        for i, L in enumerate(leads, 1):
            role = ""
            if "role:" in (L.notes or ""):
                role = L.notes.split("role:", 1)[1].split("|")[0].strip()
            w.writerow([i, L.dest_country, L.dest_city, L.buyer_company, L.email, role,
                        (L.notes or "").split("match", 1)[-1].split("|")[0].strip(), L.phone, L.website])
    print(f"wrote CSV: {path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true", help="actually send (default: dry-run)")
    ap.add_argument("--markets", default="", help="comma ISO2 filter, e.g. AF,IQ,UZ")
    ap.add_argument("--limit", type=int, default=0, help="cap the batch size")
    ap.add_argument("--csv", default="", help="also write the batch to this CSV path")
    a = ap.parse_args()

    leads = pick(a.markets)
    if a.limit:
        leads = leads[:a.limit]
    bycc = Counter(L.dest_country or "?" for L in leads)
    print(f"batch: {len(leads)} emailable zinc buyers"
          + (f"  (markets={a.markets})" if a.markets else "")
          + f"\nby country: {dict(sorted(bycc.items(), key=lambda kv: RANK.get(kv[0],99)))}")
    if a.csv:
        write_csv(leads, a.csv)

    if not leads:
        return
    subj0, body0 = zinc_message(leads[0])
    print("\n--- SAMPLE EMAIL (first buyer) ---")
    print("TO:", leads[0].buyer_company, "<" + leads[0].email + ">", "|", leads[0].dest_country, leads[0].dest_city)
    print("SUBJECT:", subj0)
    print(body0[:700] + ("..." if len(body0) > 700 else ""))
    print("--- (signature appended at send) ---")

    if not a.send:
        print(f"\nDRY-RUN — nothing sent. Re-run with --send to email these {len(leads)} buyers.")
        return

    if not SMTP_ENABLED:
        print("\nABORT: SMTP is not configured (.env SMTP_*). No emails sent.")
        return

    print(f"\nSENDING to {len(leads)} buyers (~{DELAY}s apart)...")
    sent, failed = [], []
    with Session(engine) as s:
        for L in leads:
            lead = s.get(Lead, L.id)
            subj, body = zinc_message(lead)
            text, html = build_parts(body)
            ok, err, mid = send_email(lead.email, subj, text, html=html)
            s.add(Outreach(lead_id=lead.id, direction="out", channel="email", recipient=lead.email[:200],
                           subject=subj[:200], body=body[:4000], status="sent" if ok else "failed",
                           error=err, message_id=mid))
            if ok:
                lead.next_action_at = datetime.utcnow() + timedelta(days=FOLLOWUP_DAYS_1)
                lead.next_action_note = "followup-1"
                if lead.first_response_at is None:
                    lead.first_response_at = datetime.utcnow()
                s.add(lead)
                sent.append((lead.buyer_company, lead.dest_country or "?"))
            else:
                failed.append((lead.buyer_company, lead.email, (err or "")[:80]))
            s.commit()
            time.sleep(DELAY)

    bys = Counter(cc for _, cc in sent)
    print("=" * 56)
    print(f"SENT: {len(sent)} | FAILED: {len(failed)}")
    print("by country:", dict(sorted(bys.items(), key=lambda kv: RANK.get(kv[0], 99))))
    for c, em, r in failed:
        print(f"  FAIL {c[:28]:28} {em}  {r}")
    msg = (f"⚗️ <b>KIMIEL zinc first email sent to {len(sent)} buyers</b>\n"
           + " · ".join(f"{cc} {n}" for cc, n in bys.most_common())
           + (f"\n⚠️ {len(failed)} failed" if failed else "")
           + "\nFollow-ups armed (+3d, +5bd). Only replies will ping you \U0001F4B0\U0001F4B5.")
    send_message(msg)
    print("summary alert sent to Telegram")


if __name__ == "__main__":
    main()
