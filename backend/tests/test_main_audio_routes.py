"""API-level integration tests for the Week 1 Phase D audio-reactive
session-management routes added to main.py: config validation (400),
session presets, resume-last, safety endpoints, mode-info, lightshow, and
the group/solo conflict check (409). Uses the shared `client` fixture from
conftest.py (fake config + fake tinytuya device), so no real bulb is ever
contacted -- the audio *capture* side does use this machine's real
sounddevice input in the one end-to-end start/stop test, since this
environment genuinely has real input devices available.
"""
import time

import audio_reactive
import sounddevice as sd


def _real_input_device_index():
    """This test environment has real audio hardware (unlike a typical CI
    container), so these integration tests use a genuinely valid input
    device index rather than mocking sounddevice out entirely -- picks the
    first device reporting at least one input channel."""
    for i, d in enumerate(sd.query_devices()):
        if d.get("max_input_channels", 0) > 0:
            return i
    return None


def test_audio_config_validation_returns_400_for_bad_n_bands(client):
    resp = client.post("/api/devices/bulb-1/audio-reactive/start", json={
        "device_index": 0, "mode": "band_fixed", "n_bands": 999,
    })
    assert resp.status_code == 400
    assert "n_bands" in resp.json()["detail"]


def test_audio_config_validation_returns_400_for_bad_min_dwell_ms(client):
    resp = client.post("/api/devices/bulb-1/audio-reactive/start", json={
        "device_index": 0, "mode": "band_fixed", "min_dwell_ms": 1,
    })
    assert resp.status_code == 400
    assert "min_dwell_ms" in resp.json()["detail"]


def test_audio_config_validation_rejects_flash_heavy_mode_with_disable_toggle(client):
    resp = client.post("/api/devices/bulb-1/audio-reactive/start", json={
        "device_index": 0, "mode": "strobe_on_drop", "disable_flash_heavy": True,
    })
    assert resp.status_code == 400
    assert "flash-heavy" in resp.json()["detail"]


def test_audio_reactive_start_stop_round_trip_with_real_capture_device(client):
    """End-to-end: start a real solo audio-reactive session against this
    machine's real default input device, confirm the response reports
    exactly what was applied, then stop it cleanly."""
    resp = client.post("/api/devices/bulb-1/audio-reactive/start", json={
        "device_index": _real_input_device_index(), "mode": "vu_meter", "n_bands": 3, "min_dwell_ms": 90,
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["device_id"] == "bulb-1"
    assert body["mode"] == "vu_meter"
    assert body["min_dwell_ms"] == 90

    status_resp = client.get("/api/devices/bulb-1/audio-reactive/status")
    assert status_resp.json()["active"] is True

    stop_resp = client.post("/api/devices/bulb-1/audio-reactive/stop")
    assert stop_resp.status_code == 200
    time.sleep(0.1)
    status_after = client.get("/api/devices/bulb-1/audio-reactive/status")
    assert status_after.json()["active"] is False


def test_audio_reactive_start_rate_limited_after_too_many_calls(client):
    key = "start:bulb-1"
    audio_reactive._rate_limit_hits.pop(key, None)
    last_resp = None
    for _ in range(audio_reactive.RATE_LIMIT_MAX_CALLS + 2):
        last_resp = client.post("/api/devices/bulb-1/audio-reactive/start", json={
            "device_index": 0, "mode": "band_fixed", "n_bands": 999,  # invalid config -> fast 400, no real stream
        })
    assert last_resp.status_code == 429
    audio_reactive._rate_limit_hits.pop(key, None)


def test_session_preset_save_list_delete_via_api(client):
    save_resp = client.post("/api/devices/bulb-1/audio-reactive/session-presets", json={
        "name": "My Preset", "device_index": 2, "mode": "spectrum_gradient", "n_bands": 6,
    })
    assert save_resp.status_code == 200
    preset = save_resp.json()
    assert preset["name"] == "My Preset"
    assert preset["config"]["mode"] == "spectrum_gradient"

    list_resp = client.get("/api/audio/session-presets")
    assert list_resp.status_code == 200
    assert any(p["id"] == preset["id"] for p in list_resp.json())

    del_resp = client.delete(f"/api/audio/session-presets/{preset['id']}")
    assert del_resp.status_code == 200
    del_again = client.delete(f"/api/audio/session-presets/{preset['id']}")
    assert del_again.status_code == 404


def test_resume_last_session_404_when_none_saved(client):
    resp = client.post("/api/devices/bulb-2/audio-reactive/resume-last")
    assert resp.status_code == 404


def test_audio_modes_info_reports_flash_heavy_metadata(client):
    resp = client.get("/api/audio/modes/info")
    assert resp.status_code == 200
    body = resp.json()
    by_mode = {m["mode"]: m for m in body["modes"]}
    assert by_mode["strobe_on_drop"]["flash_heavy"] is True
    assert by_mode["band_fixed"]["flash_heavy"] is False
    assert body["hard_max_flash_rate_hz"] == 3.0
    assert "WCAG" in body["flash_rate_standard"]


def test_safety_max_flash_rate_endpoint_clamps_to_hard_ceiling(client):
    resp = client.post("/api/audio/safety/max-flash-rate", json={"max_flash_rate_hz": 999})
    assert resp.status_code == 200
    assert resp.json()["max_flash_rate_hz"] == 3.0


def test_safety_disable_flash_heavy_toggle_endpoint(client):
    resp = client.post("/api/audio/safety/disable-flash-heavy", json={"disabled": True})
    assert resp.status_code == 200
    assert resp.json()["disable_flash_heavy"] is True
    # reset for other tests
    client.post("/api/audio/safety/disable-flash-heavy", json={"disabled": False})


def test_reduced_motion_profile_endpoint(client):
    resp = client.get("/api/audio/safety/reduced-motion-profile")
    assert resp.status_code == 200
    body = resp.json()
    assert body["disable_flash_heavy"] is True


def test_group_conflict_returns_409_and_force_overrides(client, fake_config):
    # Start a solo session on bulb-1 first (part of the "all" group).
    start_resp = client.post("/api/devices/bulb-1/audio-reactive/start", json={
        "device_index": _real_input_device_index(), "mode": "vu_meter",
    })
    assert start_resp.status_code == 200
    try:
        conflict_resp = client.post("/api/groups/all/audio-reactive/start", json={
            "device_index": _real_input_device_index(), "mode": "vu_meter",
        })
        assert conflict_resp.status_code == 409

        forced_resp = client.post("/api/groups/all/audio-reactive/start", json={
            "device_index": _real_input_device_index(), "mode": "vu_meter", "force": True,
        })
        assert forced_resp.status_code == 200
    finally:
        client.post("/api/devices/bulb-1/audio-reactive/stop")
        client.post("/api/groups/all/audio-reactive/stop")
        time.sleep(0.1)


def test_lightshow_export_with_no_capture_returns_400(client):
    resp = client.post("/api/devices/bulb-2/lightshow/export", json={"name": "Nothing Captured"})
    assert resp.status_code == 400


def test_lightshow_replay_unknown_id_returns_404(client):
    resp = client.post("/api/devices/bulb-1/lightshow/replay", json={"lightshow_id": "nope"})
    assert resp.status_code == 404
