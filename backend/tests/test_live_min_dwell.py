"""Changing dwell on a RUNNING session, without stopping it.

Tuning by ear needs one control to move while the music keeps playing. The
stop / change / start cycle destroys the comparison being made: the bulb goes
dark, the tempo tracker loses its onset history and has to re-lock, and the
thing you were listening against is gone by the time the new value is live.

Run with:
    pytest backend/tests/test_live_min_dwell.py -v
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np  # noqa: E402

import audio_presets  # noqa: E402
import audio_reactive as ar  # noqa: E402
import bulb_manager as bm  # noqa: E402
import capture_sources  # noqa: E402


def _live_session(controller, min_dwell_ms=200):
    """A genuinely running session: `get_active_session` requires is_alive(),
    so a constructed-but-never-started session is correctly invisible to it."""
    block = np.zeros((ar.DEFAULT_HOP_SIZE, 1), dtype=np.float32)

    def factory(callback, channels):
        return capture_sources.CallableSource([block], callback, interval_s=0.02, loop=True)

    session = ar.AudioSession(controller, device_index=None, source_kind="callable",
                              source_factory=factory, min_dwell_ms=min_dwell_ms)
    session.start()
    deadline = time.time() + 3.0
    while not session.is_alive() and time.time() < deadline:
        time.sleep(0.02)
    return session


class StubController:
    def __init__(self):
        self.cfg = {"id": "bulb-1"}
        self.sent = []

    def set_socket_timeout(self, t): pass
    def set_hsv(self, h, s, v): self.sent.append((time.time(), h, s, v))
    def set_rgb(self, r, g, b): pass
    def set_brightness(self, b): pass
    def stop_effect(self): pass
    def _log(self, *a, **k): pass


@pytest.fixture
def presets_isolation(tmp_path, monkeypatch):
    monkeypatch.setattr(audio_presets, "LAST_SESSION_PATH", str(tmp_path / "last.json"))
    monkeypatch.setattr(audio_presets, "SESSION_PRESETS_PATH", str(tmp_path / "presets.json"))
    return tmp_path


# ------------------------------------------------------------- sender level
def test_set_min_dwell_changes_a_running_sender():
    sender = ar.BulbSender(StubController(), min_dwell_ms=200)
    try:
        assert sender.min_dwell_ms == 200
        assert sender.set_min_dwell(60) == 60
        assert sender.status()["min_dwell_ms"] == 60
    finally:
        sender.stop()


def test_min_dwell_is_clamped_to_the_safety_floor_and_ceiling():
    sender = ar.BulbSender(StubController(), min_dwell_ms=90)
    try:
        assert sender.set_min_dwell(1) == ar.MIN_DWELL_FLOOR_MS
        assert sender.set_min_dwell(999999) == ar.MIN_DWELL_MS_CEILING
    finally:
        sender.stop()


def test_lowering_the_dwell_takes_effect_without_waiting_out_the_old_one():
    """The reason `_wake` exists. Sitting in a 3-second dwell and dropping it
    to the floor must not take 3 seconds to apply — that makes the slider feel
    broken exactly while someone is dragging it to find a value.
    """
    controller = StubController()
    sender = ar.BulbSender(controller, min_dwell_ms=3000)
    try:
        sender.queue(("hsv", 10.0, 100.0, 50.0))
        deadline = time.time() + 3.0
        while not controller.sent and time.time() < deadline:
            time.sleep(0.02)
        assert controller.sent, "first send should not be delayed"

        # Now it is sitting in a 3s dwell before it will send again.
        sender.queue(("hsv", 200.0, 100.0, 50.0))
        time.sleep(0.15)
        assert len(controller.sent) == 1, "still inside the long dwell"

        started = time.time()
        sender.set_min_dwell(ar.MIN_DWELL_FLOOR_MS)
        deadline = time.time() + 2.0
        while len(controller.sent) < 2 and time.time() < deadline:
            time.sleep(0.02)
        elapsed = time.time() - started

        assert len(controller.sent) >= 2, "the lowered dwell never took effect"
        assert elapsed < 1.5, (
            f"took {elapsed:.2f}s to apply a lowered dwell — it waited out the old one")
    finally:
        sender.stop()


def test_raising_the_dwell_mid_wait_extends_rather_than_fires_early():
    """The wake is a re-evaluation, not a 'send now' signal."""
    controller = StubController()
    sender = ar.BulbSender(controller, min_dwell_ms=ar.MIN_DWELL_FLOOR_MS)
    try:
        sender.queue(("hsv", 10.0, 100.0, 50.0))
        deadline = time.time() + 2.0
        while not controller.sent and time.time() < deadline:
            time.sleep(0.02)
        assert controller.sent

        sender.set_min_dwell(1500)
        sender.queue(("hsv", 200.0, 100.0, 50.0))
        time.sleep(0.4)
        assert len(controller.sent) == 1, "raising the dwell should hold the next send back"
    finally:
        sender.stop()


# ------------------------------------------------------------ session level
def test_set_session_min_dwell_applies_to_the_live_session(fake_config, fake_tuya, presets_isolation):
    controller = bm.get_controller("bulb-1")
    session = _live_session(controller)
    ar._sessions["bulb-1"] = session
    try:
        result = ar.set_session_min_dwell("bulb-1", 75)
        assert result["applied"] is True
        assert result["min_dwell_ms"] == 75
        assert session.sender.min_dwell_ms == 75
        assert session.status()["sender"]["min_dwell_ms"] == 75
    finally:
        session.stop()
        ar._sessions.pop("bulb-1", None)


def test_the_tuned_value_survives_a_restart(fake_config, fake_tuya, presets_isolation):
    """The value someone lands on by ear IS the setting. Losing it on the next
    restart would make every tuning session disposable."""
    audio_presets.save_last_session("bulb-1", {
        "device_index": 0, "mode": "vu_meter", "sensitivity": 1.0,
        "monochrome_hue": 280.0, "n_bands": 3, "min_dwell_ms": 90,
    })
    ar.set_session_min_dwell("bulb-1", 165)
    assert audio_presets.load_last_session("bulb-1")["config"]["min_dwell_ms"] == 165


def test_saving_works_with_no_session_running(fake_config, fake_tuya, presets_isolation):
    """The slider still means something with nothing playing: it is the value
    the next session starts with."""
    audio_presets.save_last_session("bulb-1", {"device_index": 0, "min_dwell_ms": 90})
    result = ar.set_session_min_dwell("bulb-1", 120)
    assert result["applied"] is False, "nothing live to apply it to"
    assert result["min_dwell_ms"] == 120
    assert audio_presets.load_last_session("bulb-1")["config"]["min_dwell_ms"] == 120


def test_out_of_range_values_are_rejected_not_clamped_at_the_api_boundary(
        fake_config, fake_tuya, presets_isolation, check_all):
    """The sender clamps defensively, but a caller asking for something silly
    should be told, not quietly given something else."""
    def _rejects(value):
        with pytest.raises(ar.AudioConfigError):
            ar.set_session_min_dwell("bulb-1", value)

    check_all([0, -50, ar.MIN_DWELL_FLOOR_MS - 1, ar.MIN_DWELL_MS_CEILING + 1, "fast"],
              _rejects, label="bad dwell")


# ---------------------------------------------------------------- API level
def test_route_changes_dwell_without_stopping_the_session(client, presets_isolation):
    controller = bm.get_controller("bulb-1")
    session = _live_session(controller)
    ar._sessions["bulb-1"] = session
    try:
        r = client.post("/api/devices/bulb-1/audio-reactive/min-dwell", json={"min_dwell_ms": 65})
        assert r.status_code == 200, r.text
        assert r.json()["min_dwell_ms"] == 65
        assert r.json()["applied"] is True

        status = client.get("/api/devices/bulb-1/audio-reactive/status").json()
        assert status["sender"]["min_dwell_ms"] == 65
        assert status["active"] is True, "the session must still be running"
    finally:
        session.stop()
        ar._sessions.pop("bulb-1", None)


def test_route_rejects_an_out_of_range_dwell_with_400(client, presets_isolation):
    r = client.post("/api/devices/bulb-1/audio-reactive/min-dwell", json={"min_dwell_ms": 5})
    assert r.status_code == 400
    assert "floor" in r.text.lower()


def test_route_404s_on_an_unknown_device(client, presets_isolation):
    r = client.post("/api/devices/nope/audio-reactive/min-dwell", json={"min_dwell_ms": 90})
    assert r.status_code == 404
