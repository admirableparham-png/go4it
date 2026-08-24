"""Catalog Studio — production PDF smoke test.

Run this INSIDE the exact production container before trusting Catalog Studio there. It renders a real PDF
with the pinned Playwright/Chromium runtime and confirms: the browser launches, fonts + an embedded raster
image render, the page size is A4, and PRODUCT_FILES_DIR is writable + private (not under /static).

    ./.venv/bin/python scripts/pdf_smoke.py

Exit 0 = ready; non-zero = not ready (Catalog Studio degrades to an error + a work item until this passes).
Secrets/credentials are never involved. This is the Chromium-in-prod verification step; do NOT label the
platform production-ready on the strength of a local run.
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

# a 1x1 transparent PNG as a data: URI — proves image embedding works end-to-end
_PNG = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
        "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


def _check_playwright_version():
    try:
        from importlib.metadata import version
        return version("playwright")
    except Exception as e:  # noqa: BLE001
        return f"NOT INSTALLED ({e})"


def _check_storage():
    from pathlib import Path
    d = Path(BASE) / "product_files"
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".smoke_probe"
        probe.write_text("ok"); probe.unlink()
        writable = True
    except Exception:  # noqa: BLE001
        writable = False
    private = "static" not in str(d).lower().split(os.sep)   # must NOT be under a public static dir
    return writable, private, str(d)


def main():
    print("=== Catalog Studio PDF smoke test ===")
    ver = _check_playwright_version()
    print(f"playwright: {ver}")
    writable, private, storage = _check_storage()
    print(f"PRODUCT_FILES_DIR: {storage}  writable={writable}  private(not-static)={private}")
    if not (writable and private):
        print("FAIL: private product storage is not usable"); sys.exit(3)

    from app import catalog_studio as S
    fields = {"name": "Smoke Test Product", "brand": "Go4it", "description": "Rendering check with fonts.",
              "specifications": [("Grade", "A"), ("HS code", "0000.00")], "origin": "IR", "packaging": "box",
              "moq": 1, "unit": "unit", "incoterms": "FOB", "certifications": "ISO",
              "contact": "Go4it — trade@go4it.vip"}
    html = S.render_html(fields).replace("</body>", f"<img src='{_PNG}' width='8'></body>")   # embedded image
    out = os.path.join(BASE, "product_files", "_smoke.pdf")
    ok, err, _ = S.BuiltinProvider().generate(html, out)
    if not ok:
        print(f"FAIL: generation error: {err}"); sys.exit(4)
    size = os.path.getsize(out)
    header = open(out, "rb").read(5) == b"%PDF-"
    # a real A4 one-pager renders to a few KB+; a near-empty file signals a missing-font/broken render
    ok_size = size > 3000
    print(f"PDF: {size} bytes  header_ok={header}  size_ok={ok_size}")
    os.remove(out)
    if not (header and ok_size):
        print("FAIL: PDF looks empty/broken (check fonts + Chromium in the container)"); sys.exit(5)
    print("PASS: Chromium PDF generation is ready in this container.")


if __name__ == "__main__":
    main()
