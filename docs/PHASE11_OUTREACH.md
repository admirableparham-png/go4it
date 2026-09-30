# Phase 11 — Outreach readiness (first real buyer send)

Tag `phase-11-outreach-readiness`. Goal: email TRSHARKS's buyers (SR-202608-0001) from **info@qmatalsaha.com**
safely. Buyers never see "g4it/go4it" — everything buyer-facing is the registered company (qmatalsaha.com).

## What changed
| Area | Change |
|---|---|
| Rendering | `app/campaign_render.py` — ONE render used by the sender and the dry-run: merge fields `{company}` `{country}` `{city}` (fail closed), optional sanitized HTML design, footer (company + postal address + "reply unsubscribe") in both parts, `List-Unsubscribe: <mailto:…>`, seller-name guard (blocks, never "[redacted]"), internal-brand guard. |
| Sending | Render before claim (no slot used on a bad message); auth/quota/config failures pause the MAILBOX, refund the slot, keep the send; stuck recipients settle so campaigns finish; bounce breaker (≥20 sent, ≥8% hard bounces → pause). |
| Inbox | IMAP read-only + `BODY.PEEK` + `InboundSeen` ledger (first poll = baseline); HTML-only replies read; quoted history/footer ignored when detecting unsubscribes; unmatched opt-outs still suppressed; newsletters ignored. |
| Funnel | Managed buyer: send → `contacted`, human reply → `responded` (forward-only). |
| Controls | `/mail`: Go4it-owned, pause, daily limit, from name, sender company, postal address, re-enter App Password. Campaign: daily limit, mailbox, UTC window/days, **Preview email**, start gate (`start_problems`). Enrol always scoped to the campaign's request + must match the previewed count; unique per campaign. |
| Security | Founder can't be taken over (password reset / demote / disable / deny need founder.control); only internal staff with `outreach.email.send` email from a lead page; bulk "Email selected" skips managed buyers. |
| Scripts | `load_managed_buyers.py` (replaces `deliver_request.py`, which now refuses), `campaign_dryrun.py`, `campaign_setup.py` (smoke / real campaign from a template folder), `migrate_gate_p11.py`. |
| Template | `campaigns/trsharks-anchors/` — founder's text (placeholders → `{company}` `{country}`), price list in the body instead of an attachment, light HTML with the qmatalsaha.com logo. Greeting names drop brackets + legal forms ("IHL Canada (Investments Hardware Ltd.)" → "IHL Canada"). |
| Ops | `.dockerignore` + `.gitattributes export-ignore`: buyer lists + HANDOFF/CLAUDE never ship. Optional `BACKUP_INTERVAL` automatic backups from the worker. |

## Runbook (founder pastes server commands)
1. `/outreach/pause-all` ON. Backup → copy to `backups/pre-p11-<ts>.db` → `docker tag go4it:latest go4it:pre-p11-rollback`.
2. Mac: `git archive phase-11-outreach-readiness-v2 | ssh root@167.233.138.214 'tar x -C /opt/go4it'` →
   server: `docker compose -f docker-compose.coexist.yml up -d --build`.
3. `docker exec go4it-app python scripts/migrate_gate_p11.py --dry-run` → without `--dry-run` → canaries.
4. `/opt/go4it/.env` (plain `KEY=value` lines, no comments): `IMAP_HOST=imap.gmail.com`, `IMAP_PORT=993`,
   `IMAP_USER=info@qmatalsaha.com`, `IMAP_PASSWORD=<app password>`, `IMAP_INTERVAL=120`,
   `CAMPAIGN_SEND_MAX_PER_RUN=1`, `CAMPAIGN_SEND_INTERVAL=180`, `FOLLOWUP_ENABLED=false`, (optional)
   `BACKUP_INTERVAL=86400`. Keep `SMTP_*` empty. Then `docker compose -f docker-compose.coexist.yml up -d`.
5. `/mail`: connect info@qmatalsaha.com (App Password) → controls: Go4it-owned, from name, company, postal
   address, limit 10.
6. Load buyers (Mac, streams the file — nothing left on the server):
   `ssh root@… 'docker exec -i go4it-app python scripts/load_managed_buyers.py --request SR-202608-0001 --stdin --exclude-countries US,MX --dry-run' < docs/prospects/buyers_trsharks.json`
   then again without `--dry-run`.
7. Smoke (sends ONLY to the founder's addresses, real buyer names for realism):
   `docker exec go4it-app python scripts/campaign_setup.py --template campaigns/trsharks-anchors --mailbox info@qmatalsaha.com --smoke "admirable.parham+1@gmail.com|IHL Canada (Investments Hardware Ltd.)|CA" --smoke "admirable.parham+2@gmail.com|Inoxa Sp. z o.o.|PL" --smoke "admirable.parham+3@gmail.com|Dani Trading LLC|AE" --start` (Pause-All off).
   Real campaign later: same script with `--request SR-202608-0001 --name "TRSHARKS anchors" --daily-limit 10` → `--enrol` → dry-run → `--start`.
8. Stress: `pytest tests/test_phase11_stress.py` + on prod `docker exec go4it-app python scripts/campaign_dryrun.py <cid>` → 0 errors.
9. Real send: limit 10 → 20 → 35 → 50/day, Mon–Fri 08–18 UTC. Pause-All on trouble.

Rollback: restore `pre-p11` DB + `docker tag go4it:pre-p11-rollback go4it:latest` + `up -d` (no `--build`).
