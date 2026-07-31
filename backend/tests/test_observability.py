"""Week 2 Phase D, roadmap section 13 -- metrics, /metrics, correlation
IDs, the configurable log level + log viewer, dependency health checks and
the self-diagnostic report.

The redaction tests here are the ones that matter most: the diagnostic
report is designed to be pasted into a bug report by a user who will not
read it first, so "no secret survives it" has to be an asserted property,
not a claim. They deliberately plant a real-shaped local_key both as a
config value and as a bare substring inside a log line, then assert it is
absent from the generated bundle.
"""

import logging

import pytest

import observability


# ------------------------------------------------------------- redaction --
def test_redact_text_masks_assignment_shaped_secrets():
    text = 'connecting with local_key=abc123secretvalue and pin="4821"'
    out = observability.redact_text(text)
    assert "abc123secretvalue" not in out
    assert "4821" not in out
    assert out.count(observability.REDACTED) == 2


def test_redact_text_masks_a_bare_known_secret_value():
    secret = "d41d8cd98f00b204e9800998"
    out = observability.redact_text(f"tinytuya raised: bad key {secret} rejected", [secret])
    assert secret not in out
    assert observability.REDACTED in out


def test_redact_text_leaves_short_known_values_alone():
    # A 4-digit PIN also occurs as a port number, a timestamp fragment, a
    # device-id chunk. Blanket-replacing it would corrupt the report while
    # adding nothing -- the assignment-shaped pass already covers `pin=1234`.
    out = observability.redact_text("listening on port 8500", ["8500"])
    assert out == "listening on port 8500"


def test_redact_obj_masks_secret_named_fields_at_any_depth():
    obj = {
        "devices": [{"id": "bulb-1", "local_key": "verysecretkey123", "ip": "10.0.0.11"}],
        "auth": {"pin_hash": "deadbeef", "nested": {"secret_key": "cafebabe"}},
        "harmless": "keep me",
    }
    out = observability.redact_obj(obj)
    assert out["devices"][0]["local_key"] == observability.REDACTED
    assert out["devices"][0]["ip"] == "10.0.0.11"
    assert out["auth"]["pin_hash"] == observability.REDACTED
    assert out["auth"]["nested"]["secret_key"] == observability.REDACTED
    assert out["harmless"] == "keep me"


def test_redact_obj_masks_a_secret_field_even_when_its_value_is_a_list():
    out = observability.redact_obj({"local_key": ["a", "b"]})
    assert out["local_key"] == observability.REDACTED


# ------------------------------------------------------------- metrics ----
def test_record_request_tracks_counts_latency_and_error_rate():
    observability.record_request("GET", "/api/x", 200, 0.010)
    observability.record_request("GET", "/api/x", 200, 0.020)
    observability.record_request("GET", "/api/x", 500, 0.030)
    observability.record_request("POST", "/api/y", 404, 0.005)

    snapshot = observability.metrics_snapshot()
    assert snapshot["totals"]["requests"] == 4
    assert snapshot["totals"]["errors"] == 1
    assert snapshot["totals"]["client_errors"] == 1
    assert snapshot["totals"]["error_rate"] == 0.25
    assert snapshot["status_classes"] == {"2xx": 2, "5xx": 1, "4xx": 1}

    by_endpoint = {(e["method"], e["endpoint"]): e for e in snapshot["endpoints"]}
    x = by_endpoint[("GET", "/api/x")]
    assert x["requests"] == 3
    assert x["errors"] == 1
    assert round(x["error_rate"], 4) == round(1 / 3, 4)
    assert x["max_ms"] == 30.0
    assert x["avg_ms"] == 20.0


def test_percentiles_are_computed_not_hardcoded():
    for i in range(1, 101):
        observability.record_request("GET", "/api/p", 200, i / 1000)  # 1ms .. 100ms
    endpoint = next(e for e in observability.metrics_snapshot()["endpoints"]
                    if e["endpoint"] == "/api/p")
    assert endpoint["latency_samples"] == 100
    assert endpoint["p50_ms"] == 50.0
    assert endpoint["p95_ms"] == 95.0
    assert endpoint["p99_ms"] == 99.0


def test_latency_window_is_bounded():
    for i in range(observability.LATENCY_WINDOW + 50):
        observability.record_request("GET", "/api/w", 200, 0.001)
    endpoint = next(e for e in observability.metrics_snapshot()["endpoints"]
                    if e["endpoint"] == "/api/w")
    assert endpoint["requests"] == observability.LATENCY_WINDOW + 50
    assert endpoint["latency_samples"] == observability.LATENCY_WINDOW


def test_prometheus_text_is_well_formed_exposition_format():
    observability.record_request("GET", "/api/devices/{device_id}/power", 200, 0.012)
    observability.record_request("GET", "/api/devices/{device_id}/power", 500, 0.050)
    text = observability.prometheus_text(version="9.9.9")
    lines = text.splitlines()

    assert 'sbd_build_info{version="9.9.9"} 1' in lines
    assert any(line.startswith("# HELP sbd_uptime_seconds") for line in lines)
    assert any(line.startswith("# TYPE sbd_requests_total counter") for line in lines)
    assert ('sbd_requests_total{method="GET",endpoint="/api/devices/{device_id}/power"} 2'
            in lines)
    assert ('sbd_request_errors_total{method="GET",endpoint="/api/devices/{device_id}/power"} 1'
            in lines)
    assert any('sbd_request_latency_seconds{' in line and 'quantile="0.95"' in line
               for line in lines)
    assert any(line.startswith("sbd_request_latency_seconds_count{") for line in lines)
    # Every non-comment line must be `name{labels} value` with a numeric value.
    for line in lines:
        if not line or line.startswith("#"):
            continue
        value = line.rsplit(" ", 1)[1]
        float(value)


def test_prometheus_label_values_are_escaped():
    observability.record_request("GET", 'weird"path\\here', 200, 0.001)
    text = observability.prometheus_text()
    assert 'endpoint="weird\\"path\\\\here"' in text


# ------------------------------------------------- route-template lookup --
def test_route_template_maps_a_concrete_path_onto_its_template(client):
    import main as main_module
    template = observability.route_template(
        main_module.app.routes, "POST", "/api/devices/bulb-1/power")
    assert template == "/api/devices/{device_id}/power"


def test_route_template_reports_unmatched_paths_distinctly(client):
    import main as main_module
    template = observability.route_template(
        main_module.app.routes, "GET", "/definitely/not/a/route")
    assert template == observability.UNMATCHED_TEMPLATE


def test_template_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(observability, "TEMPLATE_CACHE_MAX", 3)
    for i in range(20):
        observability.route_template([], "GET", f"/junk/{i}")
    assert len(observability._template_cache) == 3


# ------------------------------------------------------ correlation IDs ---
def test_adopt_correlation_id_accepts_a_plausible_incoming_id():
    assert observability.adopt_correlation_id("abc-123_xyz") == "abc-123_xyz"
    assert observability.get_correlation_id() == "abc-123_xyz"


def test_adopt_correlation_id_rejects_log_injection_shaped_values():
    # A newline in a correlation id means forged log lines, since the id is
    # written verbatim into every record for the request.
    adopted = observability.adopt_correlation_id("evil\nINFO fake log entry")
    assert "\n" not in adopted
    assert adopted != "evil\nINFO fake log entry"
    assert len(adopted) == 16


def test_adopt_correlation_id_rejects_an_overlong_value():
    adopted = observability.adopt_correlation_id("x" * 500)
    assert len(adopted) == 16


# -------------------------------------------------------------- logging ---
def test_log_level_round_trips_and_persists():
    assert observability.get_log_level() == "INFO"
    assert observability.set_log_level("debug") == {"log_level": "DEBUG"}
    assert observability.get_log_level() == "DEBUG"
    assert logging.getLogger(observability.LOGGER_NAME).level == logging.DEBUG
    observability.set_log_level("WARNING")
    assert observability.get_log_level() == "WARNING"


def test_set_log_level_rejects_nonsense():
    with pytest.raises(ValueError):
        observability.set_log_level("chatty")


def test_recent_logs_captures_records_with_their_correlation_id():
    observability.set_log_level("INFO")
    observability.adopt_correlation_id("trace-me")
    observability.get_logger().warning("something happened")
    entries = observability.recent_logs()
    assert entries[0]["message"] == "something happened"
    assert entries[0]["level"] == "WARNING"
    assert entries[0]["correlation_id"] == "trace-me"


def test_recent_logs_level_filter_is_that_level_and_above():
    observability.set_log_level("DEBUG")
    logger = observability.get_logger()
    logger.debug("a debug line")
    logger.info("an info line")
    logger.error("an error line")
    messages = [e["message"] for e in observability.recent_logs(level="INFO")]
    assert "an info line" in messages
    assert "an error line" in messages
    assert "a debug line" not in messages


def test_recent_logs_rejects_an_unknown_level():
    with pytest.raises(ValueError):
        observability.recent_logs(level="loud")


def test_log_buffer_redacts_secret_shaped_messages_on_the_way_in():
    observability.set_log_level("INFO")
    observability.get_logger().info("retrying with local_key=supersecretvalue123")
    entry = observability.recent_logs()[0]
    assert "supersecretvalue123" not in entry["message"]
    assert observability.REDACTED in entry["message"]


# --------------------------------------------------- dependency checks ----
def test_check_dependencies_passes_on_this_install():
    checks = {c["name"]: c for c in observability.check_dependencies(force=True)}
    assert checks["tinytuya"]["ok"] is True
    assert checks["numpy"]["ok"] is True
    assert checks["tinytuya"]["required"] is True
    assert checks["sounddevice"]["required"] is False


def test_startup_dependency_check_raises_with_an_actionable_message(monkeypatch):
    def broken_probe():
        raise ImportError("No module named 'tinytuya'")

    monkeypatch.setattr(observability, "DEPENDENCY_PROBES", (
        ("tinytuya", True, "local bulb control", broken_probe),
    ))
    with pytest.raises(observability.DependencyError) as excinfo:
        observability.startup_dependency_check()

    message = str(excinfo.value)
    assert "tinytuya" in message
    assert "not installed" in message
    # Actionable means it names the actual fix command, not just the problem.
    assert "pip install -r backend/requirements.txt" in message
    assert "venv" in message


def test_startup_dependency_check_tolerates_a_broken_optional_dependency(monkeypatch):
    def broken_probe():
        raise OSError("PortAudio library not found")

    monkeypatch.setattr(observability, "DEPENDENCY_PROBES", (
        ("sounddevice", False, "audio capture only", broken_probe),
    ))
    # Non-fatal: a headless host with no audio backend still gets a working
    # dashboard, it just can't run audio-reactive sessions.
    checks = observability.startup_dependency_check()
    assert checks[0]["ok"] is False
    summary = observability.dependency_summary()
    assert summary["ok"] is True
    assert summary["degraded"] == ["sounddevice"]


def test_dependency_probe_failure_distinguishes_missing_from_broken(monkeypatch):
    def installed_but_broken():
        raise RuntimeError("numpy rfft returned an implausible result")

    monkeypatch.setattr(observability, "DEPENDENCY_PROBES", (
        ("numpy", True, "audio FFT", installed_but_broken),
    ))
    check = observability.check_dependencies(force=True)[0]
    assert check["ok"] is False
    assert "installed but not working" in check["detail"]


# ------------------------------------------------ self-diagnostic report --
def test_diagnostic_report_never_contains_a_real_local_key(fake_config):
    import json as jsonmod

    real_key = "aBcD1234EfGh5678"  # long enough to be a real bare-value leak
    fake_config["devices"][0]["local_key"] = real_key

    # Plant it three ways: as a config value, assignment-shaped in a log
    # line, and bare in a log line (the shape a third-party traceback takes).
    observability.set_log_level("INFO")
    logger = observability.get_logger()
    logger.info("device handshake failed with local_key=%s", real_key)
    logger.info("tinytuya rejected %s during retry", real_key)

    report = observability.diagnostic_report(version="0.0.0-test")
    serialized = jsonmod.dumps(report)

    assert real_key not in serialized
    assert observability.REDACTED in serialized
    assert report["config"]["devices"][0]["local_key_length"] == len(real_key)
    assert report["config"]["devices"][0]["has_local_key"] is True
    # The useful, non-secret parts survive.
    assert report["config"]["devices"][0]["ip"] == "10.0.0.11"
    assert report["version"] == "0.0.0-test"


def test_diagnostic_report_never_contains_the_pin_hash_salt_or_signing_key(auth_reset):
    import json as jsonmod

    import remote_auth
    remote_auth.enable("9182736455")
    state = remote_auth._load()

    report = observability.diagnostic_report()
    serialized = jsonmod.dumps(report)

    for field in ("pin_hash", "salt", "secret_key"):
        assert state[field] not in serialized, f"{field} leaked into the diagnostic report"
    assert "9182736455" not in serialized
    assert report["remote_auth"]["enabled"] is True


def test_diagnostic_report_bundles_the_expected_sections(fake_config):
    report = observability.diagnostic_report()
    for section in ("dependencies", "metrics", "config", "remote_auth",
                    "network", "remote_access", "recent_logs", "history", "redaction"):
        assert section in report, f"missing section: {section}"


def test_diagnostic_report_survives_an_unreadable_config(monkeypatch):
    import config as cfgmod

    def explode():
        raise FileNotFoundError("config.json not found")

    monkeypatch.setattr(cfgmod, "load_config", explode)
    report = observability.diagnostic_report()
    assert report["config"]["available"] is False
    assert "FileNotFoundError" in report["config"]["error"]
