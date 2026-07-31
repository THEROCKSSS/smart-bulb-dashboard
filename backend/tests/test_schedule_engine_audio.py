"""Week 1 Phase D, section 8: the schedule-engine extension that starts an
audio-reactive session (via a named session preset) at the scheduled time.

This extends the existing recurring-schedule engine (schedule_engine.py)
rather than inventing a parallel one -- a rule with action
"audio_reactive_preset" flows through the exact same add_rule/_tick/
start_scheduler path as "power_on"/"scene"/"preset" always have.

Uses `_tick`'s injectable `now` parameter (added for this ticket) to
fast-forward through scheduled times instead of sleeping for real minutes.
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import schedule_engine  # noqa: E402
import audio_presets  # noqa: E402
import audio_reactive  # noqa: E402


class FakeController:
    def __init__(self, device_id):
        self.cfg = {"id": device_id}
        self.logs = []

    def _log(self, action, params=None, ok=True, error=None):
        self.logs.append((action, params, ok, error))

    def power(self, on):
        self.logs.append(("power", on))


def _isolate_schedule(tmp_path, monkeypatch):
    monkeypatch.setattr(schedule_engine, "SCHEDULE_PATH", str(tmp_path / "schedules.json"))
    schedule_engine._fired_today_cache.clear()


def _isolate_presets(tmp_path, monkeypatch):
    monkeypatch.setattr(audio_presets, "SESSION_PRESETS_PATH", str(tmp_path / "audio_session_presets.json"))
    monkeypatch.setattr(audio_presets, "LAST_SESSION_PATH", str(tmp_path / "audio_last_session.json"))


def test_audio_reactive_preset_rule_fires_start_session_at_the_right_time(tmp_path, monkeypatch):
    _isolate_schedule(tmp_path, monkeypatch)
    _isolate_presets(tmp_path, monkeypatch)

    preset = audio_presets.save_preset(
        "Evening Chill", "bulb-1",
        {"device_index": 2, "mode": "breathing_silence", "sensitivity": 0.8,
         "n_bands": 3, "min_dwell_ms": 100, "max_duration_s": 7200, "warmup_s": 3.0},
    )

    started_with = []

    def fake_start_session(controller, device_index, mode, sensitivity, monochrome_hue, n_bands,
                            min_dwell_ms, **kwargs):
        started_with.append({
            "device_id": controller.cfg["id"], "device_index": device_index, "mode": mode,
            "sensitivity": sensitivity, "n_bands": n_bands, "min_dwell_ms": min_dwell_ms, **kwargs,
        })

    monkeypatch.setattr(audio_reactive, "start_session", fake_start_session)

    rule = schedule_engine.add_rule("bulb-1", "22:00", ["daily"], "audio_reactive_preset",
                                     {"preset_id": preset["id"]})

    controller = FakeController("bulb-1")
    get_controller = lambda device_id: controller if device_id == "bulb-1" else None

    # Fast-forwarded clock: one minute before the scheduled time -> must not fire.
    schedule_engine._tick(get_controller, now=datetime(2026, 7, 30, 21, 59))
    assert started_with == []

    # Exactly the scheduled minute -> must fire exactly once, with the
    # preset's stored config actually applied.
    schedule_engine._tick(get_controller, now=datetime(2026, 7, 30, 22, 0))
    assert len(started_with) == 1
    call = started_with[0]
    assert call["device_id"] == "bulb-1"
    assert call["device_index"] == 2
    assert call["mode"] == "breathing_silence"
    assert call["sensitivity"] == 0.8
    assert call["n_bands"] == 3
    assert call["min_dwell_ms"] == 100
    assert call["max_duration_s"] == 7200
    assert call["warmup_s"] == 3.0

    # Ticking again within the same minute must NOT re-fire (dedup cache).
    schedule_engine._tick(get_controller, now=datetime(2026, 7, 30, 22, 0))
    assert len(started_with) == 1

    # The next day, at the same scheduled time, it fires again.
    schedule_engine._tick(get_controller, now=datetime(2026, 7, 31, 22, 0))
    assert len(started_with) == 2

    fired_logs = [entry for entry in controller.logs if entry[0] == "schedule_fired"]
    assert len(fired_logs) == 2
    assert fired_logs[0][1]["action"] == "audio_reactive_preset"


def test_audio_reactive_preset_rule_with_unknown_preset_logs_a_schedule_error(tmp_path, monkeypatch):
    _isolate_schedule(tmp_path, monkeypatch)
    _isolate_presets(tmp_path, monkeypatch)

    schedule_engine.add_rule("bulb-1", "09:00", ["daily"], "audio_reactive_preset",
                              {"preset_id": "does-not-exist"})
    controller = FakeController("bulb-1")
    get_controller = lambda device_id: controller

    schedule_engine._tick(get_controller, now=datetime(2026, 7, 30, 9, 0))
    error_logs = [entry for entry in controller.logs if entry[0] == "schedule_error"]
    assert len(error_logs) == 1


def test_audio_reactive_preset_rule_respects_day_of_week_filter(tmp_path, monkeypatch):
    _isolate_schedule(tmp_path, monkeypatch)
    _isolate_presets(tmp_path, monkeypatch)
    preset = audio_presets.save_preset("Weekday Only", "bulb-1", {"mode": "vu_meter", "n_bands": 3, "min_dwell_ms": 90})

    started_with = []
    monkeypatch.setattr(audio_reactive, "start_session",
                         lambda *a, **k: started_with.append((a, k)))

    # Monday=0 ... Sunday=6. Restrict to weekdays 0-4.
    schedule_engine.add_rule("bulb-1", "08:00", [0, 1, 2, 3, 4], "audio_reactive_preset",
                              {"preset_id": preset["id"]})
    controller = FakeController("bulb-1")
    get_controller = lambda device_id: controller

    # 2026-08-01 is a Saturday (weekday()==5) -> must NOT fire.
    schedule_engine._tick(get_controller, now=datetime(2026, 8, 1, 8, 0))
    assert started_with == []

    # 2026-08-03 is a Monday (weekday()==0) -> must fire.
    schedule_engine._tick(get_controller, now=datetime(2026, 8, 3, 8, 0))
    assert len(started_with) == 1
