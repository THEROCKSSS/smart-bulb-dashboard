"""Tests for the dedicated security-events log (Week 2, W2-141..160):
severity, rotation/retention, export, search, alerting thresholds, and the
tamper-evident hmac chain.

Every path here is redirected to a pytest tmp dir by conftest's autouse
`security_audit_isolation` fixture, so nothing touches the real
backend/data/security_events.log or its chain.
"""
import json
import os

import pytest

import config as cfgmod
import remote_auth
import security_audit


@pytest.fixture
def audit():
    """The module itself, with a clean config for each test. Returned as a
    fixture rather than imported directly so it's obvious in each test that
    the isolation fixture has already run."""
    security_audit.update_config(min_severity="info", alert_min_severity="warning",
                                 local_alerts_enabled=True, webhook_enabled=False,
                                 alert_thresholds={"login_failure": {"count": 3, "window_s": 300}})
    return security_audit


def _log_lines():
    if not os.path.exists(security_audit.EVENTS_LOG_PATH):
        return []
    with open(security_audit.EVENTS_LOG_PATH, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


# --------------------------------------------------------------- writing --

def test_event_written_with_severity_source_and_chain_fields(audit):
    entry = audit.log_event("login_lockout", "failure", source="remote_auth", ip="10.0.0.9")

    assert entry["event"] == "login_lockout"
    assert entry["severity"] == "warning"  # from DEFAULT_SEVERITIES
    assert entry["outcome"] == "failure"
    assert entry["source"] == "remote_auth"
    assert entry["detail"] == {"ip": "10.0.0.9"}
    assert entry["seq"] == 1
    assert entry["prev"] == security_audit.GENESIS_PREV
    assert len(entry["hmac"]) == 64

    lines = _log_lines()
    assert len(lines) == 1
    assert lines[0]["hmac"] == entry["hmac"]


def test_unknown_event_defaults_to_info(audit):
    entry = audit.log_event("something_new")
    assert entry["severity"] == "info"


def test_min_severity_filters_writes_out_entirely(audit):
    audit.update_config(min_severity="warning")

    assert audit.log_event("login_success") is None  # info
    assert audit.log_event("login_failure") is None  # notice
    kept = audit.log_event("login_lockout")          # warning

    assert kept is not None
    assert [e["event"] for e in _log_lines()] == ["login_lockout"]


def test_severity_override_changes_classification(audit):
    """W2-150: an operator who considers a failed login actionable on their
    network must be able to say so without editing code."""
    audit.update_config(severity_overrides={"login_failure": "critical"})
    entry = audit.log_event("login_failure", "failure")
    assert entry["severity"] == "critical"
    assert audit.is_actionable("critical") is True


def test_update_config_rejects_invalid_values(audit):
    with pytest.raises(ValueError):
        audit.update_config(min_severity="loud")
    with pytest.raises(ValueError):
        audit.update_config(severity_overrides={"x": "nope"})
    with pytest.raises(ValueError):
        audit.update_config(alert_thresholds={"x": {"count": 0, "window_s": 10}})
    with pytest.raises(ValueError):
        audit.update_config(webhook_url="ftp://somewhere")
    with pytest.raises(ValueError):
        audit.update_config(retention_days=0)
    # A rejected update must not have partially applied.
    assert audit.get_config()["min_severity"] == "info"


def test_secret_shaped_detail_keys_are_redacted(audit):
    """Backstop only -- no call site is supposed to pass these at all. If
    one ever does, the value must not reach the log."""
    entry = audit.log_event("login_failure", "failure", pin="123456",
                            local_key="abcdef0123456789", session_token="tok")  # nosecret: synthetic
    assert entry["detail"] == {"pin": "[redacted]", "local_key": "[redacted]",
                               "session_token": "[redacted]"}
    raw = open(security_audit.EVENTS_LOG_PATH, encoding="utf-8").read()
    assert "123456" not in raw
    assert "abcdef0123456789" not in raw


# ---------------------------------------------------------- tamper-evidence --

def test_verify_passes_on_an_untouched_log(audit):
    for i in range(5):
        audit.log_event("login_success", ip=f"10.0.0.{i}")
    result = audit.verify_chain()
    assert result["ok"] is True
    assert result["complete"] is True
    assert result["entries"] == 5
    assert result["first_bad_seq"] is None


def test_verify_detects_an_edited_entry(audit):
    audit.log_event("login_failure", "failure", ip="10.0.0.5")
    audit.log_event("login_success", ip="10.0.0.5")
    audit.log_event("logout", ip="10.0.0.5")

    lines = _log_lines()
    lines[1]["outcome"] = "failure"  # rewrite history: success -> failure
    with open(security_audit.EVENTS_LOG_PATH, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line, sort_keys=True) + "\n")

    result = audit.verify_chain()
    assert result["ok"] is False
    assert result["first_bad_seq"] == 2
    assert "altered" in result["reason"]


def test_verify_detects_a_deleted_middle_entry(audit):
    """The scenario the chain exists for: someone removes the one line that
    records their lockout and leaves the rest intact."""
    audit.log_event("login_success")
    audit.log_event("login_lockout", "failure")
    audit.log_event("login_success")

    lines = _log_lines()
    del lines[1]
    with open(security_audit.EVENTS_LOG_PATH, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line, sort_keys=True) + "\n")

    result = audit.verify_chain()
    assert result["ok"] is False
    assert "removed" in result["reason"] or "gap" in result["reason"]


def test_verify_detects_truncation_from_the_end(audit):
    """A pure hash chain cannot catch this on its own -- the shortened file
    is internally consistent. The separate state file is what catches it."""
    audit.log_event("login_success")
    audit.log_event("login_lockout", "failure")

    lines = _log_lines()[:1]
    with open(security_audit.EVENTS_LOG_PATH, "w", encoding="utf-8") as f:
        f.write(json.dumps(lines[0], sort_keys=True) + "\n")

    result = audit.verify_chain()
    assert result["ok"] is False
    assert "removed from the end" in result["reason"]


def test_verify_detects_a_deleted_log_file(audit):
    audit.log_event("login_lockout", "failure")
    os.remove(security_audit.EVENTS_LOG_PATH)

    result = audit.verify_chain()
    assert result["ok"] is False
    assert "deleted or truncated" in result["reason"]


def test_verify_flags_a_corrupt_line(audit):
    audit.log_event("login_success")
    with open(security_audit.EVENTS_LOG_PATH, "a", encoding="utf-8") as f:
        f.write("{not json at all\n")
    result = audit.verify_chain()
    assert result["ok"] is False
    assert "unparseable" in result["reason"]


def test_hmac_key_is_never_written_into_the_log(audit):
    audit.log_event("login_success")
    key = audit._get_key()
    raw = open(security_audit.EVENTS_LOG_PATH, encoding="utf-8").read()
    assert key not in raw
    assert len(key) == 64  # 256 bits


# --------------------------------------------------------- rotation/retention --

def _oldest_segment_index():
    """Highest existing `.N` suffix -- rotation shifts segments upward, so
    the biggest number is the oldest data."""
    return max(i for i in range(1, 21) if os.path.exists(security_audit._rotated_path(i)))


def test_rotation_writes_a_marker_and_keeps_the_chain_verifiable(audit):
    """Rotation must not break the chain, and must not leave the new
    segment empty (an empty segment would look exactly like truncation).
    Kept under `rotate_keep` rotations so nothing is pruned here -- pruning
    is the next test's subject."""
    audit.update_config(max_log_bytes=800, rotate_keep=5)
    for i in range(8):
        audit.log_event("login_success", ip=f"10.0.0.{i}")

    assert os.path.exists(security_audit._rotated_path(1))
    current = _log_lines()
    assert current, "the new segment must not be empty after rotation"
    assert current[0]["event"] == "audit_log_rotated"

    result = audit.verify_chain(include_rotated=True)
    assert result["ok"] is True, result["reason"]
    assert result["complete"] is True


def test_verify_reports_incomplete_not_tampered_when_old_segments_are_gone(audit):
    """Retention deleting the OLDEST segment is housekeeping, not
    tampering. Conflating the two would train the operator to ignore a real
    result. (Removing a segment from the middle is a different thing
    entirely, and is caught as a broken link -- see the next test.)"""
    audit.update_config(max_log_bytes=800, rotate_keep=5)
    for i in range(8):
        audit.log_event("login_success", ip=f"10.0.0.{i}")
    os.remove(security_audit._rotated_path(_oldest_segment_index()))

    result = audit.verify_chain(include_rotated=True)
    assert result["ok"] is True, result["reason"]
    assert result["complete"] is False
    assert "earlier segments are gone" in result["reason"]


def test_verify_catches_a_whole_segment_deleted_from_the_middle(audit):
    """Deleting a rotated segment that isn't the oldest leaves a real hole
    in the chain, and must read as tampering rather than housekeeping."""
    audit.update_config(max_log_bytes=800, rotate_keep=5)
    for i in range(8):
        audit.log_event("login_success", ip=f"10.0.0.{i}")
    os.remove(security_audit._rotated_path(1))  # newest rotated == the hole

    result = audit.verify_chain(include_rotated=True)
    assert result["ok"] is False
    assert "removed" in result["reason"] or "gap" in result["reason"]


def test_retention_removes_segments_beyond_rotate_keep(audit):
    audit.update_config(max_log_bytes=400, rotate_keep=2)
    for i in range(40):
        audit.log_event("login_success", ip=f"10.0.0.{i}")
    # Force extra segments to exist beyond the keep count.
    for i in (3, 4):
        with open(security_audit._rotated_path(i), "w", encoding="utf-8") as f:
            f.write("")

    removed = audit.apply_retention()
    assert "security_events.log.3" in removed
    assert "security_events.log.4" in removed
    assert os.path.exists(security_audit.EVENTS_LOG_PATH), "current segment must survive"


def test_retention_removes_segments_older_than_retention_days(audit):
    audit.update_config(retention_days=1)
    old = security_audit._rotated_path(1)
    with open(old, "w", encoding="utf-8") as f:
        f.write("")
    os.utime(old, (0, 0))  # 1970 -- comfortably older than 1 day

    assert "security_events.log.1" in audit.apply_retention()
    assert not os.path.exists(old)


# ------------------------------------------------------------ search/export --

def test_search_filters_by_event_severity_text_and_time(audit):
    audit.log_event("login_success", ip="10.0.0.1")
    audit.log_event("login_lockout", "failure", ip="10.0.0.2")
    audit.log_event("device_added", device="bulb-9")

    assert [e["event"] for e in audit.read_events(event="login_lockout")] == ["login_lockout"]
    assert {e["event"] for e in audit.read_events(min_severity="warning")} == \
        {"login_lockout", "device_added"}
    assert [e["event"] for e in audit.read_events(q="10.0.0.2")] == ["login_lockout"]
    assert [e["event"] for e in audit.read_events(outcome="failure")] == ["login_lockout"]
    assert audit.read_events(since=9e9) == []
    # Newest first, matching every other list in this app.
    assert [e["event"] for e in audit.read_events()][0] == "device_added"


def test_search_marks_actionable_entries(audit):
    """W2-159: the UI must be able to tell 'PIN gate enabled' from
    'lockout triggered' without re-deriving the rule."""
    audit.log_event("login_success")
    audit.log_event("login_lockout", "failure")
    by_event = {e["event"]: e for e in audit.read_events()}
    assert by_event["login_success"]["actionable"] is False
    assert by_event["login_lockout"]["actionable"] is True


def test_search_rejects_an_unknown_severity(audit):
    with pytest.raises(ValueError):
        audit.read_events(min_severity="extremely")


def test_export_json_keeps_the_chain_fields(audit):
    audit.log_event("login_lockout", "failure", ip="10.0.0.3")
    content, media_type, filename = audit.export_events(fmt="json")
    parsed = json.loads(content)

    assert media_type == "application/json"
    assert filename.endswith(".json")
    # Without prev/hmac an export can't be independently verified, which
    # defeats the point of exporting an audit log at all.
    assert "hmac" in parsed[0] and "prev" in parsed[0]


def test_export_csv_flattens_detail(audit):
    audit.log_event("login_lockout", "failure", ip="10.0.0.3")
    content, media_type, filename = audit.export_events(fmt="csv")

    assert media_type == "text/csv"
    assert filename.endswith(".csv")
    header, row = content.strip().splitlines()[0], content.strip().splitlines()[1]
    assert header.startswith("seq,timestamp,ts,event,severity,outcome,source,detail")
    assert "login_lockout" in row
    assert "10.0.0.3" in row


def test_export_rejects_an_unknown_format(audit):
    with pytest.raises(ValueError):
        audit.export_events(fmt="xml")


# ---------------------------------------------------------------- alerting --

def test_ordinary_daily_use_raises_no_alerts(audit):
    """W2-156, the alert-fatigue requirement, pinned as a test: a normal
    day (log in, change something, log out, one mistyped PIN) must produce
    an empty alert queue under the shipped defaults. If a future change
    promotes one of these to warning, this test fails and forces the
    tradeoff to be made deliberately."""
    audit.log_event("login_success", ip="10.0.0.1")
    audit.log_event("config_changed", file="config.json")
    audit.log_event("login_failure", "failure", ip="10.0.0.1")
    audit.log_event("backup_created", name="backup-x.zip")
    audit.log_event("logout", ip="10.0.0.1")

    assert audit.list_alerts() == []


def test_warning_and_above_raise_alerts(audit):
    audit.log_event("login_lockout", "failure", ip="10.0.0.7")
    audit.log_event("remote_auth_disabled")

    alerts = audit.list_alerts()
    events = [a["event"] for a in alerts]
    assert "login_lockout" in events
    assert "remote_auth_disabled" in events
    assert any(a["severity"] == "critical" for a in alerts)


def test_alert_threshold_aggregates_a_burst_into_one_alert(audit):
    """W2-142: three failed logins in the window is one alert, not three --
    and the individual failures stay at `notice`, below the alert floor."""
    for _ in range(3):
        audit.log_event("login_failure", "failure", ip="10.0.0.8")

    alerts = audit.list_alerts()
    assert len(alerts) == 1
    assert alerts[0]["kind"] == "threshold"
    assert alerts[0]["severity"] == "warning"
    assert "3 x login_failure" in alerts[0]["message"]

    # The burst counter resets, so the next two failures don't re-alert.
    audit.log_event("login_failure", "failure", ip="10.0.0.8")
    audit.log_event("login_failure", "failure", ip="10.0.0.8")
    assert len(audit.list_alerts()) == 1


def test_threshold_window_expires(audit):
    """Three failures spread out over an evening are not a brute force, and
    must not alert. Uses a deliberately tiny window and real sleeps rather
    than patching the clock: `security_audit.time` *is* the stdlib time
    module, so monkeypatching `time.time` through it patches it globally --
    for pytest's own internals too. (Learned the hard way: doing that made
    this test fail intermittently with StopIteration from unrelated
    callers draining the fake clock.)"""
    import time as real_time

    audit.update_config(alert_thresholds={"login_failure": {"count": 3, "window_s": 0.4}})
    for i in range(3):
        audit.log_event("login_failure", "failure")
        if i < 2:
            real_time.sleep(0.5)

    assert audit.list_alerts() == []


def test_webhook_is_called_for_an_alert_and_not_for_routine_events(audit, monkeypatch):
    """W2-158, minus the live network: the delivery function is the seam.
    Asserting on it proves the pipeline is wired end to end without this
    suite depending on an outbound HTTP call."""
    delivered = []
    monkeypatch.setattr(security_audit, "_deliver_webhook",
                        lambda url, alert: delivered.append((url, alert)))
    audit.update_config(webhook_enabled=True, webhook_url="https://example.invalid/hook")

    audit.log_event("login_success")
    assert delivered == []

    audit.log_event("login_lockout", "failure", ip="10.0.0.4")
    assert len(delivered) == 1
    assert delivered[0][0] == "https://example.invalid/hook"
    assert delivered[0][1]["event"] == "login_lockout"


def test_local_only_mode_never_calls_the_webhook(audit, monkeypatch):
    """W2-149: 'I want no external integrations' has to be a real setting,
    not just leaving the URL blank and hoping."""
    delivered = []
    monkeypatch.setattr(security_audit, "_deliver_webhook",
                        lambda url, alert: delivered.append(url))
    audit.update_config(webhook_enabled=False, webhook_url="https://example.invalid/hook",
                        local_alerts_enabled=True)

    audit.log_event("login_lockout", "failure")

    assert delivered == []
    assert len(audit.list_alerts()) == 1


def test_alerts_can_be_acknowledged(audit):
    audit.log_event("login_lockout", "failure")
    assert audit.list_alerts(unacknowledged_only=True)
    assert audit.acknowledge_alerts() == 1
    assert audit.list_alerts(unacknowledged_only=True) == []
    assert audit.acknowledge_alerts() == 0


def test_local_alert_queue_is_bounded(audit):
    audit.update_config(max_local_alerts=5)
    for i in range(12):
        audit.log_event("login_lockout", "failure", ip=f"10.0.0.{i}")
    assert len(audit.list_alerts(limit=100)) == 5


# ------------------------------------------------------- self-test / digest --

def test_self_test_writes_a_canary_and_reports_alerting_wiring(audit):
    result = audit.self_test()
    assert result["ok"] is True
    assert result["wrote_event"] is True
    assert result["verification"]["ok"] is True
    assert result["alerting"]["alert_min_severity"] == "warning"
    assert [e["event"] for e in _log_lines()] == ["audit_self_test"]


def test_digest_reports_even_a_quiet_period(audit):
    """A digest that only appears when something happened is ambiguous
    between 'quiet' and 'broken'."""
    digest = audit.digest(days=7)
    assert digest["total_events"] == 0
    assert digest["actionable_count"] == 0
    assert digest["most_recent_actionable"] is None
    assert digest["verification"]["ok"] is True


def test_digest_counts_and_surfaces_the_latest_actionable_event(audit):
    audit.log_event("login_success")
    audit.log_event("login_success")
    audit.log_event("login_lockout", "failure", ip="10.0.0.6")

    digest = audit.digest(days=7)
    assert digest["total_events"] == 3
    assert digest["by_event"]["login_success"] == 2
    assert digest["by_severity"]["warning"] == 1
    assert digest["actionable_count"] == 1
    assert digest["most_recent_actionable"]["event"] == "login_lockout"


# --------------------------------------------- integration with the app ----

def test_auth_events_are_forwarded_from_remote_auth(audit, client):
    """remote_auth keeps its own plain auth log; the security log must get
    the same events without either call site being duplicated."""
    remote_auth.log_audit_event("login_failure", "failure", ip="10.0.0.1")
    events = [e["event"] for e in audit.read_events()]
    assert "login_failure" in events
    assert audit.read_events()[0]["source"] == "remote_auth"


def test_disabling_the_pin_gate_logs_critical_and_alerts(audit, client):
    """W2-145. The routes are exercised rather than the module functions,
    because a route is what an attacker who got a session would call.

    Note the login in the middle: once the gate is on, /disable is itself
    gated, so turning it off genuinely requires the PIN. That's the correct
    behaviour and this test would fail loudly if it ever regressed to
    letting an unauthenticated caller disable the gate."""
    pin = "8461037295"
    client.post("/api/system/remote-auth/enable", json={"pin": pin})
    assert client.post("/api/system/remote-auth/disable").status_code == 401

    try:
        login = client.post("/api/auth/login", json={"pin": pin})
        assert login.status_code == 200
        client.cookies.set(remote_auth.SESSION_COOKIE,
                           login.cookies.get(remote_auth.SESSION_COOKIE))
        assert client.post("/api/system/remote-auth/disable").status_code == 200
    finally:
        client.cookies.clear()
        remote_auth.disable()

    events = {e["event"]: e for e in audit.read_events()}
    assert events["remote_auth_enabled"]["severity"] == "notice"
    assert events["remote_auth_disabled"]["severity"] == "critical"
    assert any(a["event"] == "remote_auth_disabled" for a in audit.list_alerts())


def test_disabling_an_already_disabled_gate_raises_no_alert(audit, client):
    """A UI re-render or a double click must not manufacture a critical
    alert -- see the alert-fatigue rule."""
    client.post("/api/system/remote-auth/disable")
    assert audit.list_alerts() == []
    assert [e["event"] for e in audit.read_events()] == []


def test_a_new_device_in_config_logs_a_warning(audit, tmp_path, monkeypatch):
    """W2-146. Driven through the real config module (not the fake_config
    fixture, which replaces these functions wholesale) against a tmp
    config.json, so the event genuinely fires from the code path any
    device-add goes through."""
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"devices": [], "groups": []}), encoding="utf-8")
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", str(cfg_path))

    cfgmod.upsert_device({"id": "bulb-9", "name": "Hallway", "device_id": "dev-9",
                          "local_key": "shortkey", "ip": "10.0.0.99"})

    events = {e["event"]: e for e in audit.read_events()}
    assert events["device_added"]["severity"] == "warning"
    assert events["device_added"]["detail"]["device"] == "bulb-9"
    assert any(a["event"] == "device_added" for a in audit.list_alerts())

    # W2-147: the file write itself is tracked too, by hash rather than
    # content, so a config change is provable without the log holding
    # credentials.
    assert events["config_changed"]["severity"] == "info"
    assert len(events["config_changed"]["detail"]["sha256"]) == 64

    # Renaming an existing device is a change, not a new device -- if it
    # alerted, every settings edit would be a security alert.
    before = len(audit.read_events(event="device_added"))
    cfgmod.upsert_device({"id": "bulb-9", "name": "Hallway Lamp", "device_id": "dev-9",
                          "local_key": "shortkey", "ip": "10.0.0.99"})
    assert len(audit.read_events(event="device_added")) == before


def test_removing_a_device_logs_but_does_not_alert(audit, tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"devices": [{"id": "bulb-9"}], "groups": []}),
                        encoding="utf-8")
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", str(cfg_path))

    cfgmod.delete_device("bulb-9")

    events = {e["event"]: e for e in audit.read_events()}
    assert events["device_removed"]["severity"] == "notice"
    assert audit.list_alerts() == []


# ------------------------------------------------------------------ API ----

def test_events_api_returns_filtered_results(audit, client):
    audit.log_event("login_lockout", "failure", ip="10.0.0.2")
    audit.log_event("login_success", ip="10.0.0.2")

    resp = client.get("/api/security/events?min_severity=warning")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["events"][0]["event"] == "login_lockout"
    assert body["events"][0]["actionable"] is True
    assert "critical" in body["severities"]


def test_events_api_rejects_a_bad_severity(audit, client):
    assert client.get("/api/security/events?min_severity=nope").status_code == 400


def test_export_api_sets_a_download_filename(audit, client):
    audit.log_event("login_lockout", "failure")
    resp = client.get("/api/security/events/export?format=csv")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "attachment; filename=" in resp.headers["content-disposition"]
    assert client.get("/api/security/events/export?format=xml").status_code == 400


def test_verify_api_reports_tampering(audit, client):
    audit.log_event("login_success")
    audit.log_event("login_success")
    lines = _log_lines()
    lines[0]["event"] = "nothing_to_see_here"
    with open(security_audit.EVENTS_LOG_PATH, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line, sort_keys=True) + "\n")

    body = client.get("/api/security/verify").json()
    assert body["ok"] is False
    assert body["first_bad_seq"] == 1


def test_config_api_round_trip_and_validation(audit, client):
    resp = client.post("/api/security/config", json={"alert_min_severity": "critical"})
    assert resp.status_code == 200
    assert resp.json()["alert_min_severity"] == "critical"
    assert client.get("/api/security/config").json()["alert_min_severity"] == "critical"
    assert client.post("/api/security/config", json={"alert_min_severity": "x"}).status_code == 400


def test_alerts_and_ack_api(audit, client):
    audit.log_event("login_lockout", "failure")
    assert len(client.get("/api/security/alerts").json()["alerts"]) == 1
    assert client.post("/api/security/alerts/ack").json()["acknowledged"] == 1
    assert client.get("/api/security/alerts?unacknowledged_only=true").json()["alerts"] == []


def test_digest_and_self_test_api(audit, client):
    assert client.get("/api/security/digest?days=7").json()["total_events"] == 0
    assert client.get("/api/security/digest?days=0").status_code == 400
    assert client.post("/api/security/self-test").json()["ok"] is True


def test_rotate_api_rotates_and_applies_retention(audit, client):
    audit.log_event("login_success")
    resp = client.post("/api/security/events/rotate")
    assert resp.status_code == 200
    assert os.path.exists(security_audit._rotated_path(1))
    assert client.get("/api/security/verify").json()["ok"] is True


def test_logging_never_raises_when_the_log_is_unwritable(audit, monkeypatch):
    """Best-effort contract, inherited from remote_auth.log_audit_event: a
    disk problem must never break the operation being audited."""
    monkeypatch.setattr(security_audit, "EVENTS_LOG_PATH",
                        os.path.join(security_audit.EVENTS_LOG_PATH, "nope", "x.log"))
    assert audit.log_event("login_lockout", "failure") is None
