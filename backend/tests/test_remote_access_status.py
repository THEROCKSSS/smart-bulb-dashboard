"""Week 2 Phase D, roadmap sections 1 and 2 -- public-IP detection, the
DuckDNS last-sync readout, the Tailscale status check, and the exposure
warning banner (including the persistent fail-safe for "the PIN gate got
turned off later").

Both external dependencies are injected: the public-IP fetch takes a
`fetcher` and the Tailscale check takes a `runner`. No test here may
depend on this machine having internet access or Tailscale installed --
including the "it isn't installed" path, which is simulated rather than
observed, because a machine that DOES have Tailscale would otherwise
silently skip it.
"""

import json
import subprocess

import pytest

import remote_access_status as ras


TAILSCALE_RUNNING_JSON = json.dumps({
    "BackendState": "Running",
    "Self": {
        "DNSName": "desk.tailnet-abc.ts.net.",
        "TailscaleIPs": ["100.101.102.103"],
    },
    "Peer": {"nodekey:1": {}, "nodekey:2": {}},
})


class FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ------------------------------------------------------------ public IP ---
def test_detect_public_ip_records_a_successful_lookup():
    result = ras.detect_public_ip(fetcher=lambda url, timeout: "203.0.113.9\n")
    assert result["public_ip"] == "203.0.113.9"
    assert result["error"] is None
    assert ras.get_state()["public_ip"] == "203.0.113.9"
    assert ras.get_state()["public_ip_checked_at"] is not None


def test_detect_public_ip_rejects_a_non_ip_response():
    # A captive portal / error page returning HTML must not be recorded as
    # "your public IP is <!DOCTYPE html>".
    result = ras.detect_public_ip(fetcher=lambda url, timeout: "<html>blocked</html>")
    assert result["public_ip"] is None
    assert "ValueError" in result["error"]


def test_detect_public_ip_degrades_gracefully_when_offline():
    def offline(url, timeout):
        raise OSError("getaddrinfo failed")

    result = ras.detect_public_ip(fetcher=offline)
    assert result["public_ip"] is None
    assert "OSError" in result["error"]
    # And it must not take anything else down with it.
    assert ras.status()["public_ip"]["error"] is not None


def test_status_never_performs_the_public_ip_lookup_itself():
    # The no-telemetry promise in SECURITY.md only holds if the outbound
    # lookup has exactly one caller. Loading the status panel is not it.
    def must_not_be_called(url, timeout):
        raise AssertionError("status() performed an outbound request")

    original = ras._default_fetcher
    ras._default_fetcher = must_not_be_called
    try:
        result = ras.status()
    finally:
        ras._default_fetcher = original
    assert result["public_ip"]["ip"] is None


# -------------------------------------------------------------- DuckDNS ---
def test_record_duckdns_sync_surfaces_domain_and_time():
    ras.record_duckdns_sync("myhouse.duckdns.org", "203.0.113.9", ok=True)
    duckdns = ras.status()["duckdns"]
    assert duckdns["domain"] == "myhouse.duckdns.org"
    assert duckdns["last_sync_ip"] == "203.0.113.9"
    assert duckdns["last_sync_ok"] is True
    assert duckdns["last_sync_at"] is not None


def test_a_successful_duckdns_sync_arms_the_exposure_flag():
    assert ras.get_state()["exposure_configured"] is False
    ras.record_duckdns_sync("myhouse.duckdns.org", "203.0.113.9", ok=True)
    state = ras.get_state()
    assert state["exposure_configured"] is True
    assert state["exposure_source"] == "duckdns:myhouse.duckdns.org"


def test_a_failed_duckdns_sync_does_not_arm_exposure_but_does_warn():
    ras.mark_exposure(True, source="manual")
    ras.record_duckdns_sync("myhouse.duckdns.org", None, ok=False, detail="401 from duckdns")
    ids = [w["id"] for w in ras.exposure_warnings(pin_gate_enabled=True)]
    assert "duckdns_sync_failing" in ids


# ------------------------------------------------------------ Tailscale ---
def test_tailscale_status_reports_a_running_daemon_and_the_tailnet_url():
    result = ras.tailscale_status(
        runner=lambda args, timeout: FakeCompleted(0, TAILSCALE_RUNNING_JSON), port=8500)
    assert result["installed"] is True
    assert result["running"] is True
    assert result["magic_dns_name"] == "desk.tailnet-abc.ts.net"
    assert result["tailscale_ips"] == ["100.101.102.103"]
    assert result["tailnet_url"] == "http://desk.tailnet-abc.ts.net:8500"
    assert result["peer_count"] == 2
    assert result["error"] is None


def test_tailscale_status_falls_back_to_the_ip_when_magicdns_is_off():
    payload = json.dumps({"BackendState": "Running",
                          "Self": {"TailscaleIPs": ["100.64.5.6"]}, "Peer": {}})
    result = ras.tailscale_status(runner=lambda args, timeout: FakeCompleted(0, payload), port=8502)
    assert result["tailnet_url"] == "http://100.64.5.6:8502"


def test_tailscale_status_when_the_cli_is_not_installed():
    def missing(args, timeout):
        raise FileNotFoundError("tailscale")

    result = ras.tailscale_status(runner=missing)
    assert result["installed"] is False
    assert result["running"] is False
    assert "not installed" in result["error"]
    assert result["tailnet_url"] is None


def test_tailscale_status_when_the_daemon_is_stopped():
    payload = json.dumps({"BackendState": "Stopped", "Self": {}, "Peer": {}})
    result = ras.tailscale_status(runner=lambda args, timeout: FakeCompleted(0, payload))
    assert result["installed"] is True
    assert result["running"] is False
    assert result["backend_state"] == "Stopped"
    assert "tailscale up" in result["error"]
    assert result["tailnet_url"] is None


def test_tailscale_status_when_the_cli_times_out():
    def slow(args, timeout):
        raise subprocess.TimeoutExpired(cmd="tailscale", timeout=timeout)

    result = ras.tailscale_status(slow, timeout_s=1.0)
    assert result["installed"] is True
    assert result["running"] is False
    assert "timed out" in result["error"]


def test_tailscale_status_survives_unparseable_output():
    result = ras.tailscale_status(runner=lambda args, timeout: FakeCompleted(0, "not json"))
    assert result["running"] is False
    assert "could not parse" in result["error"]


def test_tailscale_status_reports_a_nonzero_exit():
    result = ras.tailscale_status(
        runner=lambda args, timeout: FakeCompleted(1, "", "needs login"))
    assert result["running"] is False
    assert result["error"] == "needs login"


# --------------------------------------------------- public client notice -
def test_note_client_ip_ignores_lan_and_tailnet_sources():
    assert ras.note_client_ip("192.168.1.20") is None
    assert ras.note_client_ip("100.101.102.103") is None
    assert ras.note_client_ip("127.0.0.1") is None
    assert ras.get_state()["public_client_seen_at"] is None


def test_note_client_ip_records_the_first_public_source_only():
    ras.note_client_ip("8.8.8.8")
    first_seen = ras.get_state()["public_client_seen_at"]
    assert first_seen is not None
    assert ras.get_state()["public_client_ip"] == "8.8.8.8"

    ras.note_client_ip("9.9.9.9")
    assert ras.get_state()["public_client_ip"] == "8.8.8.8"
    assert ras.get_state()["public_client_seen_at"] == first_seen


# ------------------------------------------------------------- warnings ---
def test_no_warnings_on_a_plain_lan_only_install():
    assert ras.exposure_warnings(pin_gate_enabled=False) == []


def test_public_client_with_the_gate_off_is_a_critical_warning():
    ras.note_client_ip("8.8.8.8")
    warnings = ras.exposure_warnings(pin_gate_enabled=False)
    warning = next(w for w in warnings if w["id"] == "public_client_observed")
    assert warning["severity"] == "critical"
    assert "8.8.8.8" in warning["detail"]


def test_public_client_with_the_gate_on_produces_no_warning():
    ras.note_client_ip("8.8.8.8")
    assert ras.exposure_warnings(pin_gate_enabled=True) == []


def test_failsafe_warning_returns_when_the_gate_is_disabled_after_exposure():
    # The specific failure this guards: exposure gets set up WITH the gate
    # on (no warning, correctly), and the gate is turned off some time
    # later. A dismiss-once banner would never fire again; this one does.
    ras.record_duckdns_sync("myhouse.duckdns.org", "203.0.113.9", ok=True)
    assert ras.exposure_warnings(pin_gate_enabled=True) == []

    ids = [w["id"] for w in ras.exposure_warnings(pin_gate_enabled=False)]
    assert "exposure_configured_gate_disabled" in ids


def test_failsafe_warning_persists_across_a_restart():
    ras.record_duckdns_sync("myhouse.duckdns.org", "203.0.113.9", ok=True)
    # Simulate a restart: nothing in memory, state re-read from disk.
    reloaded = ras.get_state()
    ids = [w["id"] for w in ras.exposure_warnings(reloaded, pin_gate_enabled=False)]
    assert "exposure_configured_gate_disabled" in ids


def test_retracting_exposure_clears_the_warning_and_the_observed_evidence():
    ras.record_duckdns_sync("myhouse.duckdns.org", "203.0.113.9", ok=True)
    ras.note_client_ip("8.8.8.8")
    assert ras.exposure_warnings(pin_gate_enabled=False)

    ras.mark_exposure(False)

    assert ras.exposure_warnings(pin_gate_enabled=False) == []
    assert ras.get_state()["public_client_seen_at"] is None


def test_status_bundles_every_panel_the_ui_needs():
    result = ras.status()
    for section in ("public_ip", "duckdns", "exposure", "tailscale", "warnings"):
        assert section in result
    assert result["tailscale"]["checked"] is False  # not probed unless asked


def test_status_can_probe_tailscale_on_request():
    result = ras.status(include_live_lookups=True,
                        runner=lambda args, timeout: FakeCompleted(0, TAILSCALE_RUNNING_JSON))
    assert result["tailscale"]["running"] is True


def test_exposure_warnings_default_to_reading_the_real_pin_gate(auth_reset):
    import remote_auth
    ras.record_duckdns_sync("myhouse.duckdns.org", "203.0.113.9", ok=True)

    remote_auth.enable("87654321")
    assert ras.exposure_warnings() == []

    remote_auth.disable()
    ids = [w["id"] for w in ras.exposure_warnings()]
    assert "exposure_configured_gate_disabled" in ids
