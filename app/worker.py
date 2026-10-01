"""Background ingestion + enrichment worker.

    python -m app.worker            # loop: inbox often, portal hourly, web-enrich if ENRICH_INTERVAL>0
    python -m app.worker --once     # one pass (inbox + portal + enrich) — used by launchd/cron
    python -m app.worker --portal   # portal only (captures the login page to ./debug)
    python -m app.worker --enrich   # one web-enrich pass only (fill blank contacts from sites)
    python -m app.worker --inbound  # one inbound-email poll (thread buyer replies; needs IMAP_*)
    python -m app.worker --daily-summary [--send]   # print today's ops summary ([--send] = Telegram now; no marker)

Run it as ONE process (a separate worker, not inside gunicorn's web workers — that would double-fire
every job). Idempotent dedup means a crash-and-restart never double-imports; the enrich pass skips
leads it already attempted, so it works down NEW harvested leads instead of re-scraping dead sites.
"""
import logging
import os
import sys
import time

from sqlmodel import Session

from datetime import datetime, timedelta

from sqlmodel import select

from .config import (BASE_URL, ENRICH_BATCH, ENRICH_INTERVAL, FOLLOWUP_ENABLED, FOLLOWUP_INTERVAL,
                     GO4WORLD_ENABLED, GO4WORLD_INTERVAL, IMAP_ENABLED, IMAP_INTERVAL, INBOX_DIR,
                     INGEST_INTERVAL, REQUEST_REMINDER_HOURS, REQUEST_REMINDER_INTERVAL, SMTP_ENABLED)
from .db import engine, init_db
from .enrich_service import run_web_enrichment
from .followups import process_followups
from .inbound_email import poll_inbox
from .ingest import ingest_source

# How often the loop runs the idempotent Work Queue repair pass (0 disables the loop cadence; the app still
# creates work items live at each event, and cron can call `--sync-workitems`). Default 15 min.
WORKITEM_SYNC_INTERVAL = int(os.getenv("WORKITEM_SYNC_INTERVAL", "900"))
# Max NEW work items one sync pass may create, so a large backlog can't make a single pass run unbounded
# (the remainder is idempotently picked up on the next pass). Bounds "slow synchronization".
WORKITEM_SYNC_MAX_PER_RUN = int(os.getenv("WORKITEM_SYNC_MAX_PER_RUN", "200"))
# Campaign-send cycle (Phase 4): bounded by BOTH a count cap and a wall-clock deadline; cursor/batched.
CAMPAIGN_SEND_INTERVAL = int(os.getenv("CAMPAIGN_SEND_INTERVAL", "300"))
CAMPAIGN_SEND_MAX_PER_RUN = int(os.getenv("CAMPAIGN_SEND_MAX_PER_RUN", "200"))
CAMPAIGN_SEND_DEADLINE_SEC = int(os.getenv("CAMPAIGN_SEND_DEADLINE_SEC", "60"))
# Recipients looked at per campaign per cycle — independent of MAX_PER_RUN, so a small send cap (slow warm-up
# pacing, e.g. MAX_PER_RUN=1) can't be starved by recipients that are waiting on a retry backoff.
CAMPAIGN_SEND_SCAN_LIMIT = int(os.getenv("CAMPAIGN_SEND_SCAN_LIMIT", "1000"))
# Phase 11: optional automatic DB backup (the same WAL-safe online backup + integrity check as scripts/backup_db.py,
# keeping the newest BACKUP_KEEP=14). 0 = off (default); 86400 = daily. Needs ./backups mounted into the worker.
BACKUP_INTERVAL = int(os.getenv("BACKUP_INTERVAL", "0"))
# Phase 12: one daily ops summary to the admin Telegram chat at/after this UTC time ("HH:MM"); "" = off (default).
# Counts only — never a buyer's name or email. Set it on the server only (a second worker would send a second one).
DAILY_SUMMARY_AT = os.getenv("DAILY_SUMMARY_AT", "").strip()
_SUMMARY_MARKER = "daily_summary_last"      # in the shared control dir (prod: the DB volume) — survives restarts
from .models import ServiceRequest, User
from .telegram import send_message
from .sources.go4world_csv import Go4WorldCsvSource
from .sources.go4world_portal import Go4WorldPortalSource

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("go4it.worker")


def _heartbeat() -> None:
    """Write a liveness timestamp the container HEALTHCHECK reads (scripts/worker_healthcheck.py). Proves the
    background LOOP is alive — the worker serves no HTTP, so the app's curl health check never applied to it."""
    try:
        from . import ai_provider as _p
        with open(os.path.join(_p._control_dir(), "worker_heartbeat"), "w") as fh:
            fh.write(str(int(time.time())))
    except Exception:  # noqa: BLE001 — never let heartbeat IO crash a worker pass
        pass


def run_inbox() -> dict:
    return ingest_source(Go4WorldCsvSource(INBOX_DIR))


def run_portal() -> dict:
    return ingest_source(Go4WorldPortalSource())


def run_enrich() -> dict:
    """One bounded web-enrichment pass over not-yet-attempted website leads."""
    with Session(engine) as session:
        return run_web_enrichment(session, limit=ENRICH_BATCH, skip_attempted=True,
                                  log=logger.info)


def run_inbound_email() -> dict:
    """One inbound-email poll: thread buyer replies onto their leads (no-op unless IMAP configured)."""
    with Session(engine) as session:
        return poll_inbox(session, log=logger.info)


def run_followups() -> dict:
    """One follow-up sweep: threaded FU emails + 'call' nudges. No-op unless armed (FOLLOWUP_ENABLED) +
    SMTP configured. Only ever acts on leads whose first email the founder already sent."""
    if not (FOLLOWUP_ENABLED and SMTP_ENABLED):
        return {"skipped": "disabled"}
    with Session(engine) as session:
        return process_followups(session, log=logger.info)


_reminded_requests = set()   # in-memory dedup: ping the founder about each stale request only once


def run_request_reminders() -> dict:
    """Ping the founder about concierge requests still 'submitted' after REQUEST_REMINDER_HOURS — once
    each, so no trader's request is silently dropped. In-memory dedup (a restart may re-ping; harmless)."""
    if REQUEST_REMINDER_INTERVAL <= 0:
        return {"skipped": "disabled"}
    cutoff = datetime.utcnow() - timedelta(hours=REQUEST_REMINDER_HOURS)
    sent = 0
    with Session(engine) as session:
        stale = session.exec(select(ServiceRequest).where(
            ServiceRequest.status == "submitted", ServiceRequest.created_at <= cutoff)).all()
        for sr in stale:
            if sr.id in _reminded_requests:
                continue
            requester = session.get(User, sr.requester_id)
            who = (requester.name or requester.email) if requester else "a trader"
            try:
                send_message(f"⏰ <b>Pending request {sr.tracking_code}</b> from {who} — "
                             f"{sr.product or '-'} → {sr.market or '-'}\nApprove: {BASE_URL}/admin/requests")
            except Exception:  # noqa: BLE001
                pass
            _reminded_requests.add(sr.id)
            sent += 1
    return {"reminded": sent}


def run_work_item_sync():
    """Idempotent Work Queue repair pass, ISOLATED from the rest of the worker. It (a) never propagates an
    exception — any failure is caught and returned so research/ingestion/email cycles keep running, and
    (b) caps creations per run (WORKITEM_SYNC_MAX_PER_RUN) so a huge backlog can't make one pass run
    unbounded. Never creates a duplicate open task, and never recreates a dispositioned one (durable via
    condition_version). Mirrors scripts/sync_work_items.py so cron and the loop agree."""
    from . import work_queue as WQ
    try:
        with Session(engine) as s:
            summary = WQ.run_all_sync(s, None, limit=WORKITEM_SYNC_MAX_PER_RUN)
            s.commit()
            return summary
    except Exception as e:  # noqa: BLE001 — sync must never stop the other worker jobs
        logger.exception("work-item sync failed (isolated; other worker jobs continue)")
        return {"error": str(e), "total": 0}


def run_campaign_send(now=None):
    """Isolated campaign-send cycle (Phase 4). ISOLATION: one campaign or mailbox failure can never stop the
    others (per-item try/except + rollback) and the whole cycle never raises. BOUNDED: stops at both a count
    cap (CAMPAIGN_SEND_MAX_PER_RUN) and a wall-clock deadline (CAMPAIGN_SEND_DEADLINE_SEC). CURSOR/BATCH:
    processes due recipients ordered by id. CRASH-SAFE + IDEMPOTENT: each step is CLAIMED with a leased
    CampaignSend row before SMTP and the Outreach event is written only after the provider accepts, so a crash
    never leaves a phantom 'sent' and concurrent workers never send the same step twice. Abandoned claims are
    recovered at the top of the cycle. Refuses everything while Pause-all is on."""
    from sqlalchemy import func as _func, or_ as _or
    from . import campaign_service as CS
    from . import send_guard as SG
    from .models import Campaign, CampaignRecipient, MailAccount
    summary = {"campaigns": 0, "sent": 0, "skipped": 0, "failed": 0, "errors": 0, "capped": False}
    start = time.time()

    def _done():   # every SMTP attempt counts toward the per-cycle cap, so failures can't bypass the pacing
        return (summary["sent"] + summary["failed"] >= CAMPAIGN_SEND_MAX_PER_RUN
                or (time.time() - start) >= CAMPAIGN_SEND_DEADLINE_SEC)
    try:
        with Session(engine) as s:
            if SG.outreach_paused(s):
                summary["paused"] = True
                return summary
            # crash recovery FIRST: reclaim abandoned claims, flag ambiguous mid-send rows for review
            try:
                rec = CS.recover_stale_sends(s, now)
                summary["recovered"] = rec.get("reclaimed", 0)
                summary["needs_review"] = rec.get("needs_review", 0)
            except Exception:  # noqa: BLE001 — recovery must never stop the send cycle
                s.rollback()
            for c in s.exec(select(Campaign).where(Campaign.status == "running")).all():
                if _done():
                    summary["capped"] = True
                    break
                summary["campaigns"] += 1
                try:
                    mb = s.get(MailAccount, c.mailbox_id) if c.mailbox_id else None
                    ok, _why = SG.mailbox_ok(mb)
                    if not ok:
                        continue                       # unhealthy mailbox — skip this campaign, others go on
                    if CS.bounce_breaker(s, c):        # too many hard bounces → paused before sending more
                        continue
                    t = now or datetime.utcnow()
                    if SG.within_window(c, t):         # Phase 12: the day's warm-up decision, before its first send
                        try:
                            wu = CS.apply_warmup(s, c, mb, t)
                            if wu.get("action") in ("advance", "hold"):
                                logger.info("warm-up campaign %s: %s %s→%s/day (%s)", c.id, wu["action"],
                                            wu["from"], wu["to"], wu["why"])
                        except Exception:  # noqa: BLE001 — the ramp must never stop the sending
                            logger.exception("warm-up check failed (isolated; sending continues)")
                            s.rollback()
                    due = s.exec(select(CampaignRecipient).where(
                        CampaignRecipient.campaign_id == c.id,
                        CampaignRecipient.status.not_in(CS.TERMINAL_RECIPIENT),
                        _or(CampaignRecipient.next_action_at.is_(None),
                            CampaignRecipient.next_action_at <= t))
                        .order_by(CampaignRecipient.id)
                        .limit(max(CAMPAIGN_SEND_MAX_PER_RUN, CAMPAIGN_SEND_SCAN_LIMIT))).all()
                    fresh = None                       # reply reading, worked out once per campaign per cycle
                    for r in due:
                        if _done():
                            summary["capped"] = True
                            break
                        try:
                            if fresh is None and r.current_step > 0:
                                fresh = CS.reply_reading_fresh(s, t)
                            res = CS.send_step(s, c, r, mb, t, inbox_fresh=fresh)
                            st = res.get("status")
                            if st == "sent":
                                summary["sent"] += 1
                            elif st == "mailbox_failed":       # auth/quota/config: mailbox already paused
                                summary["failed"] += 1
                                break                          # stop this mailbox; other campaigns continue
                            elif st in ("retryable", "permanently_failed"):
                                summary["failed"] += 1
                            elif st == "limited" or (st == "render_failed" and res.get("scope") == "template") \
                                    or (st == "skipped" and CS.is_campaign_level_skip(res.get("reason"))):
                                summary["skipped"] += 1
                                break                          # holds for every recipient of this campaign
                            else:
                                summary["skipped"] += 1
                        except Exception:  # noqa: BLE001 — one recipient can't stop the batch
                            summary["errors"] += 1
                            s.rollback()
                    remaining = s.exec(select(_func.count()).where(
                        CampaignRecipient.campaign_id == c.id,
                        CampaignRecipient.status.not_in(CS.TERMINAL_RECIPIENT))).one()
                    if remaining == 0 and c.status == "running":
                        c.status, c.completed_at = "completed", now or datetime.utcnow()
                        s.add(c); s.commit()
                except Exception:  # noqa: BLE001 — one campaign can't stop the others
                    summary["errors"] += 1
                    s.rollback()
            return summary
    except Exception as e:  # noqa: BLE001 — the cycle itself never propagates
        logger.exception("campaign-send failed (isolated; other worker jobs continue)")
        return {"error": str(e), **summary}


def run_backup():
    """Isolated automatic backup. A failure (incl. a failed integrity check) is logged + alerted, never stops the
    worker."""
    try:
        from scripts import backup_db
        backup_db.run()
        return {"ok": True}
    except (Exception, SystemExit) as e:  # noqa: BLE001 — backup_db raises SystemExit on a bad snapshot
        logger.error("automatic backup FAILED: %s", e)
        try:
            send_message(f"⚠️ go4it automatic database backup FAILED: {str(e)[:200]}")
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "error": str(e)}


def _last_backup_time() -> float:
    """When the newest data-*.db snapshot was written (0 = none) — the loop starts its backup cadence from it, so a
    deploy or restart doesn't take an extra copy and eat into the BACKUP_KEEP retention."""
    try:
        import glob
        from scripts import backup_db
        return max((os.path.getmtime(p) for p in glob.glob(os.path.join(backup_db.OUT, "data-*.db"))), default=0.0)
    except Exception:  # noqa: BLE001 — unknown → back up on the first pass, as before
        return 0.0


# --- Phase 12: daily ops summary -------------------------------------------------------------------------------
def _marker_path(name):
    from . import ai_provider as _p
    return os.path.join(_p._control_dir(), name)


def _read_marker(name) -> str:
    try:
        with open(_marker_path(name)) as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _write_marker(name, value) -> bool:
    """Atomic (temp file + rename): a crash never leaves a half-written marker. False if it can't be written."""
    path = _marker_path(name)
    try:
        with open(path + ".tmp", "w") as fh:
            fh.write(value)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(path + ".tmp", path)
        return True
    except OSError:
        return False


def _summary_at(at):
    """'HH:MM' → (hour, minute), or None when off/invalid."""
    try:
        hh, mm = (int(x) for x in (at or "").split(":"))
    except ValueError:
        return None
    return (hh, mm) if 0 <= hh < 24 and 0 <= mm < 60 else None


def _summary_since(now):
    """The previous summary's time (the marker), else the last 24 h."""
    try:
        since = datetime.fromisoformat(_read_marker(_SUMMARY_MARKER))
    except ValueError:
        since = None
    return since if since is not None and since < now else now - timedelta(hours=24)


def run_daily_summary(now=None, at=None) -> dict:
    """Once per UTC day at/after DAILY_SUMMARY_AT: the ops summary (app/ops_summary.py) to the admin Telegram chat.
    The day's marker is written (atomically) BEFORE sending, so a restart can never send it twice; a summary that
    fails to build writes no marker and is retried on the next pass; with nothing to report the day is just marked."""
    hm = _summary_at(DAILY_SUMMARY_AT if at is None else at)
    if hm is None:
        return {"skipped": "off"}
    now = now or datetime.utcnow()
    if (now.hour, now.minute) < hm:
        return {"skipped": "not due"}
    if _read_marker(_SUMMARY_MARKER)[:10] == now.strftime("%Y-%m-%d"):
        return {"skipped": "done today"}
    try:
        from . import ops_summary
        with Session(engine) as s:
            text, news = ops_summary.build(s, now, _summary_since(now))
    except Exception as e:  # noqa: BLE001 — isolated; no marker, so the next pass retries
        logger.exception("daily summary failed (isolated; retried next pass)")
        return {"error": str(e)}
    if not _write_marker(_SUMMARY_MARKER, now.isoformat(timespec="seconds")):
        logger.error("daily summary NOT sent: its marker can't be written (it would repeat every pass)")
        return {"error": "marker not writable"}
    if not news:
        return {"skipped": "nothing to report"}
    try:
        return {"sent": bool(send_message(text))}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def preview_daily_summary(send=False, now=None) -> str:
    """The CLI: the summary as it would go out now (since the last one). Never touches the marker; send=True also
    sends it to the admin chat."""
    from . import ops_summary
    now = now or datetime.utcnow()
    with Session(engine) as s:
        text, _news = ops_summary.build(s, now, _summary_since(now))
    if send:
        send_message(text)
    return text


def run_once():
    """One full pass: CSV inbox always, portal if creds set, enrich/inbound-email if enabled."""
    init_db()
    out = [run_inbox()]
    if GO4WORLD_ENABLED:
        out.append(run_portal())
    if ENRICH_INTERVAL > 0:
        out.append(run_enrich())
    if IMAP_ENABLED:
        out.append(run_inbound_email())
    if FOLLOWUP_ENABLED:
        out.append(run_followups())
    out.append(run_request_reminders())
    out.append(run_work_item_sync())
    out.append(run_campaign_send())
    return out


def main():
    if "--portal" in sys.argv:
        init_db()
        print(run_portal())
        return
    if "--enrich" in sys.argv:
        init_db()
        print(run_enrich())
        return
    if "--inbound" in sys.argv:
        init_db()
        print(run_inbound_email())
        return
    if "--followups" in sys.argv:
        init_db()
        print(run_followups())
        return
    if "--reminders" in sys.argv:
        init_db()
        print(run_request_reminders())
        return
    if "--sync-workitems" in sys.argv:
        init_db()
        print(run_work_item_sync())
        return
    if "--campaigns" in sys.argv:
        init_db()
        print(run_campaign_send())
        return
    if "--daily-summary" in sys.argv:
        init_db()
        print(preview_daily_summary(send="--send" in sys.argv))
        return
    if "--once" in sys.argv:
        for r in run_once():
            print(r)
        return

    logger.info("worker started; inbox every %ss, portal %s, web-enrich %s, inbound-email %s",
                INGEST_INTERVAL,
                f"every {GO4WORLD_INTERVAL}s" if GO4WORLD_ENABLED else "disabled (no creds)",
                f"every {ENRICH_INTERVAL}s (batch {ENRICH_BATCH})" if ENRICH_INTERVAL > 0
                else "disabled (set ENRICH_INTERVAL)",
                f"every {IMAP_INTERVAL}s" if (IMAP_ENABLED and IMAP_INTERVAL > 0)
                else "disabled (set IMAP_*)")
    if DAILY_SUMMARY_AT and _summary_at(DAILY_SUMMARY_AT) is None:
        logger.warning("DAILY_SUMMARY_AT=%r is not HH:MM (UTC) — the daily summary is off", DAILY_SUMMARY_AT)
    _heartbeat()      # first beat at startup so the container is healthy before the first full pass completes
    last_portal = last_enrich = last_imap = last_followup = last_reminder = last_worksync = 0.0
    last_campaign = 0.0
    last_backup = _last_backup_time() if BACKUP_INTERVAL > 0 else 0.0   # a restart keeps the backup cadence
    while True:
        try:
            init_db()
            r = run_inbox()
            if r["new"] or r["seen"]:
                logger.info("inbox %s", r)
            now = time.time()
            if GO4WORLD_ENABLED and now - last_portal >= GO4WORLD_INTERVAL:
                logger.info("portal %s", run_portal())
                last_portal = now
            if ENRICH_INTERVAL > 0 and now - last_enrich >= ENRICH_INTERVAL:
                run_enrich()
                last_enrich = now
            if IMAP_ENABLED and IMAP_INTERVAL > 0 and now - last_imap >= IMAP_INTERVAL:
                run_inbound_email()
                last_imap = now
            if FOLLOWUP_ENABLED and FOLLOWUP_INTERVAL > 0 and now - last_followup >= FOLLOWUP_INTERVAL:
                logger.info("followups %s", run_followups())
                last_followup = now
            if REQUEST_REMINDER_INTERVAL > 0 and now - last_reminder >= REQUEST_REMINDER_INTERVAL:
                rr = run_request_reminders()
                if rr.get("reminded"):
                    logger.info("request reminders %s", rr)
                last_reminder = now
            if WORKITEM_SYNC_INTERVAL > 0 and now - last_worksync >= WORKITEM_SYNC_INTERVAL:
                ws = run_work_item_sync()
                if ws.get("total"):
                    logger.info("work-item sync %s", ws)
                last_worksync = now
            if CAMPAIGN_SEND_INTERVAL > 0 and now - last_campaign >= CAMPAIGN_SEND_INTERVAL:
                cs = run_campaign_send()
                if cs.get("sent") or cs.get("error"):
                    logger.info("campaign-send %s", cs)
                last_campaign = now
            if DAILY_SUMMARY_AT:
                ds = run_daily_summary()
                if not ds.get("skipped"):
                    logger.info("daily summary %s", ds)
            if BACKUP_INTERVAL > 0 and now - last_backup >= BACKUP_INTERVAL:
                run_backup()
                last_backup = now
        except Exception:
            logger.exception("worker pass failed")
        _heartbeat()      # mark the loop alive AFTER each pass (even a failed one) for the health check
        time.sleep(INGEST_INTERVAL)


if __name__ == "__main__":
    main()
