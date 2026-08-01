"""Tests for the website->contact enrichment helpers (pure, no network)."""
from app.enrich_service import _emails_from, _phones_from, _same_domain, clean_site, valid_email


def test_clean_site_rejects_internal_ips_and_nonhttp_schemes():
    # SSRF guard: internal/loopback/link-local IP literals + non-http(s) schemes must be refused
    for bad in ("http://127.0.0.1/x", "http://169.254.169.254/latest/meta-data/",
                "http://10.0.0.5", "http://192.168.1.1:6379", "http://[::1]/",
                "file:///etc/passwd", "gopher://internal"):
        assert clean_site(bad) == "", bad
    # a normal public domain still passes (clean_site does NO DNS for hostnames — stays offline)
    assert clean_site("acme.ge") == "http://acme.ge"


def test_same_domain_uses_dot_boundary():
    assert _same_domain("info@acme.ge", "acme.ge")            # exact
    assert _same_domain("sales@shop.acme.ge", "acme.ge")      # subdomain
    assert not _same_domain("sales@notacme.ge", "acme.ge")    # suffix overlap is NOT same-domain


def test_emails_offdomain_suffix_does_not_outrank_real():
    ranked = _emails_from("real info@acme.ge and partner sales@notacme.ge", "acme.ge")
    assert ranked[0] == "info@acme.ge"       # the genuine same-domain address wins


def test_valid_email_rejects_asset_version_strings():
    # false positives EMAIL_RE matches on real pages (Google Fonts axis, JS lib versions, minified)
    for bogus in ("magnific-popup@1.1.0", "wght@100..900", "rspack@1.6.8", "dx@h.e"):
        assert not valid_email(bogus), bogus
    for good in ("sales@acme.ge", "info@bsg.com.ge", "nodar.shukakidze@gorgia.ge"):
        assert valid_email(good), good


def test_emails_from_drops_version_string_false_positive():
    got = _emails_from("loads magnific-popup@1.1.0 then real sales@shop.ge", "shop.ge")
    assert got == ["sales@shop.ge"]


def test_clean_site_bare_host():
    assert clean_site("llcprogress.ge") == "http://llcprogress.ge"


def test_clean_site_strips_mirror_note_and_www():
    # 'peaniltd.com (shop.peaniltd.com)' -> just the first token, www dropped
    assert clean_site("peaniltd.com (shop.peaniltd.com)") == "http://peaniltd.com"
    assert clean_site("https://www.decora.ge/products") == "https://www.decora.ge"


def test_clean_site_rejects_social_and_empty():
    assert clean_site("https://facebook.com/somepage") == ""
    assert clean_site("https://www.instagram.com/x") == ""
    assert clean_site("wa.me/9715551234") == ""
    assert clean_site("") == ""
    assert clean_site("notadomain") == ""          # no dot


def test_emails_prefers_same_domain_then_role():
    host = "acme.ge"
    text = "reach personal@gmail.com or sales@acme.ge or noreply@acme.ge or info@acme.ge"
    ranked = _emails_from(text, host)
    # a same-domain role mailbox wins; the gmail (off-domain) and noreply sink below it
    assert ranked[0] == "sales@acme.ge"
    assert ranked.index("info@acme.ge") < ranked.index("personal@gmail.com")
    assert ranked[-1] == "noreply@acme.ge"


def test_emails_drop_asset_and_noise_addresses():
    text = "logo@2x.png icon.png@sentry.io real@shop.ge x@example.com"
    got = _emails_from(text, "shop.ge")
    assert "real@shop.ge" in got
    assert all("sentry" not in e and "example." not in e and ".png" not in e for e in got)


def test_phones_only_from_tel_hrefs_and_normalized():
    html = 'call <a href="tel:+995 32 2 123-456">us</a> or tel:12 (junk) tel:+9715551234567'
    phones = _phones_from(html)
    assert "+995322123456" in phones
    assert "+9715551234567" in phones
    assert "12" not in phones                        # too short -> rejected
