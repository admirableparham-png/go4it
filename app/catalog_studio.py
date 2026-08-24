"""Phase 5 — Catalog Studio: branded one-to-two-page product catalog generation (admin-only).

Provider abstraction so the generator is swappable:
  * BuiltinProvider  — a REAL, self-hosted HTML→PDF via Playwright/Chromium (installed). Degrades gracefully
    (returns an error → the caller opens a work item) if no browser is present in production.
  * HiggsProvider    — an HONEST "Not configured" stub. No invented endpoints; env-gated; it would send only
    APPROVED product fields (never contacts, credentials, buyer data, or internal margins) and store a job id.
    Reported as deferred pending real credentials/API docs.

Only APPROVED product fields + images are used; the contact is always Go4it (never a supplier or buyer). The
generated PDF is stored privately; nothing is emailed automatically.
"""
import html as _html
import json
import logging
import os

logger = logging.getLogger("go4it.catalog_studio")

HIGGS_ENABLED = os.getenv("HIGGS_ENABLED", "").lower() in ("1", "true", "yes", "on")
HIGGS_API_KEY = os.getenv("HIGGS_API_KEY", "")
DEFAULT_PROVIDER = os.getenv("CATALOG_PROVIDER", "builtin")
GEN_TIMEOUT_MS = int(os.getenv("CATALOG_GEN_TIMEOUT_MS", "20000"))


def approved_fields(product, price_version=None) -> dict:
    """The ONLY data that reaches the generator — approved public product fields + a Go4it contact. Never
    includes supplier contacts, buyer data, internal margins, internal notes, or a generation prompt."""
    data = {
        "name": product.name or "", "description": product.short_description or product.spec or "",
        "specifications": _spec_lines(product), "origin": product.origin_country or product.origin_region or "",
        "packaging": product.packaging or "", "moq": product.min_order_qty or "",
        "unit": product.unit or "", "incoterms": product.incoterms or "",
        "certifications": product.certifications or "", "brand": product.brand or "",
        "contact": "Go4it — trade@go4it.vip",   # ALWAYS Go4it; never a supplier/buyer contact
    }
    if price_version and price_version.status == "approved":
        data["indicative_price"] = f"{price_version.unit_price} {price_version.currency}/{product.unit or 'unit'} ({price_version.incoterm})"
    return data


def _spec_lines(product):
    out = []
    for label, val in (("Grade", product.grade), ("HS code", product.hs_code),
                       ("Producer", product.producer), ("Lead time",
                        f"{product.lead_time_days} days" if product.lead_time_days else ""),
                       ("Shelf life", product.shelf_life), ("Storage", product.storage_requirements)):
        if val:
            out.append((label, str(val)))
    return out


def render_html(fields: dict) -> str:
    """Deterministic branded one-pager (Go4it palette/fonts). No external assets — self-contained so it
    renders identically offline and via Playwright."""
    def e(x):
        return _html.escape(str(x or ""))
    specs = "".join(f"<tr><td class='k'>{e(k)}</td><td>{e(v)}</td></tr>" for k, v in fields.get("specifications", []))
    certs = e(fields.get("certifications", ""))
    price = fields.get("indicative_price", "")
    return f"""<!doctype html><html><head><meta charset='utf-8'><style>
      @page {{ size: A4; margin: 18mm; }}
      body {{ font-family: 'Helvetica Neue', Arial, sans-serif; color: #0f172a; }}
      .hd {{ border-bottom: 3px solid #0ea5e9; padding-bottom: 10px; margin-bottom: 16px; }}
      .brand {{ color: #0ea5e9; font-weight: 800; letter-spacing: .04em; font-size: 13px; }}
      h1 {{ font-size: 26px; margin: 6px 0 2px; }}
      .sub {{ color: #475569; font-size: 12px; }}
      .desc {{ margin: 14px 0; font-size: 13px; line-height: 1.5; }}
      table {{ width: 100%; border-collapse: collapse; font-size: 12px; margin: 8px 0; }}
      td {{ padding: 5px 4px; border-bottom: 1px solid #e2e8f0; }}
      .k {{ color: #64748b; width: 34%; }}
      .grid {{ display: flex; gap: 24px; }}
      .box {{ background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 10px 12px; font-size: 12px; }}
      .ft {{ margin-top: 22px; border-top: 1px solid #e2e8f0; padding-top: 8px; color: #64748b; font-size: 11px; }}
    </style></head><body>
      <div class='hd'><div class='brand'>GO4IT · TRADE CATALOG</div>
        <h1>{e(fields.get('name'))}</h1>
        <div class='sub'>{e(fields.get('brand'))}{' · ' if fields.get('brand') else ''}Origin: {e(fields.get('origin') or '—')}</div>
      </div>
      <div class='desc'>{e(fields.get('description'))}</div>
      <table>{specs}</table>
      <div class='grid'>
        <div class='box'>MOQ: {e(fields.get('moq') or '—')} {e(fields.get('unit'))}<br>Packaging: {e(fields.get('packaging') or '—')}<br>Incoterms: {e(fields.get('incoterms') or '—')}</div>
        <div class='box'>{('Indicative: ' + e(price) + '<br>') if price else ''}Certifications: {certs or '—'}</div>
      </div>
      <div class='ft'>Enquiries via {e(fields.get('contact'))} · Go4it mediates all supply. Prices indicative, subject to confirmation.</div>
    </body></html>"""


# --------------------------------------------------------------------- providers
class BuiltinProvider:
    name = "builtin"

    def status(self):
        try:
            import playwright  # noqa: F401
            return "ready"
        except Exception:  # noqa: BLE001
            return "unavailable (playwright not installed)"

    def generate(self, html_str, out_path, timeout_ms=GEN_TIMEOUT_MS):
        """Render HTML→PDF with headless Chromium in a SANDBOX. Returns (ok, error, provider_job_id); never
        raises. Sandbox: JavaScript OFF (neutralizes any injected <script>), ALL network egress blocked
        (only inline data:/about: may load — no file://, no remote URLs, no data exfiltration), a hard
        execution timeout, and constrained launch flags. The HTML is fully self-contained (inline CSS, no
        external assets), so blocking the network never affects a legitimate render."""
        try:
            from playwright.sync_api import sync_playwright
        except Exception as e:  # noqa: BLE001
            return False, f"playwright unavailable: {e}", ""

        def _guard(route):
            url = (route.request.url or "").lower()
            if url.startswith(("data:", "about:")):     # inline content + the blank page only
                route.continue_()
            else:                                        # http/https/file/ftp/anything → refused
                route.abort()
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(args=["--disable-extensions", "--disable-dev-shm-usage",
                                                   "--disable-gpu"])
                try:
                    context = browser.new_context(java_script_enabled=False, offline=True)
                    context.set_default_timeout(timeout_ms)
                    context.route("**/*", _guard)        # block file://, remote URLs, injected fetches
                    page = context.new_page()
                    page.set_content(html_str, wait_until="load", timeout=timeout_ms)
                    page.pdf(path=str(out_path), format="A4", print_background=True)
                    context.close()
                finally:
                    browser.close()
            return True, "", "builtin"
        except Exception as e:  # noqa: BLE001 — a render failure is reported, never raised
            logger.warning("catalog PDF generation failed", exc_info=True)
            return False, str(e)[:300], ""


class HiggsProvider:
    name = "higgs"

    def status(self):
        return "configured" if (HIGGS_ENABLED and HIGGS_API_KEY) else "not_configured"

    def generate(self, html_str, out_path, timeout_ms=GEN_TIMEOUT_MS):
        """Honest: there is no confirmed Higgs API/integration in this codebase. When real credentials + API
        docs exist, implement the HTTP call here (send only APPROVED fields; timeouts + bounded retries;
        sanitized logs; store the provider job id). Until then it refuses rather than pretend."""
        if not (HIGGS_ENABLED and HIGGS_API_KEY):
            return False, "Higgs not configured (no API key / integration)", ""
        return False, "Higgs integration not implemented — deferred pending real API documentation", ""


def get_provider(name=None):
    name = name or DEFAULT_PROVIDER
    return HiggsProvider() if name == "higgs" else BuiltinProvider()


def provider_status():
    return {"builtin": BuiltinProvider().status(), "higgs": HiggsProvider().status(),
            "default": DEFAULT_PROVIDER}


def generate_catalog(session, job, product_files_dir, price_version=None, provider=None, actor=None):
    """Orchestrate one generation: build the APPROVED-fields snapshot, render, call the provider, store the PDF
    privately, and update the job. Non-raising: on failure the job is marked 'failed' (the caller opens a work
    item). Returns the updated job."""
    from datetime import datetime
    from .models import Product
    p = session.get(Product, job.product_id)
    if not p:
        job.status = "failed"; job.error = "product not found"; session.add(job)
        return job
    fields = approved_fields(p, price_version)
    job.params = json.dumps(fields)[:8000]           # store the exact approved snapshot sent to the generator
    job.status = "generating"; session.add(job)
    prov = provider or get_provider(job.provider)
    job.provider = prov.name
    out_dir = product_files_dir / str(p.id)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"catalog_{job.id}.pdf"
    ok, err, pjid = prov.generate(render_html(fields), out_path)
    if ok:
        job.status = "needs_review"; job.file_path = f"{p.id}/catalog_{job.id}.pdf"
        job.provider_job_id = pjid; job.generated_at = datetime.utcnow()
        job.generated_by = getattr(actor, "email", "") or ""
    else:
        job.status = "failed"; job.error = err[:400]
    session.add(job)
    return job
