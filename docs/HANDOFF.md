# go4it — Handoff (2026-09-29, updated 2026-10-01)

Written when go4it moved out of the PULSE-LOCALHOST (TradeSitter) workspace into its own VS Code window.
Engineering facts below are as of **2026-08-31** (last go4it work). Anything about production is a month
old — re-check it before acting on it.

## 1. Open it

1. VS Code → **File → New Window** → **Open Folder** → `/Users/pstudio/go4it`.
2. Start Claude Code in that window. It loads `CLAUDE.md` and this project's own memory automatically.
3. First commands:
   ```bash
   git status && git log --oneline -3       # expect branch work/phase-9-ai-command-automation, clean
   make test                                 # expect 617 passed
   make run                                  # local app on http://localhost:8400
   ```
Nothing needs cloning or installing: the repo, `.venv`, `.env` and `data.db` are already in this folder.

## 2. Git state

| Item | Value |
|---|---|
| Working branch | `work/phase-9-ai-command-automation` (Phases 7–10 + this handoff) |
| `main` | `26f05cd` — 25 commits behind the working branch. Do not merge without a founder decision. |
| Deployed tag | `phase-13-local-hours-v3` → `6e374d7` (2026-10-02 ~11:40 UTC; Phase 12 06:15, Phase 13 09:25 UTC) |
| Rollback images | `go4it:pre-p13-rollback`, `go4it:pre-p12-rollback` (+ backups data-20261002-092036.db / -061335.db) |
| Remote | `origin` = github.com/admirableparham-png/go4it (private). All branches + tags pushed 2026-09-29. |

Tags are immutable. New work = new commits + a new tag.

## 2b. 2026-09-30 update

- **Phase 10 deployed** (gate OK, 11 role templates + 6 profiles, access canary 16/16, counts unchanged). Rollback:
  `backups/pre-p10-20260930-121529.db` + image `go4it:pre-p10-rollback`.
- **Security:** demo users `sara@go4it.local` (manager) and `ali@go4it.local` (agent) still had seed passwords on prod —
  both DISABLED with random passwords. SSH is key-only (one key: pstudio@mac-studio).
- **Mac worker stopped:** launchd `com.kimiel.go4it.worker` (old code, live Gmail, follow-ups on) killed +
  `launchctl disable`d. Re-enable only after it is moved to the latest code.
- **Phase 11 (outreach readiness)** built on this branch — 726 tests. Runbook + what changed: `docs/PHASE11_OUTREACH.md`.
- **Founder rules:** g4it.vip is internal-only; buyer-facing = qmatalsaha.com. Founder pastes server commands (or says
  "you run it"). Never touch tradesitter; never `docker compose down`; never `docker image prune` (shared box).
- `make run` is no longer used (no local ports); verify with `make test`.

## 2c. 2026-10-01 update

- **Phase 11 live (v7)** — first real campaign: #33 "TRSHARKS anchors — wave 1", info@qmatalsaha.com, 406 buyers
  enrolled (US/MX/CA excluded, best first), 10/day Mon–Fri 08–18 UTC. Day 1: 10 sent, 0 errors. Replies → Telegram.
- **Phase 12 built** (follow-up email 2 held for approval, warm-up ramp, daily Telegram summary, safe seller login,
  reviewed email enrichment for the 462 email-less buyers, Work Queue cleanup). Reviewed twice (13 + 12 findings, plus
  sign-in now starts a clean session) — all fixed with tests. Runbook: `docs/PHASE12_OPERATIONS.md`.
- **Founder decisions:** Canada (77) = a separate wave after all TRSHARKS emails, with a postal address in the email;
  TRSHARKS gets a seller login (only after Phase 12 is live); the founder's test-reply tasks stay until he explains.
- **Local creds parked:** `.env` TELEGRAM_*/SMTP_*/IMAP_* renamed `DISABLED_LOCAL_*` after a test script sent a false
  Telegram alert — nothing on the Mac can send.

## 2d. 2026-10-02 update

- **Phase 12 + 13 deployed.** #33 now sends in each buyer's own hours (09:00–11:00, 14:00–16:00 their time, their
  working week) best-ranked first; warm-up 10→20 (Fri) → 35 (Sun) → 50; daily Telegram summary 18:05 UTC.
- **Enrichment:** 462 email-less buyers scanned → 133 company addresses added to #33 (end of queue); 75 personal
  addresses + 26 weak ones wait for the founder (`debug/enrich_req1_[a-d].csv`, local only).
- **Work Queue:** 162 legacy tasks dismissed (batch wqc-20261002-061915, revertible); 39 left incl. the founder's
  test-reply items (untouched until he explains them).
- **Founder:** admin login stays admin@go4it.local; sets user passwords himself (Users → user → Security).
- **Follow-up (email 2):** the founder's text (wording "sent recently") is on #33, HELD with the rule "starts by itself
  once every earlier email is out" (founder chose: only after all first emails). Telegram announces the start.
- **Enrichment round 2:** 99 reviewed addresses (personal ones OK per founder) added → #33 = 638 buyers.

## 3. Production (https://g4it.vip) — as of 2026-08-31 (Phase 10 since deployed — see 2b)

- Running **v5** (Phases 1–9). Phase 10 (roles & access control) is **tagged but NOT deployed**.
- Claude copilot **live, founder-only**: `AI_MODEL=claude-sonnet-5`, `AI_LIVE_ALLOWLIST=admin@go4it.local`,
  cost caps on, Pause-All is file-based and cross-process. Key only in `/opt/go4it/.env`.
- **Email: BLOCKED** — no Go4it mailbox/SMTP credentials on prod; campaigns paused.
- **Malware scanner: BLOCKED** — none configured; document exchange stays gated.
- Counts at last deploy: users 6 · leads 5029 · quotes 56 · deals 1 · products 96 · outreach 140 · requests 3.
- Backup before deploy: `/opt/go4it/backups/data-20260826-105901.db`.

### Deploying Phase 10 (only when the founder says so)
```bash
# on the server (ssh root@167.233.138.214, cd /opt/go4it):
docker exec go4it-app python scripts/backup_db.py
# from the Mac, in this repo:
git archive phase-10-profiles-roles-access-control | ssh root@167.233.138.214 'tar x -C /opt/go4it'
# back on the server:
docker compose -f docker-compose.coexist.yml up -d --build
docker exec go4it-app python scripts/migrate.py
docker exec go4it-app python scripts/migrate_gate_p10.py --dry-run      # then without --dry-run
docker exec go4it-app python scripts/backfill_access_control.py --dry-run   # then without
```
Mapping: admin→Founder, manager→Admin/Manager, viewer→Auditor, agent→Seller. Rollback:
`backfill_access_control.py --rollback` or restore the backup. Verify with `scripts/access_canary.py` (16/16).

## 4. Today's task A — find buyers for new products

Rules (non-negotiable, see CLAUDE.md): two parts (RFQ posters 2025–26 / bulk-wholesale potential), bulk rows
highlighted, website always, **source only in the `_ADMIN` csv**, no retail or junk.

- Template: `scripts/gen_copper_buyers_report.py` (newest) and `scripts/gen_anchors_buyers_report.py`.
- Output per product in `docs/prospects/`: `<product>_buyers.json`, `<product>_buyers_client.html`,
  `<product>_buyers_ADMIN.csv` (+ PDF if wanted).
- Sources: go4world (paid account), Alibaba RFQ, TradeKey, ExportHub, TradeWheel, IndiaMART/TradeIndia, EC21,
  eWorldTrade, customs/importer data, per-country directories (e.g. yellowpages-uae).
- Scale: broad commodities (tea, honey, zinc) → 2,000–3,000+; a narrow 1–2 SKU niche tops out near
  1,000–1,200 distinct verified buyers (TRSHARKS: 3,888 raw → 1,011 distinct). Don't pad.
- Big multi-agent sweeps hit the daily usage cap (~14M tokens a wave) — stage waves across resets.
- Product ideas from the founder's notes, researched: `docs/strategy/export_strategy_report_fa.html`.
  Best fits: handmade leather, rose/saffron candles & diffusers, khatam/firoozeh-koobi, termeh/small kilims.
  Weak: raw stone by sea, copperware *into* Turkey/India, zinc as a product line.
  **Market reality (Aug 2026):** UAE suspended all trade and payments with Iran (~19 Aug 2026); US/Canada
  ban Iranian-origin goods; EU accepts the goods but EU banks refuse Iran payments. Recheck before quoting.

## 5. Today's task B — email buyers we already have

Buyer lists already in `docs/prospects/`:

| Product | Files |
|---|---|
| Zinc (sulphate) | `zinc_sendlist.*`, `zinc_send_207.*` (207 ready), `scripts/send_zinc.py`, `scripts/gen_zinc_sendlist.py` |
| Honey | `honey_sendlist.*`, `honey_callsheet.*`, `honey_offer_sheet_{en,fa}.*`, `honey_buyers_*` |
| Copper ingots | `copper_ingots_buyers_client.html`, `_ADMIN.csv`, `buyers_copper.json` |
| Drywall anchors (TRSHARKS) | `trsharks_anchors_buyers_*`, `buyers_trsharks.*` (1,011) |
| UAE home decor | `decoration_buyers.*` |
| Blank CD/DVD (UAE) | `cd-dvd_buyers.*` |
| Georgia chemicals | `georgia_chem_buyers.*` |
| Georgia brick/tile/rubber | `georgia_prospects.*` |
| Iran export buyers by country | `iran_buyers_by_country_*`, `iran_export_*` |

Steps to send safely:
1. **Connect the Go4it mailbox** — on g4it.vip as admin → `/mail` → add a Gmail/Workspace account with an
   **App Password** (stored Fernet-encrypted; verified over SMTP before save). Mark it Go4it-owned.
2. **Canary** — send one message to your own address from that account; confirm delivery, not spam.
3. **Load the list** as managed buyers (admin pool, `owner_id=NULL`) — reuse the loader pattern in
   `scripts/` for the product; dedup against existing leads.
4. **Send in batches** — `/leads` → select → "✉ Email selected" (capped at 60 per send) or `/campaign`.
   Suppression list is enforced; stay well under the mailbox's daily sending limit.
5. **Replies** — set `IMAP_*` in `/opt/go4it/.env` (same mailbox) so replies land in the inbox and move the
   pipeline stage.

## 6. Known gaps / deferred

- Internal least-privilege is partial: quote/contract approve, deal advance, freight, campaign start,
  suppression, docs publish, remittance, research still gate on "internal staff" not a granular permission.
  Seller confidentiality is fully enforced.
- MFA not implemented. No impersonation (permission preview only).
- Four handwriting readings in the export notes still unconfirmed (see the report, section 2).
- `/Caddyfile` stray file on the server root — harmless, can be removed.
