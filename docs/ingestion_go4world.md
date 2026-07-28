# Automated go4worldbusiness ingestion (browser bot)

Logs into **your own** go4worldbusiness account, opens the buy-lead pages you
configure, and imports new Georgia tile/brick leads into go4it — matched, quoted,
and deduped. Runs hourly.

> ⚠️ **Account risk.** Automated access likely conflicts with go4worldbusiness's
> Terms and can get your paid account suspended. Keep the cadence gentle (hourly,
> the default) and stop if you get any warning. You chose this route knowingly.

## One-time setup

1. **Put your login in `.env`** (never committed — `.env` is gitignored):
   ```
   GO4WORLD_EMAIL=you@example.com
   GO4WORLD_PASSWORD=your-password
   GO4WORLD_LEAD_URLS=https://www.go4worldbusiness.com/buyers/georgia/ceramic-tiles.html,https://www.go4worldbusiness.com/buyers/georgia/bricks.html
   GO4WORLD_HEADLESS=false     # watch the first run; set true once it works
   ```

2. **First run — capture the real page** (so the parser can be tuned):
   ```
   make ingest-portal
   ```
   This logs in and saves each page's HTML + screenshot to **`./debug/`**
   (`after-login.png`, `leads-*.html`, `leads-*.png`). It imports 0 leads on this
   first run by design — send me the `./debug` files and I'll write the exact
   parser for your account's layout.

3. **Go live hourly** once the parser is tuned — either:
   - **launchd (recommended, survives reboot):** see
     `docs/launchd/com.go4it.hourly-ingest.plist` (fires `--once` every hour), or
   - **a long-running worker:** `make worker` (inbox often + portal hourly).

## What it does each pass
`login → open lead URLs → extract buy-leads → dedupe → match to catalog →
auto-quote → Telegram alert`, and records an IngestionRun (see the **Ingest** page).
Re-runs are idempotent, so nothing is imported twice.

## Notes
- `GO4WORLD_HEADLESS=false` shows the browser — useful if login needs a captcha or
  2FA on the first run (do it once headful, then switch to headless).
- Buyer **contact details** on go4worldbusiness are unlocked by *responding* to a
  lead (your ~100/day quota); the bot captures the lead **feed**, and go4it queues
  the best ones + auto-drafts the quote for your team to send within quota.
