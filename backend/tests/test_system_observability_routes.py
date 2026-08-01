"""HTTP round-trips for the Week 2 Phase D system routes: /metrics, the
health summary, the log viewer and level control, the diagnostic report,
network state, per-bulb latency history and the remote-access panels.

These go through the real app (and therefore the real middleware), which
is the point -- the request-timing/correlation-id middleware is only worth
having if it actually fires on live traffic, so several of these assert on
what a request *did to* the metrics rather than on a handler's return value.
"""

import json

import pytest

import network_health
import observability
import remote_access_status as ras


# ------------------------------------------------------------- /metrics ---
def test_metrics_endpoint_returns_prometheus_text(client):
    client.get("/api/system/health")
    resp = client.get("/metrics")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    body = resp.text
    assert "# TYPE sbd_requests_total counter" in body
    assert 'sbd_build_info{version=' in body
    assert 'sbd_requests_total{method="GET",endpoint="/api/system/health"} 1' in body


def test_middleware_records_real_traffic_against_route_templates(client):
    client.post("/api/devices/bulb-1/power", json={"on": True})
    client.post("/api/devices/bulb-2/power", json={"on": True})

    snapshot = client.get("/api/system/metrics").json()
    power = next(e for e in snapshot["endpoints"]
                 if e["endpoint"] == "/api/devices/{device_id}/power")
    # Both bulbs collapse onto one series -- one series per real device id
    # would make percentiles useless and the cardinality unbounded.
    assert power["requests"] == 2
    assert power["p50_ms"] is not None


def test_middleware_counts_a_404_as_a_client_error_not_a_server_error(client):
    client.get("/api/devices/nope/status")
    totals = client.get("/api/system/metrics").json()["totals"]
    assert totals["client_errors"] >= 1
    assert totals["errors"] == 0


def test_correlation_id_header_is_echoed_and_generated(client):
    generated = client.get("/api/system/health")
    assert len(generated.headers[observability.CORRELATION_HEADER]) == 16

    echoed = client.get("/api/system/health",
                        headers={observability.CORRELATION_HEADER: "trace-abc-1"})
    assert echoed.headers[observability.CORRELATION_HEADER] == "trace-abc-1"


def test_correlation_id_from_a_hostile_header_is_replaced(client):
    resp = client.get("/api/system/health",
                      headers={observability.CORRELATION_HEADER: "a b c;drop"})
    assert resp.headers[observability.CORRELATION_HEADER] != "a b c;drop"


# ------------------------------------------------------ health summary ----
def test_health_summary_is_distinct_from_per_device_diagnostics(client):
    body = client.get("/api/system/health-summary").json()

    # Backend-level facts, not "is this one bulb reachable".
    assert body["process"]["version"]
    assert body["process"]["python"]
    assert body["dependencies"]["ok"] is True
    assert "mode" in body["network"]
    assert isinstance(body["problems"], list)
    assert isinstance(body["endpoints"], list)


def test_health_summary_reports_a_degraded_optional_dependency(client, monkeypatch):
    def broken(*a, **k):
        return {"data_source": "LIVE DATA", "ok": True, "degraded": ["sounddevice"],
                "checks": [{"name": "sounddevice", "required": False, "ok": False,
                            "detail": "PortAudio not found", "why": "audio capture"}]}

    monkeypatch.setattr(observability, "dependency_summary", broken)
    body = client.get("/api/system/health-summary").json()
    assert body["healthy"] is False
    assert any("sounddevice" in p for p in body["problems"])


def test_health_summary_flags_no_lan(client, monkeypatch):
    monkeypatch.setattr(network_health, "list_host_ips", lambda: ["100.101.102.103"])
    body = client.get("/api/system/health-summary").json()
    assert body["network"]["mode"] == "tailscale_only"
    assert any("bulb control" in p for p in body["problems"])


def test_dependencies_endpoint(client):
    body = client.get("/api/system/dependencies").json()
    names = {c["name"] for c in body["checks"]}
    assert {"tinytuya", "numpy", "sounddevice"} <= names


# -------------------------------------------------------- log viewer ------
def test_log_level_get_and_set_round_trip(client):
    assert client.get("/api/system/log-level").json()["log_level"] == "INFO"

    resp = client.post("/api/system/log-level", json={"level": "DEBUG"})
    assert resp.status_code == 200
    assert client.get("/api/system/log-level").json()["log_level"] == "DEBUG"


def test_setting_an_unknown_log_level_is_a_400(client):
    resp = client.post("/api/system/log-level", json={"level": "verbose"})
    assert resp.status_code == 400


def test_log_viewer_returns_recent_entries(client):
    observability.set_log_level("INFO")
    observability.get_logger().warning("a thing worth seeing")

    body = client.get("/api/system/logs", params={"limit": 10}).json()
    assert body["entries"][0]["message"] == "a thing worth seeing"
    assert body["log_level"] == "INFO"


def test_log_viewer_rejects_an_unknown_level_filter(client):
    assert client.get("/api/system/logs", params={"level": "loud"}).status_code == 400


# --------------------------------------------------- diagnostic report ----
def test_diagnostic_report_endpoint_is_redacted(client, fake_config):
    secret = "zXcVbNmAsDfGhJkL"
    fake_config["devices"][0]["local_key"] = secret
    observability.get_logger().warning("handshake failed for local_key=%s", secret)

    resp = client.get("/api/system/diagnostic-report")
    assert resp.status_code == 200
    assert secret not in resp.text
    assert observability.REDACTED in resp.text


def test_diagnostic_report_writes_nothing_to_disk(client):
    # A support bundle that quietly drops a file next to the code is
    # exactly how a local_key ends up in a commit. It is returned to the
    # caller and nowhere else.
    import os

    before = set(os.listdir(observability.DATA_DIR))
    client.get("/api/system/diagnostic-report")
    after = set(os.listdir(observability.DATA_DIR))
    assert after == before


def test_diagnostic_report_names_its_own_redaction_policy(client):
    body = client.get("/api/system/diagnostic-report").json()
    assert observability.REDACTED in body["redaction"]
    assert "Skim it before" in body["redaction"]


# ---------------------------------------------------------- network -------
def test_network_endpoint_reports_state_connectivity_and_firewall(client, monkeypatch):
    monkeypatch.setattr(network_health, "list_host_ips", lambda: ["192.168.1.50"])
    body = client.get("/api/system/network").json()

    assert body["connectivity"]["mode"] == "lan_only"
    ports = {p["port"] for p in body["firewall"]["lan_only_ports"]}
    assert {8500, 6668} <= ports
    assert all(p["safe_to_close_externally"] for p in body["firewall"]["lan_only_ports"])


def test_network_refresh_takes_a_reading_now(client, monkeypatch):
    monkeypatch.setattr(network_health, "primary_ip", lambda: "192.168.1.50")
    body = client.post("/api/system/network/refresh").json()
    assert body["ip"] == "192.168.1.50"
    assert client.get("/api/system/network").json()["state"]["current_ip"] == "192.168.1.50"


def test_latency_history_endpoint(client):
    client.get("/api/devices/bulb-1/status")
    body = client.get("/api/devices/bulb-1/latency-history").json()
    assert body["device_id"] == "bulb-1"
    assert body["sample_count"] >= 1
    assert "resets when the backend restarts" in body["note"]


def test_latency_history_404s_for_an_unknown_device(client):
    assert client.get("/api/devices/nope/latency-history").status_code == 404


# ----------------------------------------------------- remote access ------
def test_remote_access_status_endpoint(client):
    body = client.get("/api/system/remote-access/status").json()
    assert body["public_ip"]["ip"] is None
    assert body["duckdns"]["domain"] is None
    assert body["warnings"] == []


def test_detect_public_ip_endpoint_uses_the_real_lookup_path(client, monkeypatch):
    monkeypatch.setattr(ras, "_default_fetcher", lambda url, timeout: "198.51.100.4")
    body = client.post("/api/system/remote-access/detect-public-ip").json()
    assert body["public_ip"] == "198.51.100.4"
    assert client.get("/api/system/remote-access/status").json()["public_ip"]["ip"] == "198.51.100.4"


def test_duckdns_sync_endpoint_records_and_arms_the_failsafe(client, auth_reset):
    resp = client.post("/api/system/remote-access/duckdns-sync",
                       json={"domain": "myhouse.duckdns.org", "ip": "198.51.100.4", "ok": True})
    body = resp.json()
    assert body["duckdns"]["domain"] == "myhouse.duckdns.org"
    assert body["exposure"]["configured"] is True
    # PIN gate is off in this fixture, so the fail-safe warning fires.
    assert any(w["id"] == "exposure_configured_gate_disabled" for w in body["warnings"])


def test_exposure_can_be_retracted_through_the_api(client):
    client.post("/api/system/remote-access/exposure",
                json={"configured": True, "source": "manual port forward"})
    assert client.get("/api/system/remote-access/status").json()["warnings"]

    body = client.post("/api/system/remote-access/exposure", json={"configured": False}).json()
    assert body["exposure"]["configured"] is False
    assert body["warnings"] == []


def test_tailscale_endpoint_degrades_gracefully_without_tailscale(client, monkeypatch):
    def missing(args, timeout):
        raise FileNotFoundError("tailscale")

    monkeypatch.setattr(ras, "_default_runner", missing)
    body = client.get("/api/system/remote-access/tailscale").json()
    assert body["installed"] is False
    assert body["tailnet_url"] is None


def test_tailscale_endpoint_surfaces_the_tailnet_url(client, monkeypatch):
    payload = json.dumps({"BackendState": "Running",
                          "Self": {"DNSName": "desk.tailnet-abc.ts.net.",
                                   "TailscaleIPs": ["100.101.102.103"]},
                          "Peer": {}})

    class Completed:
        returncode = 0
        stdout = payload
        stderr = ""

    monkeypatch.setattr(ras, "_default_runner", lambda args, timeout: Completed())
    body = client.get("/api/system/remote-access/tailscale").json()
    assert body["running"] is True
    assert body["tailnet_url"].startswith("http://desk.tailnet-abc.ts.net:")


# ------------------------------------------------------------ auth gate ---
def test_new_system_routes_are_gated_by_the_pin_gate(client, auth_reset):
    import remote_auth

    remote_auth.enable("13571357")
    try:
        for path in ("/metrics", "/api/system/metrics", "/api/system/health-summary",
                     "/api/system/logs", "/api/system/diagnostic-report",
                     "/api/system/network", "/api/system/remote-access/status"):
            assert client.get(path).status_code == 401, f"{path} was reachable without a session"
    finally:
        remote_auth.disable()
