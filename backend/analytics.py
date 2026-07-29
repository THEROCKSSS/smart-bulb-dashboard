"""Energy/usage analytics derived honestly from real logged state-change
history (see `bulb_manager.BulbController._log` / `.history()`).

This project has no real wattage/power-draw data source (the bulb model in
use doesn't expose it -- see ROADMAP.md / roadmap/week-4-analytics-polish-
and-release.md section 1, item 5). Rather than fabricate wattage numbers,
this module computes the one honest metric that already exists in the
data: on-time / usage-duration per device, derived from the real "power"
action entries every power(), toggle(), scene, preset, timer, and
identify/flash-alert call already writes to history.
"""

from datetime import datetime, timedelta, timezone

import bulb_manager as bm
import config as cfgmod

VALID_PERIODS = {"today", "7d"}


def _period_bounds(period, now=None):
    """Returns (start, end) as tz-aware UTC datetimes for the requested
    period. `now` is injectable so tests can get deterministic results
    without depending on wall-clock timing; production callers always pass
    None and get the real current instant."""
    now = now or datetime.now(timezone.utc)
    if period == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "7d":
        start = now - timedelta(days=7)
    else:
        raise ValueError(f"unknown period '{period}', expected one of {sorted(VALID_PERIODS)}")
    return start, now


def _parse_ts(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _device_on_seconds(history, start, end):
    """Walks the real power-transition log entries for one device and
    returns total seconds it was ON within [start, end].

    Only successful ("ok": True) "power" action entries are treated as real
    state transitions. Every code path that changes power -- power(),
    toggle() (which calls power() internally), apply_scene(), sleep/wake
    timers firing, identify(), flash_alert() -- routes through power(),
    so this single action type captures every real transition without
    needing to special-case each caller.

    If there is no transition on record before `start`, we have no evidence
    the device was ever on, so no time before the first in-window
    transition is counted -- this deliberately under-counts rather than
    guesses/fabricates a prior state.
    """
    events = []
    for entry in history:
        if entry.get("action") != "power" or not entry.get("ok", True):
            continue
        on = entry.get("params", {}).get("on")
        if on is None:
            continue
        try:
            ts = _parse_ts(entry["timestamp"])
        except Exception:
            continue
        events.append((ts, bool(on)))
    events.sort(key=lambda e: e[0])

    state = False
    for ts, on in events:
        if ts <= start:
            state = on
        else:
            break

    total = 0.0
    cursor = start
    for ts, on in events:
        if ts <= start:
            continue
        if ts >= end:
            break
        if state:
            total += (ts - cursor).total_seconds()
        cursor = ts
        state = on

    if state and cursor < end:
        total += (end - cursor).total_seconds()

    return max(0.0, total)


def usage_summary(period, now=None):
    """Per-device on-time summary for `period` ("today" or "7d"). Every
    configured device is always present in the result, with a real 0 when
    it has no history in the window -- never omitted, per this project's
    own no-blank-data-areas convention."""
    start, end = _period_bounds(period, now=now)
    cfg = cfgmod.load_config()
    devices = cfg.get("devices", [])

    results = []
    for d in devices:
        controller = bm.get_controller(d["id"])
        history = controller.history() if controller else []
        seconds = _device_on_seconds(history, start, end)
        results.append({
            "device_id": d["id"],
            "name": d.get("name", d["id"]),
            "on_time_seconds": round(seconds, 1),
            "on_time_minutes": round(seconds / 60, 1),
            "on_time_hours": round(seconds / 3600, 2),
        })

    return {
        "period": period,
        "start": start.isoformat().replace("+00:00", "Z"),
        "end": end.isoformat().replace("+00:00", "Z"),
        "data_source": "LIVE DATA (derived from logged power state-change history)",
        "devices": results,
    }
