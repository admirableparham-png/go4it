# Production canary — runbook (founder-only)

A controlled, minimal live-path test for **one** Go4it admin-owned mailbox, sending **only** to an
allowlisted internal test address. It never uses a production campaign audience and keeps every inferred
legacy group paused. Run it once before trusting live outreach.

## What runs automatically (safe, no external email)
`./.venv/bin/python scripts/canary.py` runs three **mechanism checks** in an isolated in-memory database
(production untouched):
1. **Suppression blocks a second send** — a suppressed address is refused by the send safety chain.
2. **Pause-All stops the worker** — with the global kill switch on, the campaign worker sends nothing.
3. **Sellers see nothing** — a seller gets 403 on Campaigns / Inbox / Suppression / Email Accounts /
   Analytics, and a buyer email never leaks into any seller-visible response.

If live credentials are not configured, the script reports the live canary as **NOT RUN** and never simulates
a successful live result.

## Enabling the LIVE canary
Set these in the environment (never commit real addresses):
```bash
export CANARY_ENABLED=1
export CANARY_ALLOWLIST="you@yourdomain.com,ops@yourdomain.com"
export CANARY_TEST_RECIPIENT="you@yourdomain.com"        # MUST be inside CANARY_ALLOWLIST
export CANARY_BAD_RECIPIENT="nouser@yourdomain.com"      # a known-invalid address for the bounce test
```
Connect one live admin-owned mailbox with a valid SMTP app-password at **/mail** (credentials are stored
encrypted — see docs/CREDENTIAL_ENCRYPTION.md). Confirm SPF/DKIM/DMARC on that domain out-of-band.

## Live steps (each confirmed by a human)
Do these against a **draft** canary campaign whose only recipient is `CANARY_TEST_RECIPIENT`. Never point it
at a real audience.
1. **Send** step 1 to the test address. Confirm it arrives.
2. **Message-ID persisted** — verify the sent `Outreach.message_id` is non-empty and matches the header in
   the received mail; verify `CampaignSend.status='sent'` with a `provider_message_id`.
3. **Reply matching** — reply from the test inbox. Confirm the inbound is threaded to the recipient via
   `In-Reply-To`, the recipient stops, and a `review_inbound_reply` work item appears.
4. **Unsubscribe** — reply "unsubscribe". Confirm the address is added to Suppression and no further send goes.
5. **Controlled bounce** — enroll `CANARY_BAD_RECIPIENT`, send, and confirm the hard bounce suppresses the
   address, cancels the recipient, and writes a `BounceRecord`.
6. **Suppression prevents another send** — attempt to re-send to the now-suppressed test address; confirm it
   is refused.
7. **Pause-All** — toggle Pause-All; confirm the worker sends nothing until it is cleared.
8. **Seller blindness** — from a seller account, confirm none of the mailbox, recipient identity, reply,
   suppression entry, or buyer contact is visible.

## After a clean live pass
Only once every live step above passes should the final production tag be cut:
```bash
git tag -a phase-4-production-ready -m "Phase 1–4 hardened; live canary passed"
```
Until then the code carries the `phase-4-precanary` tag (hardened, live canary not yet run).
