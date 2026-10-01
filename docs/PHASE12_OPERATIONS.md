# Phase 12 — running the first campaign safely (follow-up, warm-up, seller login, enrichment, clean queue)

Tag `phase-12-ops-followups`. Builds on Phase 11 (v7 live, campaign #33 "TRSHARKS anchors — wave 1" sending 10/day
from info@qmatalsaha.com). Independently reviewed in three rounds (13 + 12 + 6 confirmed findings so far, all fixed with regression tests).

## What changed
| Area | Change |
|---|---|
| Follow-up (email 2) | `scripts/campaign_followup.py add/reopen/approve` appends email 2 to a LIVE campaign in place (email 1 can never repeat). It goes as a reply in each buyer's own thread (In-Reply-To/References, "Re: <their subject>"), due at their own email 1 + N days, **held until the founder approves**. Never after a reply, a bounce (hard or soft) or an unsubscribe — and never unless a complete IMAP read of the inbox finished in the last 2 h (`CAMPAIGN_WARMUP_IMAP_FRESH_SEC`); first emails keep going. After an outage the first complete read reaches back to the last one (≤ 30 days), so replies sent meanwhile are seen before any follow-up. |
| Warm-up ramp | Campaign field "warm-up plan" (e.g. `10,20,35,50`): once per UTC day, before the first send, the campaign's own limit moves one step up after a full sending day with < 5 % hard bounces and IMAP working. Never lowers, never passes the mailbox limit; a hold raises one Work Queue task. |
| Daily summary | Telegram at `DAILY_SUMMARY_AT` (UTC `HH:MM`): sends, replies, bounces, warm-up, IMAP, backups. `python -m app.worker --daily-summary [--send]` to preview. |
| Backups | Worker backup every `BACKUP_INTERVAL` s; keeps `BACKUP_KEEP` (14); files chmod 600; WAL-safe. `make pull-backup` copies the newest to the Mac. |
| Seller view | Sellers see only seller-safe deliverables and the anonymized funnel (admin result files/links hidden). Every admin→seller text (chat, update, answer, delivery note/link) is checked against the seller's buyers: names, emails, sites, phones, contact persons (lead + Trade Network), cities. No longer blocked: a city where 3+ distinct buyers of the request are ("CIF Dubai"), regions/provinces, and a generic-word buyer name written as prose ("plaster wholesalers in the UK" — "Plaster Wholesalers confirmed" still blocks, and a name that is the buyer's own domain blocks in any case). Towns that are everyday words and surnames count only capitalised ("Split", not "split the order"). The block message names what to remove; sign-in starts a fresh session. Founder can change a login email (`/admin/users/{id}`). |
| Enrichment | `scripts/enrich_managed_buyers.py scan → apply → enrol` finds emails for email-less confidential buyers on their own sites — polite (robots.txt, neutral User-Agent, ≤ 8 requests per site), reviewed (CSV), one address per company domain unless approved, crash-safe per site. A bounce never re-arms an address; the found candidate goes to review. |
| Work Queue | Scanners close tasks whose condition is settled (archived product, approved quote, disposed duplicate, fresh source…). `scripts/cleanup_work_queue.py` clears the legacy backlog: dry-run by default, `--apply --actor <admin email>`, `--revert <batch>`; buyer-reply tasks are never touched. |
| campaign_setup | `--daily-limit` sets a new (or draft) campaign's limit only; a running / sent / warm-up campaign keeps its own. |

## Deploy (only after the founder says "deploy")
Run outside Mon–Fri 08–18 UTC, or with outreach Pause-All ON (`/outreach`), and turn it off afterwards.
```bash
# server
docker exec go4it-app python scripts/backup_db.py            # integrity_check=ok
docker tag go4it:latest go4it:pre-p12-rollback
# Mac (this repo)
git archive phase-12-ops-followups | ssh root@167.233.138.214 'tar x -C /opt/go4it'
# server — migrate.py runs at boot (adds campaign.warmup_plan / warmup_checked_on)
docker compose -f docker-compose.coexist.yml up -d --build
docker exec go4it-app python scripts/migrate_gate_p11.py --dry-run     # then without --dry-run
docker exec -e BASE_URL=http://localhost:8400 go4it-app python scripts/access_canary.py
```
`.env` (plain `KEY=value`): add `DAILY_SUMMARY_AT=18:05` (`BACKUP_INTERVAL=86400` is already set), then
`docker compose -f docker-compose.coexist.yml up -d` so the worker picks them up.

Then, in order:
1. Campaign #33 page → warm-up plan `10,20,35,50`.
2. Follow-up, held (outside the sending window): `campaign_followup.py add 33 --template
   campaigns/trsharks-anchors-followup --delay-days 7 --hold` → same with `--apply`. Release only after the founder
   approves the text: `campaign_followup.py approve 33 --email 2 --apply`.
3. Work Queue: `cleanup_work_queue.py` (dry-run) → `--apply --actor admin@go4it.local`.
4. Enrichment: scan in chunks of 120 → founder reviews the CSV → `apply --include-auto --mark-misses` (dry-run first)
   → `enrol --campaign 33 --expected N` (dry-run first). New recipients go to the END of #33's queue.
5. TRSHARKS seller login: set its password on `/admin/users`, log in as the seller and check that no buyer
   identity is visible anywhere.

Rollback: restore the backup + `docker tag go4it:pre-p12-rollback go4it:latest` + `up -d` (no `--build`).

## Later
- Canada (77 buyers): a separate wave after every TRSHARKS email has gone, with a postal address in the email —
  `load_managed_buyers.py --only-countries CA`.
- Mac worker: re-set up on the latest code (its local mail/Telegram credentials are parked as `DISABLED_LOCAL_*`).
