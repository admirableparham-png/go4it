"""go4worldbusiness authenticated portal scraper (browser bot).

Logs into the operator's OWN paid account with Playwright, visits the configured
buy-lead listing pages, and extracts buy-leads into RawLeads. Runs gently — a
single browser, sequential pages, human-like pauses — on an hourly cadence via
the worker.

⚠️ Automated access likely conflicts with go4worldbusiness's Terms and can risk
the account; enabled only when GO4WORLD_EMAIL/PASSWORD are set (operator's choice).

Two-step bring-up: the login-form selectors and the lead-row selectors below are
best-effort. On every run this saves the page HTML + a screenshot to ./debug so
the exact selectors can be tuned to the real logged-in DOM. Until tuned,
parse_leads_html returns [] rather than guessing wrong.
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

# Best-effort selectors — refined after the first authenticated capture.
_LOGIN_EMAIL_SELECTORS = ("input[type=email]", "input[name=email]", "#email",
                          "input[name=username]", "input[name=login]")
_LOGIN_PASSWORD_SELECTORS = ("input[type=password]", "input[name=password]", "#password")
_LOGIN_SUBMIT_SELECTORS = ("button[type=submit]", "input[type=submit]",
                           "button:has-text('Login')", "button:has-text('Sign In')",
                           "button:has-text('Log In')")


def _slug(url: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", url.lower()).strip("-")[-48:]


def parse_leads_html(html: str):
    """Parse a go4worldbusiness buy-leads listing page into RawLeads.

    PLACEHOLDER: returns [] until the selectors are tuned to the real logged-in
    DOM (captured to ./debug on the first run). Replacing the body here is the
    only change needed to go live.
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

    def fetch(self):
        if not (self.email and self.password):
            logger.info("go4world portal disabled (no GO4WORLD_EMAIL/PASSWORD)")
            return []

        from playwright.sync_api import sync_playwright

        os.makedirs(DEBUG_DIR, exist_ok=True)
        leads = []
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.headless)
            ctx = browser.new_context(
                user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/126.0.0.0 Safari/537.36"),
                locale="en-US", viewport={"width": 1440, "height": 900})
            page = ctx.new_page()
            try:
                self._login(page)
                for url in self.lead_urls:
                    time.sleep(random.uniform(2.5, 5.0))     # be gentle between pages
                    page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    page.wait_for_timeout(3000)
                    html = page.content()
                    stamp = _slug(url)
                    with open(os.path.join(DEBUG_DIR, f"leads-{stamp}.html"), "w", encoding="utf-8") as f:
                        f.write(html)
                    try:
                        page.screenshot(path=os.path.join(DEBUG_DIR, f"leads-{stamp}.png"), full_page=True)
                    except Exception:
                        pass
                    got = parse_leads_html(html)
                    for r in got:
                        r.dest_country = _COUNTRY.get((r.dest_country or "").lower(), (r.dest_country or "").upper())
                    logger.info("go4world %s -> %d leads (HTML saved to ./debug)", url, len(got))
                    leads.extend(got)
            except Exception:
                logger.exception("go4world portal fetch failed (screenshot in ./debug/error.png)")
                try:
                    page.screenshot(path=os.path.join(DEBUG_DIR, "error.png"), full_page=True)
                except Exception:
                    pass
            finally:
                ctx.close()
                browser.close()
        return leads

    def _login(self, page):
        page.goto(self.login_url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2000)
        for sel in _LOGIN_EMAIL_SELECTORS:
            if page.query_selector(sel):
                page.fill(sel, self.email)
                break
        for sel in _LOGIN_PASSWORD_SELECTORS:
            if page.query_selector(sel):
                page.fill(sel, self.password)
                break
        for sel in _LOGIN_SUBMIT_SELECTORS:
            if page.query_selector(sel):
                page.click(sel)
                break
        page.wait_for_timeout(4000)
        try:
            page.screenshot(path=os.path.join(DEBUG_DIR, "after-login.png"), full_page=True)
        except Exception:
            pass
