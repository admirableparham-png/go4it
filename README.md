# go4it

A deal-sourcing tool for a small trading team. Feed in **demands** (a buyer wants
something) and **offers** (a seller has something). go4it **matches** them
automatically and **alerts you and your colleagues on Telegram + a web dashboard**
the moment there's a fit — so you can jump in, contact the parties, and broker the
deal in the real world.

The loop: **Demand + Offer → Match → Instant alert → You act.**

## Stack

Deliberately simple, fast, and easy to change:

- **FastAPI** (Python) — the app + matching engine
- **SQLite** — one local file, zero setup (swap to Postgres later with one line)
- **HTMX + Tailwind** — a snappy dashboard with no frontend build step
- **Telegram Bot API** — instant alerts to your team

## Quick start

```bash
make install     # one-time: create venv + install deps
make seed        # optional: load demo demands/offers so matching shows immediately
make run         # start the app -> http://localhost:8400
```

Open http://localhost:8400, add a demand and an offer, and matches appear at the top.

## Telegram alerts (optional)

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy the token.
2. Add the bot to your team's group chat.
3. `cp .env.example .env` and fill in `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.

With those set, every new match is pushed to the group instantly. Without them, the
app runs fine — alerts are just off.

## How matching works

For each demand/offer pair, `app/matching.py` produces a 0-100 score from:

- **fuzzy text similarity** of product + category + spec (the main signal)
- **category** match
- **quantity** — does the seller cover the buyer's amount?
- **price** — is the seller within (or near) the buyer's budget?
- **location** match

Pairs scoring at or above `MATCH_THRESHOLD` (default 60) become matches. Tune the
threshold in `.env`. The engine is a single function, so adding semantic/AI matching
later is a drop-in change.

## Daily save

At the end of each day, run:

```bash
./save.sh        # or: make save
```

It commits everything and pushes to GitHub with a short status report, so you always
have proof the day's work is safe.

## Project layout

```
app/
  main.py        FastAPI app + routes
  models.py      Demand, Offer, Match tables
  matching.py    the scoring engine
  telegram.py    team alerts
  db.py          database engine
  config.py      settings from .env
  seed.py        demo data
  templates/     HTMX + Tailwind UI
save.sh          end-of-day commit + push
Makefile         install / run / seed / save
```

## Roadmap ideas

- Semantic matching (embeddings) for free-text demands
- Edit / close demands & offers from the UI
- Per-user accounts for colleagues
- Match history & win-rate stats
- Import demands/offers from Telegram messages or a spreadsheet
