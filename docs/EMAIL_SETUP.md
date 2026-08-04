# Turning on two-way email (Conversation panel)

go4it's Conversation panel is a full two-way email thread — **the code is complete and dormant**. It
goes live purely by adding credentials + running the worker. Nothing here needs code changes.

## What you get once it's on
- **Send** from inside a lead's Conversation panel (SMTP). Each message stamps a `Message-ID`.
- **Receive**: the worker polls your mailbox (IMAP), and threads each buyer reply onto the right lead —
  matched first by the **In-Reply-To** header of the reply (the outbound we sent), then by the sender's
  email, then by phone. It stamps `buyer_replied_at`, shows an inbound bubble, and pings Telegram.
- Buyers can also **Accept / Request changes** on the public pro-forma (`/p/<token>`) — captured on-platform.

## 1. Outbound — SMTP (required to send)
Set in `.env` (Gmail example — use an **App Password**, not your login password):

```
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=you@yourdomain.com
SMTP_PASSWORD=your-app-password
SMTP_FROM=you@yourdomain.com        # optional; defaults to SMTP_USER
```
`SMTP_ENABLED` flips true automatically when host+user+password are all set. The Conversation panel's
"Send email + log" button appears only when SMTP is on (otherwise it just logs the message).

## 2. Inbound — IMAP (required to receive replies)
```
IMAP_HOST=imap.gmail.com
IMAP_PORT=993
IMAP_USER=you@yourdomain.com
IMAP_PASSWORD=your-app-password
IMAP_INTERVAL=120                    # seconds between inbox polls; 0 = OFF (default)
```
`IMAP_ENABLED` needs host+user+password; polling also needs `IMAP_INTERVAL > 0`.

## 3. Public links must resolve for the buyer
Set `BASE_URL` to your **public** host so the `/p/<token>` pro-forma links inside emails work:
```
BASE_URL=https://yourdomain.com
```

## 4. Run the worker (one process only)
The worker does inbox ingest + enrichment + inbound-email polling. Run it as a **single** process
(not inside gunicorn workers, or jobs double-fire):
```
python -m app.worker            # loop
python -m app.worker --inbound  # one inbound-email pass (for testing)
```

## Notes / limits
- Unmatched inbound senders are **skipped**, never auto-made into leads (by design).
- Threading uses In-Reply-To/References then sender identity — a reply from a brand-new address with
  no quoted headers may not match; forward it into the lead manually.
- No open/click tracking, bounce handling, or attachments yet (the pro-forma is a link, not a PDF).
