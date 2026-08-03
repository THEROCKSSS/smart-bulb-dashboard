"""Live-editable session settings, and mood presets that don't break the bridge.

Two things are tested here, and they are the same underlying problem:

1. Changing mode / sensitivity / beat sensitivity / dwell used to require a
   stop-change-start cycle. That killed the bulb, reset the tempo tracker's
   lock, and made tuning by ear impractical.

2. Applying a genre preset went through `start_session`, whose `source_kind`
   defaults to "device". On a bridge session that silently switched capture
   back to local devices -- and inside the container there are none, so the
   session ran, reported itself running, and never reacted to a sound. The
   exact silent no-op the bridge exists to eliminate.

Run with:
    pytest backend/tests/test_live_session_settings.py -v
"""
import os
import sys
import time

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import audio_presets  # noqa: E402
import audio_reactive as ar  # noqa: E402
import bulb_manager as bm  # noqa: E402
import capture_sources  # noqa: E402


@pytest.fixture
def presets_isolation(tmp_path, monkeypatch):
    monkeypatch.setattr(audio_presets, "LAST_SESSION_PATH", str(tmp_path / "last.json"))
    monkeypatch.setattr(audio_presets, "SESSION_PRESETS_PATH", str(tmp_path / "presets.json"))
    return tmp_path


def _live_session(controller, **kwargs):
    block = np.zeros((ar.DEFAULT_HOP_SIZE, 1), dtype=np.float32)

    def factory(callback, channels):
        return capture_sources.CallableSource([block], callback, interval_s=0.02, loop=True)

    session = ar.AudioSession(controller, device_index=None, source_kind="callable",
                              source_factory=factory, **kwargs)
    session.start()
    deadline = time.time() + 3.0
    while not session.is_alive() and time.time() < deadline:
        time.sleep(0.02)
    return session


@pytest.fixture
def live(fake_config, fake_tuya, presets_isolation):
    controller = bm.get_controller("bulb-1")
    session = _live_session(controller, mode="band_fixed", sensitivity=1.0, min_dwell_ms=200)
    ar._sessions["bulb-1"] = session
    yield session
    session.stop()
    ar._sessions.pop("bulb-1", None)


# ------------------------------------------------------- live editing
def test_every_advertised_setting_actually_applies(live, check_all):
    """LIVE_SETTINGS is a promise. Each entry must really reach the session."""
    ar.update_session_settings(
        "bulb-1", mode="vu_meter", sensitivity=2.5, beat_sensitivity="aggressive",
        min_dwell_ms=120, monochrome_hue=200.0, n_bands=8)

    expectations = [
        ("mode", lambda: live.mode, "vu_meter"),
        ("sensitivity", lambda: live.ctx["sensitivity"], 2.5),
        ("beat_sensitivity", lambda: live.tempo.beat_sensitivity, "aggressive"),
        ("min_dwell_ms", lambda: live.sender.min_dwell_ms, 120),
        ("monochrome_hue", lambda: live.ctx["monochrome_hue"], 200.0),
        ("n_bands", lambda: live.n_bands, 8),
    ]

    def _check(case):
        name, getter, want = case
        assert getter() == want, f"{name} did not apply (got {getter()!r}, wanted {want!r})"

    check_all(expectations, _check, label="setting", name=lambda c: c[0])


def test_changing_one_setting_leaves_the_others_alone(live):
    """"When I edit the mode it sticks to editing that mode." A partial update
    must not quietly reset everything it didn't mention."""
    ar.update_session_settings("bulb-1", sensitivity=3.0, min_dwell_ms=150,
                               beat_sensitivity="subtle")
    ar.update_session_settings("bulb-1", mode="energy_contour")

    assert live.mode == "energy_contour"
    assert live.ctx["sensitivity"] == 3.0, "sensitivity was reset by a mode change"
    assert live.sender.min_dwell_ms == 150, "dwell was reset by a mode change"
    assert live.tempo.beat_sensitivity == "subtle", "beat sensitivity was reset by a mode change"


def test_the_session_keeps_running_across_edits(live):
    ar.update_session_settings("bulb-1", mode="vu_meter", sensitivity=2.0)
    assert live.is_alive(), "editing a setting must not tear the session down"
    assert live.status()["active"] is True


def test_n_bands_change_rebuilds_the_band_edges(live):
    before = list(live.band_edges)
    ar.update_session_settings("bulb-1", n_bands=12)
    assert live.n_bands == 12
    assert live.band_edges != before
    assert len(live.band_edges) != len(before)


def test_stereo_flag_follows_the_mode(live):
    ar.update_session_settings("bulb-1", mode="stereo_split")
    assert live._stereo is True
    ar.update_session_settings("bulb-1", mode="band_fixed")
    assert live._stereo is False


def test_edits_persist_for_the_next_session(live):
    audio_presets.save_last_session("bulb-1", {"device_index": 0, "mode": "band_fixed",
                                               "sensitivity": 1.0, "min_dwell_ms": 90})
    ar.update_session_settings("bulb-1", mode="vu_meter", sensitivity=2.2, min_dwell_ms=140)
    cfg = audio_presets.load_last_session("bulb-1")["config"]
    assert cfg["mode"] == "vu_meter"
    assert cfg["sensitivity"] == 2.2
    assert cfg["min_dwell_ms"] == 140


def test_settings_are_saved_even_with_nothing_running(fake_config, fake_tuya, presets_isolation):
    audio_presets.save_last_session("bulb-1", {"device_index": 0, "mode": "band_fixed"})
    result = ar.update_session_settings("bulb-1", mode="vu_meter")
    assert result["live"] is False, "must not claim a bulb changed when none is running"
    assert audio_presets.load_last_session("bulb-1")["config"]["mode"] == "vu_meter"


def test_bad_values_are_rejected_rather_than_silently_clamped(live, check_all):
    bad = [
        {"mode": "not_a_mode"},
        {"beat_sensitivity": "extremely"},
        {"min_dwell_ms": 5},
        {"min_dwell_ms": 999999},
        {"n_bands": 1},
        {"n_bands": 99},
    ]

    def _rejects(kwargs):
        with pytest.raises(ar.AudioConfigError):
            ar.update_session_settings("bulb-1", **kwargs)

    check_all(bad, _rejects, label="bad setting", name=lambda k: str(k))


def test_an_unknown_setting_is_refused_not_ignored(live):
    """Silently dropping a setting someone thinks they just changed is the
    worst available outcome."""
    with pytest.raises(ar.AudioConfigError) as exc:
        ar.update_session_settings("bulb-1", warmup_s=5.0)
    assert "warmup_s" in str(exc.value)


# ------------------------------------------- presets vs. the audio bridge
def test_applying_a_preset_keeps_a_bridge_session_on_the_bridge(client, presets_isolation,
                                                                monkeypatch):
    """The bug this file exists for.

    `apply-preset` called `start_session` without `source_kind`, which defaults
    to "device". Applying a mood on a bridge session therefore moved capture to
    a local device -- of which the container has none.
    """
    import audio_bridge
    controller = bm.get_controller("bulb-1")
    session = _live_session(controller)
    session.source_kind = "bridge"
    ar._sessions["bulb-1"] = session
    monkeypatch.setattr(audio_bridge, "get_server", lambda: object())
    try:
        preset = ar.AUDIO_GENRE_PRESETS[0]
        r = client.post("/api/devices/bulb-1/audio-reactive/apply-preset",
                        json={"preset_id": preset["id"], "device_index": 0})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["source"] == "bridge", "the preset moved the session off the bridge"
        assert body["restarted"] is False, "a live session should take a preset without a restart"
        assert ar.get_active_session("bulb-1") is session, "the session was torn down"
        assert session.mode == preset["mode"]
    finally:
        session.stop()
        ar._sessions.pop("bulb-1", None)


def test_applying_a_preset_refuses_when_the_bridge_is_gone(client, presets_isolation, monkeypatch):
    """Better to refuse than to hand back a session with no possible audio."""
    import audio_bridge
    controller = bm.get_controller("bulb-1")
    session = _live_session(controller)
    session.source_kind = "bridge"
    ar._sessions["bulb-1"] = session
    monkeypatch.setattr(audio_bridge, "get_server", lambda: None)
    try:
        r = client.post("/api/devices/bulb-1/audio-reactive/apply-preset",
                        json={"preset_id": ar.AUDIO_GENRE_PRESETS[0]["id"], "device_index": 0})
        assert r.status_code == 409
        assert "bridge" in r.text.lower()
    finally:
        session.stop()
        ar._sessions.pop("bulb-1", None)


def test_a_preset_applied_with_no_session_still_starts_one(client, presets_isolation):
    preset = ar.AUDIO_GENRE_PRESETS[0]
    r = client.post("/api/devices/bulb-1/audio-reactive/apply-preset",
                    json={"preset_id": preset["id"], "device_index": 0})
    assert r.status_code == 200, r.text
    assert r.json()["restarted"] is True
    try:
        assert ar.get_active_session("bulb-1") is not None
    finally:
        s = ar._sessions.pop("bulb-1", None)
        if s:
            s.stop()


# ------------------------------------------------------------- API shape
def test_route_applies_a_partial_update(client, presets_isolation):
    controller = bm.get_controller("bulb-1")
    session = _live_session(controller)
    ar._sessions["bulb-1"] = session
    try:
        r = client.post("/api/devices/bulb-1/audio-reactive/settings",
                        json={"mode": "vu_meter", "sensitivity": 1.8})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["live"] is True
        assert body["applied"]["mode"] == "vu_meter"
        assert session.mode == "vu_meter"

        status = client.get("/api/devices/bulb-1/audio-reactive/status").json()
        assert status["active"] is True, "the session must survive the edit"
        assert status["mode"] == "vu_meter"
    finally:
        session.stop()
        ar._sessions.pop("bulb-1", None)


def test_route_rejects_an_empty_body(client, presets_isolation):
    r = client.post("/api/devices/bulb-1/audio-reactive/settings", json={})
    assert r.status_code == 400
    assert "no settings" in r.text.lower()


def test_route_rejects_a_bad_mode_with_400(client, presets_isolation):
    r = client.post("/api/devices/bulb-1/audio-reactive/settings", json={"mode": "nope"})
    assert r.status_code == 400
