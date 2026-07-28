"""go4worldbusiness authenticated portal scraper (browser bot).

Logs into the operator's OWN paid account with Playwright, reuses the session
(so it doesn't re-login every hour), visits the configured buy-lead pages, and
returns RawLeads through the normal dedupe -> match -> quote -> alert pipeline.

⚠️ go4worldbusiness actively rate-limits/bot-detects automated access ("Too many
requests"), and this likely conflicts with their Terms and can risk the account.
Enabled only when GO4WORLD_EMAIL/PASSWORD are set. Runs gently (session reuse,
long random pauses, light stealth). Page HTML + screenshots are saved to ./debug
for selector tuning; parse_leads_html returns [] until tuned to the real DOM.
"""
import logging
import os
import random
import re
import time

from ..config import (DEBUG_DIR, GO4WORLD_EMAIL, GO4WORLD_HEADLESS,
                      GO4WORLD_LEAD_URLS, GO4WORLD_LOGIN_URL, GO4WORLD_PASSWORD)
from .base import LeadSource, RawLead

logger = logging.getLogger("go4it.go4world")

_COUNTRY = {"georgia": "GE", "turkey": "TR", "türkiye": "TR"}
_STATE_FILE = os.path.join(os.path.dirname(DEBUG_DIR), ".go4world_session.json")
# Hide the most obvious automation fingerprint.
_STEALTH_JS = "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"


def _slug(url: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", url.lower()).strip("-")[-48:]


def _is_rate_limited(html: str) -> bool:
    return "too many requests" in (html or "").lower()


def _needs_login(html: str) -> bool:
    low = (html or "").lower()
    return "please login" in low or "sign in with otp" in low


def parse_leads_html(html: str):
    """Parse a go4worldbusiness buy-leads page into RawLeads.

    PLACEHOLDER: returns [] until tuned to the real logged-in DOM (captured to
    ./debug). Replacing this body is the only change needed to go live.
    """
    return []


class Go4WorldPortalSource(LeadSource):
    name = "go4world_portal"

    def __init__(self, email=GO4WORLD_EMAIL, password=GO4WORLD_PASSWORD,
                 login_url=GO4WORLD_LOGIN_URL, lead_urls=None, headless=GO4WORLD_HEADLESS):
        self.email = email
        self.password = password
        self.login_url = login_url
        self.lead_urls = lead_urls if lead_urls is not None else GO4WORLD_LEAD_URLS
        self.headless = headless
        self.blocked = False

    def fetch(self):
        if not (self.email and self.password):
            logger.info("go4world portal disabled (no GO4WORLD_EMAIL/PASSWORD)")
            return []

        from playwright.sync_api import sync_playwright

        os.makedirs(DEBUG_DIR, exist_ok=True)
        leads = []
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.headless)
            ctx_args = dict(
                user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/126.0.0.0 Safari/537.36"),
                locale="en-US", viewport={"width": 1440, "height": 900})
            if os.path.exists(_STATE_FILE):
                ctx_args["storage_state"] = _STATE_FILE      # reuse prior session
            ctx = browser.new_context(**ctx_args)
            ctx.add_init_script(_STEALTH_JS)
            page = ctx.new_page()
            try:
                if not os.path.exists(_STATE_FILE):
                    self._login(page, ctx)
                for url in self.lead_urls:
                    time.sleep(random.uniform(6.0, 12.0))    # gentle spacing
                    page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    page.wait_for_timeout(3000)
                    html = page.content()
                    if _is_rate_limited(html):
                        self.blocked = True
                        logger.warning("go4world RATE-LIMITED on %s (bot detection)", url)
                        self._dump(page, "blocked-" + _slug(url))
                        break
                    if _needs_login(html):                   # session expired -> re-login once
                        logger.info("go4world session expired; re-logging in")
                        self._login(page, ctx)
                        page.goto(url, wait_until="domcontentloaded", timeout=45000)
                        page.wait_for_timeout(3000)
                        html = page.content()
                    self._dump(page, "leads-" + _slug(url))
                    got = parse_leads_html(html)
                    for r in got:
                        r.dest_country = _COUNTRY.get((r.dest_country or "").lower(),
                                                      (r.dest_country or "").upper())
                    logger.info("go4world %s -> %d leads (HTML saved to ./debug)", url, len(got))
                    leads.extend(got)
            except Exception:
                logger.exception("go4world portal fetch failed (see ./debug/error.png)")
                self._dump(page, "error")
            finally:
                ctx.close()
                browser.close()
        return leads

    def _login(self, page, ctx):
        page.goto(self.login_url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2500)
        self._dump(page, "login-page")
        user = page.locator(
            "input[placeholder='Username'], input[name='username'], input[name='email']").first
        pw = page.locator(
            "input[placeholder='Password'], input[type='password'], input[name='password']").first
        user.wait_for(state="visible", timeout=15000)
        user.fill(self.email)
        pw.fill(self.password)
        page.locator(
            "button:has-text('Sign in'), button:has-text('Log In'), input[type='submit']").first.click()
        try:
            page.wait_for_load_state("networkidle", timeout=25000)
        except Exception:
            pass
        page.wait_for_timeout(3000)
        self._dump(page, "after-login")
        try:
            ctx.storage_state(path=_STATE_FILE)              # remember the session
        except Exception:
            pass

    def _dump(self, page, name):
        try:
            with open(os.path.join(DEBUG_DIR, f"{name}.html"), "w", encoding="utf-8") as f:
                f.write(page.content())
        except Exception:
            pass
        try:
            page.screenshot(path=os.path.join(DEBUG_DIR, f"{name}.png"), full_page=True)
        except Exception:
            pass
