"""The lighting mix survives edits, reload, preset recall, and storage failures."""
import pytest


def test_complete_stopped_mix_and_nullable_timing_can_be_recalled(client):
    path = "/api/devices/bulb-1/audio-reactive/settings"
    settings = {"max_duration_s": 120, "warmup_s": 2, "silence_auto_off": False,
                "auto_resume_grace_s": 5, "use_saved_calibration": False,
                "fallback_device_index": 7, "hop_size": 256, "window_size": 1024}
    assert client.post(path, json=settings).status_code == 200
    loaded = client.get(path).json()["settings"]
    assert all(loaded[key] == value for key, value in settings.items())
    assert client.post(path, json={"max_duration_s": None}).status_code == 200
    assert client.get(path).json()["settings"]["max_duration_s"] is None
    preset = client.post('/api/devices/bulb-1/audio-reactive/session-presets', json={"name": "Unlimited", "max_duration_s": None}).json()
    assert client.post(path, json={"max_duration_s": 300}).status_code == 200
    stored = client.get('/api/audio/session-presets').json()[0]
    assert stored['id'] == preset['id']
    assert stored['config']['max_duration_s'] is None
    assert client.post(path, json=stored['config']).status_code == 200
    assert client.get(path).json()['settings']['max_duration_s'] is None


def test_gentle_mix_persists_a_restartable_mode(client):
    import audio_reactive
    path = "/api/devices/bulb-1/audio-reactive/settings"
    assert client.post(path, json={"mode": "strobe_on_drop"}).status_code == 200
    assert client.post(path, json={"disable_flash_heavy": True}).status_code == 200
    settings = client.get(path).json()["settings"]
    assert settings["mode"] == "weighted_blend"
    audio_reactive.validate_start_config(settings["n_bands"], settings["min_dwell_ms"],
        mode=settings["mode"], disable_flash_heavy=settings["disable_flash_heavy"])


def test_three_mixer_gains_shape_multiband_energy_and_meter():
    from audio_signal import SignalConditioner
    conditioner = SignalConditioner(band_gains=[2, 1, 0])
    out = conditioner.apply_band_gains({"energies": [1, 1, 1], "extra_energies": [1] * 5})
    assert out["energies"] == [2, 1, 0]
    assert out["extra_energies"] == [2, 1.5, 1, 0.5, 0]
    assert out["extra_fractions"] == pytest.approx([.4, .3, .2, .1, 0])


def test_stopped_mixer_saves_complete_settings_and_keeps_each_bulb_separate(client):
    path = "/api/devices/bulb-1/audio-reactive/settings"
    mix = {"source": "bridge", "mode": "weighted_blend", "sensitivity": 1.7,
           "beat_sensitivity": "subtle", "band_gains": [1.8, 0.7, 1.2],
           "noise_gate_enabled": True, "noise_gate_floor": 0.004,
           "agc_enabled": True, "agc_target_rms": 0.12,
           "agc_attack_ms": 30, "agc_release_ms": 450,
           "smoothing_ms": 120, "brightness_min": 2, "brightness_max": 65}
    saved = client.post(path, json=mix)
    assert saved.status_code == 200, saved.text
    assert saved.json()["saved"] is True
    assert saved.json()["live"] is False
    # A second bulb cannot overwrite the first bulb's saved mix.
    assert client.post("/api/devices/bulb-2/audio-reactive/settings",
                       json={"sensitivity": 0.6}).status_code == 200
    loaded = client.get(path)
    assert loaded.status_code == 200
    for key, value in mix.items():
        assert loaded.json()["settings"][key] == value, key


def test_invalid_mixer_edit_preserves_previous_settings(client):
    path = "/api/devices/bulb-1/audio-reactive/settings"
    assert client.post(path, json={"sensitivity": 1.5}).status_code == 200
    response = client.post(path, json={"sensitivity": 2, "band_gains": [-2, 1, 1]})
    assert response.status_code in (400, 422)
    assert client.get(path).json()["settings"]["sensitivity"] == 1.5


def test_failed_save_is_reported_and_previous_mix_is_readable(client, monkeypatch):
    import audio_presets
    path = "/api/devices/bulb-1/audio-reactive/settings"
    assert client.post(path, json={"sensitivity": 1.5}).status_code == 200
    def full_disk(*args, **kwargs):
        raise OSError("synthetic full disk")
    monkeypatch.setattr(audio_presets, "save_last_session", full_disk)
    response = client.post(path, json={"sensitivity": 2})
    assert response.status_code == 503
    assert client.get(path).json()["settings"]["sensitivity"] == 1.5


def test_named_mix_can_be_updated_and_recalled_with_its_source(client, monkeypatch):
    import audio_reactive
    created = client.post("/api/devices/bulb-1/audio-reactive/session-presets", json={
        "name": "Evening", "source": "bridge", "band_gains": [2, 0.8, 0.5],
        "smoothing_ms": 180, "brightness_max": 55, "beat_sensitivity": "subtle"})
    assert created.status_code == 200, created.text
    preset = created.json()
    update = client.patch("/api/audio/session-presets/" + preset["id"],
                          json={"name": "Quiet evening"})
    assert update.status_code == 200
    assert update.json()["config"]["band_gains"] == [2, 0.8, 0.5]
    seen = {}
    class Started:
        def confirmation(self):
            return {"active": True}
    def start(controller, **kwargs):
        seen.update(kwargs)
        return Started()
    monkeypatch.setattr(audio_reactive, "start_session", start)
    applied = client.post("/api/devices/bulb-1/audio-reactive/session-presets/apply",
                          json={"preset_id": preset["id"]})
    assert applied.status_code == 200, applied.text
    assert seen["source_kind"] == "bridge"
    assert seen["band_gains"] == [2, 0.8, 0.5]
    assert seen["smoothing_ms"] == 180
    assert seen["brightness_max"] == 55
