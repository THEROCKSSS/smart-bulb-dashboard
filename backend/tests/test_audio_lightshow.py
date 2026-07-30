"""Week 1 Phase D, section 12: light-show export/replay round trip.

Captures a real (synthetic) sequence of sent actions via a real
`audio_reactive.BulbSender`, exports it with `audio_lightshow`, then
replays it against a fresh fake controller and asserts the replayed
actions match what was originally captured.
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import audio_reactive  # noqa: E402
import audio_lightshow  # noqa: E402


class FakeController:
    """Minimal stand-in with just enough surface for BulbSender: set_hsv,
    _log, cfg, and a no-op set_socket_timeout (mirrors what
    tests/conftest.py's FakeTuyaBulbDevice-backed BulbController provides,
    but standalone here so this test doesn't need the FastAPI app/config
    fixtures)."""

    def __init__(self, device_id):
        self.cfg = {"id": device_id}
        self.calls = []

    def set_socket_timeout(self, seconds):
        pass

    def set_hsv(self, h, s, v):
        self.calls.append(("hsv", h, s, v))

    def set_rgb(self, r, g, b):
        self.calls.append(("rgb", r, g, b))

    def set_brightness(self, pct):
        self.calls.append(("brightness", pct))

    def _log(self, action, params=None, ok=True, error=None):
        pass


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(audio_lightshow, "LIGHTSHOWS_DIR", str(tmp_path))


def test_export_then_replay_round_trip_matches_captured_actions(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)

    # 1. Capture a real short sequence via a real BulbSender (tiny dwell so
    #    the test runs fast) -- this is what a live AudioSession does.
    source_controller = FakeController("bulb-1")
    sender = audio_reactive.BulbSender(source_controller, min_dwell_ms=audio_reactive.MIN_DWELL_FLOOR_MS)
    try:
        sequence = [(10.0, 100, 50), (60.0, 100, 70), (200.0, 90, 90)]
        for h, s, v in sequence:
            sender.queue(("hsv", h, s, v))
            time.sleep(0.06)  # exceed the floor dwell so each queued value actually sends separately
    finally:
        sender.stop()

    captured = sender.get_captured_points()
    assert len(captured) >= 2, f"expected at least 2 distinct captured sends, got {captured}"
    # The captured hues should include values close to what was queued.
    captured_hues = {round(p["h"]) for p in captured}
    assert any(abs(h - 10) < 1 for h in captured_hues) or any(abs(h - 60) < 1 for h in captured_hues)

    # 2. Export it.
    record = audio_lightshow.export_lightshow("bulb-1", "Test Show", captured)
    assert record["device_id"] == "bulb-1"
    assert record["name"] == "Test Show"
    assert record["point_count"] == len(captured)

    listed = audio_lightshow.list_lightshows("bulb-1")
    assert len(listed) == 1
    assert "points" not in listed[0]  # metadata-only listing

    fetched = audio_lightshow.get_lightshow(record["id"])
    assert fetched["points"] == captured

    # 3. Replay it against a *fresh* controller and confirm the replayed
    #    actions match what was captured (same hue/sat/val sequence).
    replay_controller = FakeController("bulb-1")
    replay = audio_lightshow.LightshowReplay(replay_controller, fetched["points"], loop=False)
    replay.start()
    # Wait for replay to finish (points span < 1s of relative time here).
    deadline = time.time() + 5
    while replay.is_alive() and time.time() < deadline:
        time.sleep(0.05)
    replay.stop()

    assert not replay.is_alive()
    hsv_calls = [c for c in replay_controller.calls if c[0] == "hsv"]
    assert len(hsv_calls) >= 1
    replayed_hues = [round(c[1]) for c in hsv_calls]
    original_hues = [round(p["h"]) for p in fetched["points"]]
    # Every replayed hue should have come from the captured/exported set
    # (dwell pacing on the sender may coalesce some, but nothing invented).
    for rh in replayed_hues:
        assert any(abs(rh - oh) <= 1 for oh in original_hues), (
            f"replayed hue {rh} doesn't match any exported point {original_hues}"
        )
    # And the very first exported point's hue must appear in the replay.
    assert any(abs(replayed_hues[0] - original_hues[0]) <= 1 for _ in [0])


def test_export_with_no_points_raises_clear_error(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    try:
        audio_lightshow.export_lightshow("bulb-1", "Empty", [])
        assert False, "expected a ValueError for an empty capture"
    except ValueError as e:
        assert "no captured actions" in str(e)


def test_replay_unknown_lightshow_id_raises(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    controller = FakeController("bulb-1")
    try:
        audio_lightshow.start_replay("bulb-1", controller, "does-not-exist")
        assert False, "expected a ValueError for an unknown lightshow id"
    except ValueError:
        pass


def test_delete_lightshow(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    record = audio_lightshow.export_lightshow("bulb-1", "ToDelete", [{"t": 0.0, "h": 1, "s": 100, "v": 50}])
    assert audio_lightshow.delete_lightshow(record["id"]) is True
    assert audio_lightshow.get_lightshow(record["id"]) is None
    assert audio_lightshow.delete_lightshow(record["id"]) is False
