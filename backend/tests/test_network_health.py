"""Week 2 Phase D, roadmap section 11 -- host-IP change detection,
reconnection after a connectivity loss, router-reboot resilience for
discovery, per-bulb latency history, and LAN-vs-Tailscale degradation.

Every network fact here comes from an injected probe. Nothing in this file
may depend on whether the machine running the suite actually has a LAN, a
tailnet, or an internet connection -- a test that goes red because someone
unplugged their ethernet is a test that gets ignored.
"""

import time

import pytest

import bulb_manager as bm
import discovery
import network_health


# ------------------------------------------------------ IP classification -
@pytest.mark.parametrize("ip,expected", [
    ("127.0.0.1", "loopback"),
    ("169.254.10.1", "link_local"),
    ("192.168.1.50", "private"),
    ("10.0.0.11", "private"),
    ("172.16.4.2", "private"),
    # Tailscale's CGNAT range: Python's own is_private says False for it,
    # so without the explicit check a tailnet peer would look like a public
    # client and trip the exposure warning.
    ("100.101.102.103", "tailscale"),
    ("8.8.8.8", "public"),
    ("93.184.216.34", "public"),
    # 203.0.113.0/24 is TEST-NET-3 (reserved for documentation). Python's
    # ipaddress folds every reserved-and-not-globally-routable range into
    # is_private, so it lands in "private" rather than "public" -- which is
    # the answer that matters here, since the only consequential question
    # this classifier answers is "did this request come from the open
    # internet". Asserted so nobody re-bases the classifier on is_global
    # alone and quietly reclassifies it as public.
    ("203.0.113.7", "private"),
    ("not-an-ip", "unknown"),
    (None, "unknown"),
])
def test_classify_ip(ip, expected):
    assert network_health.classify_ip(ip) == expected


# --------------------------------------------------- connectivity modes ---
def test_connectivity_full_when_lan_and_tailscale_both_present():
    summary = network_health.connectivity_summary(["192.168.1.50", "100.101.102.103"])
    assert summary["mode"] == "full"
    assert summary["lan"] is True
    assert summary["tailscale"] is True
    assert summary["bulb_control_available"] is True


def test_connectivity_lan_only_when_tailscale_is_down():
    summary = network_health.connectivity_summary(["192.168.1.50"])
    assert summary["mode"] == "lan_only"
    assert summary["bulb_control_available"] is True
    assert "tailnet remote access does not" in summary["message"]


def test_connectivity_tailscale_only_says_bulb_control_cannot_work():
    # The important half of "graceful behaviour when the LAN is down but
    # Tailscale is up": the dashboard is reachable, so a user WILL open it,
    # and it has to say plainly that bulb commands can't work rather than
    # letting each one time out with its own opaque error.
    summary = network_health.connectivity_summary(["100.101.102.103"])
    assert summary["mode"] == "tailscale_only"
    assert summary["lan"] is False
    assert summary["bulb_control_available"] is False
    assert "cannot reach any bulb" in summary["message"]


def test_connectivity_offline_with_only_loopback():
    summary = network_health.connectivity_summary(["127.0.0.1"])
    assert summary["mode"] == "offline"
    assert summary["bulb_control_available"] is False


# ------------------------------------------------------------- poll() -----
def test_poll_records_the_first_ip_without_calling_it_a_change():
    result = network_health.poll(ip_probe=lambda: "192.168.1.50", reconnect_hook=lambda: None)
    assert result["ip"] == "192.168.1.50"
    assert result["ip_changed"] is False
    assert network_health.get_state()["current_ip"] == "192.168.1.50"
    assert network_health.get_state()["changes"] == []


def test_poll_logs_a_host_ip_change():
    calls = []
    network_health.poll(ip_probe=lambda: "192.168.1.50", reconnect_hook=lambda: None)
    result = network_health.poll(ip_probe=lambda: "192.168.4.77",
                                  reconnect_hook=lambda: calls.append("reset"))

    assert result["ip_changed"] is True
    assert result["previous_ip"] == "192.168.1.50"
    state = network_health.get_state()
    assert state["current_ip"] == "192.168.4.77"
    assert state["changes"][0]["old_ip"] == "192.168.1.50"
    assert state["changes"][0]["new_ip"] == "192.168.4.77"
    # A new IP usually means a new router/subnet, so cached bulb sockets
    # are stale too -- the reconnect hook fires here as well.
    assert calls == ["reset"]


def test_poll_marks_the_host_down_when_there_is_no_route():
    result = network_health.poll(ip_probe=lambda: None, reconnect_hook=lambda: None)
    assert result["connectivity"] == "down"
    assert network_health.get_state()["down_since"] is not None


def test_poll_reconnects_when_connectivity_comes_back():
    calls = []
    network_health.poll(ip_probe=lambda: "192.168.1.50", reconnect_hook=lambda: None, now=1000.0)
    network_health.poll(ip_probe=lambda: None, reconnect_hook=lambda: None, now=1010.0)
    result = network_health.poll(ip_probe=lambda: "192.168.1.50",
                                  reconnect_hook=lambda: calls.append("reset"), now=1042.0)

    assert result["regained"] is True
    assert result["down_seconds"] == 32.0
    assert result["reconnected"] is True
    assert calls == ["reset"]
    state = network_health.get_state()
    assert state["down_since"] is None
    assert state["reconnects"][0]["down_seconds"] == 32.0


def test_poll_survives_a_probe_that_raises():
    result = network_health.poll(ip_probe=lambda: 1 / 0, reconnect_hook=lambda: None)
    assert result["ip"] is None
    assert result["connectivity"] == "down"


def test_poll_survives_a_reconnect_hook_that_raises():
    def broken_hook():
        raise RuntimeError("bulb layer exploded")

    network_health.poll(ip_probe=lambda: "192.168.1.50", reconnect_hook=lambda: None)
    result = network_health.poll(ip_probe=lambda: "192.168.9.9", reconnect_hook=broken_hook)
    assert result["ip_changed"] is True
    assert result["reconnected"] is False


def test_change_log_is_bounded(monkeypatch):
    monkeypatch.setattr(network_health, "MAX_CHANGE_LOG", 3)
    for i in range(10):
        network_health.poll(ip_probe=lambda i=i: f"192.168.1.{i}", reconnect_hook=lambda: None)
    assert len(network_health.get_state()["changes"]) == 3


# ------------------------------------------- reconnection at the bulb layer
def test_reset_all_connections_drops_cached_sockets_but_keeps_history(fake_config, fake_tuya):
    controller = bm.get_controller("bulb-1")
    controller.status()  # forces a device handle to exist
    controller._log("power", {"on": True})
    assert controller._dev is not None

    count = bm.reset_all_connections()

    assert count == 1
    assert controller._dev is None
    # The controller itself survives -- history, timers and effect state are
    # not collateral damage of a router reboot.
    assert bm.get_controller("bulb-1") is controller
    assert len(controller.history()) >= 1


def test_default_reconnect_hook_resets_real_controllers(fake_config, fake_tuya):
    controller = bm.get_controller("bulb-1")
    controller.status()
    assert controller._dev is not None
    network_health._default_reconnect_hook()
    assert controller._dev is None


# ------------------------------------------------- per-bulb latency -------
def test_status_calls_feed_the_latency_history(fake_config, fake_tuya):
    controller = bm.get_controller("bulb-1")
    controller.status()
    controller.status()

    history = network_health.latency_history("bulb-1")
    assert history["sample_count"] == 2
    assert history["failure_count"] == 0
    assert history["avg_ms"] is not None
    assert history["samples"][0]["ok"] is True


def test_failed_status_is_recorded_as_a_latency_failure(fake_config, fake_tuya, monkeypatch):
    controller = bm.get_controller("bulb-1")

    def explode():
        raise OSError("host unreachable")

    monkeypatch.setattr(controller, "_get_device", explode)
    result = controller.status()
    assert result["online"] is False

    history = network_health.latency_history("bulb-1")
    assert history["sample_count"] == 1
    assert history["failure_count"] == 1
    assert history["failure_rate"] == 1.0
    # A failed round trip has no meaningful latency, so it must not drag
    # the percentile stats around.
    assert history["avg_ms"] is None


def test_latency_history_computes_real_percentiles():
    for value in range(1, 101):
        network_health.record_latency("bulb-1", value, ok=True)
    history = network_health.latency_history("bulb-1")
    assert history["min_ms"] == 1
    assert history["max_ms"] == 100
    assert history["p50_ms"] == 50
    assert history["p95_ms"] == 95


def test_latency_window_is_bounded(monkeypatch):
    monkeypatch.setattr(network_health, "LATENCY_SAMPLES", 5)
    for value in range(20):
        network_health.record_latency("bulb-1", value)
    assert network_health.latency_history("bulb-1")["sample_count"] == 5


def test_all_latency_summaries_omits_raw_samples():
    network_health.record_latency("bulb-1", 12.0)
    network_health.record_latency("bulb-2", 40.0)
    summaries = network_health.all_latency_summaries()
    assert {s["device_id"] for s in summaries} == {"bulb-1", "bulb-2"}
    assert all("samples" not in s for s in summaries)


# ------------------------------------------- router-reboot resilience -----
def test_scan_retries_a_failure_before_giving_up(monkeypatch, fake_config):
    attempts = {"n": 0}
    slept = []

    def flaky_scan(maxretry=2):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise OSError("network is unreachable")
        return {}

    monkeypatch.setattr(discovery, "_raw_scan", flaky_scan)
    monkeypatch.setattr(discovery, "_load", lambda: discovery._default_state())
    monkeypatch.setattr(discovery, "_save", lambda state: None)

    result = discovery.scan_now(retries=2, retry_delay_s=1.0, sleeper=slept.append)

    assert result["ok"] is True
    assert result["attempts"] == 3
    # Widening gap, and nothing actually slept.
    assert slept == [1.0, 2.0]


def test_scan_reports_failure_after_exhausting_retries(monkeypatch):
    def always_fails(maxretry=2):
        raise OSError("network is unreachable")

    monkeypatch.setattr(discovery, "_raw_scan", always_fails)
    result = discovery.scan_now(retries=1, retry_delay_s=0.0, sleeper=lambda s: None)
    assert result["ok"] is False
    assert result["attempts"] == 2
    assert "unreachable" in result["error"]


def test_scheduler_skips_scanning_when_there_is_no_lan():
    state = {"last_scan": None, "interval_hours": 168}
    connectivity = network_health.connectivity_summary(["100.101.102.103"])
    decision = discovery.should_scan(state, connectivity)
    # Running a broadcast scan with no LAN can't find anything AND would
    # overwrite last_scan, pushing the next real chance a week out.
    assert decision["scan"] is False
    assert decision["reason"] == "no_lan"


def test_scheduler_scans_immediately_after_a_host_ip_change():
    state = {"last_scan": "2999-01-01T00:00:00Z", "interval_hours": 168}  # nowhere near due
    connectivity = network_health.connectivity_summary(["192.168.1.50"])
    decision = discovery.should_scan(state, connectivity,
                                      network_poll={"ip_changed": True,
                                                    "previous_ip": "192.168.1.50",
                                                    "ip": "192.168.9.9"})
    assert decision["scan"] is True
    assert decision["reason"] == "host_ip_changed"


def test_scheduler_respects_the_interval_when_nothing_changed():
    from datetime import datetime, timedelta, timezone
    recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    connectivity = network_health.connectivity_summary(["192.168.1.50"])

    not_due = discovery.should_scan({"last_scan": recent, "interval_hours": 168}, connectivity)
    assert not_due["scan"] is False
    assert not_due["reason"] == "not_due"

    due = discovery.should_scan({"last_scan": recent, "interval_hours": 0}, connectivity)
    assert due["scan"] is True
    assert due["reason"] == "interval_elapsed"


def test_scan_due_treats_a_corrupt_timestamp_as_due():
    assert discovery.scan_due({"last_scan": "not-a-date", "interval_hours": 168}) is True


# ---------------------------------------------------------- host probes ---
def test_primary_ip_returns_none_when_there_is_no_route(monkeypatch):
    class DeadSocket:
        def settimeout(self, _):
            pass

        def connect(self, _):
            raise OSError("network is unreachable")

        def getsockname(self):
            raise AssertionError("should not be reached")

        def close(self):
            pass

    monkeypatch.setattr(network_health.socket, "socket", lambda *a, **k: DeadSocket())
    assert network_health.primary_ip() is None


def test_start_monitor_is_idempotent(monkeypatch):
    monkeypatch.setattr(network_health, "_monitor_thread", None)
    monkeypatch.setattr(network_health, "poll", lambda: None)
    first = network_health.start_monitor(interval_s=3600)
    second = network_health.start_monitor(interval_s=3600)
    assert first is second
    # Give the loop a moment to prove it isn't spinning; it should be
    # sitting in its sleep.
    time.sleep(0.05)
    assert first.is_alive()
