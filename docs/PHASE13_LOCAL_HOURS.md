# Phase 13 — every buyer gets the email in their own business hours

Founder's request (2026-10-02): send considering each buyer's time zone, at the best hours.

## How it works
- Campaign field **buyer local hours** (campaign page, or `campaign_setup.py --local-hours`), e.g.
  `09:00-11:00,14:00-16:00` — the two best B2B reading slots. Empty = the old UTC window/days.
- Each buyer's clock = their country's business time zone (`app/local_time.py`); Australia, Canada, the US, Mexico,
  Brazil, Russia, Indonesia and Kazakhstan use the city/state too (Perth ≠ Sydney, Vancouver ≠ Toronto).
- Working days = the buyer's week: Mon–Fri; Sun–Thu in Saudi Arabia, Kuwait, Qatar, Bahrain, Oman, Israel, Egypt,
  Jordan, Iraq …; the UAE works Mon–Fri. No afternoon email on the last working day of their week (Friday;
  Thursday in the Gulf).
- Order stays the ranked list: each UTC day the day's quota (warm-up limit) is kept for the best-ranked buyers whose
  hours still come that day — a top-ranked buyer in Auckland keeps its place although Europe's mornings come first.
  Buyers that can no longer be sent (opted out, replied, bounced) never take a slot and are settled.
- Daily limit, warm-up, bounce breaker, Pause-All and the follow-up rules are unchanged. The warm-up now looks back
  3 days for a full sending day (a weekend UTC day can be short: only Gulf / Monday-morning Asia-Pacific buyers).

| Buyer region | 09:00–11:00 local = UTC (Oct 2026) |
|---|---|
| UK, Ireland, Portugal | 08:00–10:00 (09:00–11:00 after 25 Oct) |
| Central Europe, South Africa | 07:00–09:00 (08:00–10:00 after 25 Oct; South Africa stays) |
| Finland, Greece, Romania, Israel | 06:00–08:00 (07:00–09:00 after 25 Oct) |
| Saudi Arabia, Kuwait, Qatar | 06:00–08:00 (Sun–Thu) |
| UAE | 05:00–07:00 |
| New Zealand | 20:00–22:00 the day before |
| Australia (Sydney/Melbourne · Brisbane · Perth) | 22:00–00:00 · 23:00–01:00 the day before · 01:00–03:00 |

## Switch it on (after deploy)
Campaign page → buyer local hours `09:00-11:00,14:00-16:00` → Save. The stop-gap holds put on 2 Oct (Gulf/Israel
Sunday 06:00 UTC, UAE Monday 05:00, NZ Sunday 20:00, AU Sunday 22:00) simply expire.

Also in this release: the campaign page shows its save messages; Admin → Users → user → **Security** tab → set a
password (masked, typed twice; the user is signed out everywhere).

## Follow-up after every first email (founder's choice, 2 Oct)
`campaign_followup.py approve 33 --email 2 --when-earlier-done --apply` keeps email 2 held and lets the worker start
it by itself once no buyer can still get email 1 (opted-out / replied / bounced buyers and sends held for review don't
count); a Telegram message announces it. A later `--replace` of the text keeps that rule; a plain `approve` refuses to
override it without `--now`. The daily summary shows "Email 2: held — starts by itself once every earlier email is out".
