"""Phase 4 hardening — production canary tooling (founder-only, allowlist-gated).

The isolated mechanism checks (suppression blocks, pause-all stops, sellers see nothing) actually pass; the
live path is refused unless CANARY_ENABLED + an allowlist + a live mailbox are configured, and is NEVER
simulated as a success. Recipients are restricted to the allowlist.
"""
import scripts.canary as CAN


def test_mechanism_checks_pass_in_isolation():
    for fn in (CAN.check_suppression_blocks, CAN.check_pause_all_stops_worker, CAN.check_sellers_see_nothing):
        ok, detail = fn()
        assert ok, f"{fn.__name__} failed: {detail}"


def test_live_preflight_not_ready_without_config():
    ready, reasons = CAN.live_preflight()
    assert ready is False and reasons                     # gated: refuses without explicit config


def test_allowlist_restricts_recipients(monkeypatch):
    monkeypatch.setattr(CAN, "CANARY_ALLOWLIST", ["ok@internal.test"])
    assert CAN.allowlist_ok("ok@internal.test") is True
    assert CAN.allowlist_ok("stranger@buyer.com") is False
    assert CAN.allowlist_ok("") is False


def test_live_preflight_requires_recipient_inside_allowlist(monkeypatch):
    monkeypatch.setattr(CAN, "CANARY_ENABLED", True)
    monkeypatch.setattr(CAN, "CANARY_ALLOWLIST", ["ok@internal.test"])
    monkeypatch.setattr(CAN, "CANARY_TEST_RECIPIENT", "outside@buyer.com")   # not in allowlist
    ready, reasons = CAN.live_preflight()
    assert ready is False
    assert any("allowlist" in r.lower() for r in reasons)


def test_report_runs_and_reports_not_run(capsys):
    rc = CAN.main_report()
    out = capsys.readouterr().out
    assert rc == 0                                        # mechanism checks pass
    assert "NOT RUN" in out and "not simulated" in out.lower()
