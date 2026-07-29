"""GET /api/analytics/usage -- energy/usage analytics.

This project has no real wattage/power-draw source and no real
long-running device history in this environment, so these tests seed
synthetic-but-realistic "power" history entries directly through
BulbController._log() (the exact same method every real power()/toggle()/
scene/timer call uses) and verify the on-time math analytics.py derives
from them is actually computed, not hardcoded.
"""

from datetime import datetime, timedelta, timezone

import pytest

import analytics
import bulb_manager as bm


def _seed_power_transition(controller, ts, on):
    controller._log("power", {"on": on})
    controller._history[0]["timestamp"] = ts.isoformat().replace("+00:00", "Z")


def test_usage_summary_computes_ontime_from_seeded_transitions(fake_config):
    fixed_now = datetime(2026, 7, 29, 15, 0, 0, tzinfo=timezone.utc)

    c1 = bm.get_controller("bulb-1")
    c1._history.clear()
    _seed_power_transition(c1, fixed_now - timedelta(hours=3), True)   # on  @ 12:00
    _seed_power_transition(c1, fixed_now - timedelta(hours=2), False)  # off @ 13:00
    _seed_power_transition(c1, fixed_now - timedelta(hours=1), True)   # on  @ 14:00, still on at "now"

    c2 = bm.get_controller("bulb-2")
    c2._history.clear()  # no history at all in the period

    result = analytics.usage_summary("today", now=fixed_now)

    assert result["period"] == "today"
    assert result["data_source"].startswith("LIVE DATA")
    devices = {d["device_id"]: d for d in result["devices"]}
    assert set(devices) == {"bulb-1", "bulb-2"}

    bulb1 = devices["bulb-1"]
    # on 12:00-13:00 (1h) + on 14:00-15:00 (1h, still on at "now") = 2h total
    assert abs(bulb1["on_time_seconds"] - 7200) < 1
    assert bulb1["on_time_hours"] == 2.0

    bulb2 = devices["bulb-2"]
    assert bulb2["on_time_seconds"] == 0.0
    assert bulb2["on_time_minutes"] == 0.0
    assert bulb2["on_time_hours"] == 0.0


def test_usage_summary_ignores_transitions_outside_the_window(fake_config):
    fixed_now = datetime(2026, 7, 29, 15, 0, 0, tzinfo=timezone.utc)
    c1 = bm.get_controller("bulb-1")
    c1._history.clear()
    # both transitions are yesterday -- should not count toward "today"
    _seed_power_transition(c1, fixed_now - timedelta(days=1, hours=2), True)
    _seed_power_transition(c1, fixed_now - timedelta(days=1, hours=1), False)

    result = analytics.usage_summary("today", now=fixed_now)
    bulb1 = next(d for d in result["devices"] if d["device_id"] == "bulb-1")
    assert bulb1["on_time_seconds"] == 0.0


def test_usage_summary_carries_state_that_started_before_the_window(fake_config):
    fixed_now = datetime(2026, 7, 29, 15, 0, 0, tzinfo=timezone.utc)
    c1 = bm.get_controller("bulb-1")
    c1._history.clear()
    # turned on yesterday, never turned off -> should count from start-of-window to now for "today"
    _seed_power_transition(c1, fixed_now - timedelta(days=1), True)

    result = analytics.usage_summary("today", now=fixed_now)
    bulb1 = next(d for d in result["devices"] if d["device_id"] == "bulb-1")
    start_of_today = fixed_now.replace(hour=0, minute=0, second=0, microsecond=0)
    expected_seconds = (fixed_now - start_of_today).total_seconds()
    assert abs(bulb1["on_time_seconds"] - expected_seconds) < 1


def test_usage_summary_zero_for_device_with_no_history_at_all(fake_config):
    result = analytics.usage_summary("7d")
    assert {d["device_id"] for d in result["devices"]} == {"bulb-1", "bulb-2"}
    assert all(d["on_time_seconds"] == 0.0 for d in result["devices"])


def test_usage_summary_rejects_unknown_period():
    with pytest.raises(ValueError):
        analytics.usage_summary("monthly")


def test_analytics_endpoint_http_roundtrip_with_seeded_history(client):
    controller = bm.get_controller("bulb-1")
    controller._history.clear()
    now = datetime.now(timezone.utc)
    _seed_power_transition(controller, now - timedelta(minutes=90), True)
    _seed_power_transition(controller, now - timedelta(minutes=30), False)

    resp = client.get("/api/analytics/usage", params={"period": "7d"})
    assert resp.status_code == 200
    body = resp.json()
    devices = {d["device_id"]: d for d in body["devices"]}

    bulb1 = devices["bulb-1"]
    # on for 60 minutes (90 min ago -> 30 min ago), safely inside the 7-day window
    assert abs(bulb1["on_time_seconds"] - 3600) < 5
    bulb2 = devices["bulb-2"]
    assert bulb2["on_time_seconds"] == 0.0


def test_analytics_endpoint_defaults_to_today_period(client):
    resp = client.get("/api/analytics/usage")
    assert resp.status_code == 200
    assert resp.json()["period"] == "today"


def test_analytics_endpoint_rejects_unknown_period(client):
    resp = client.get("/api/analytics/usage", params={"period": "monthly"})
    assert resp.status_code == 400
