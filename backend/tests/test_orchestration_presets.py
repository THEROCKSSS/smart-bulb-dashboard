"""Orchestration preset save/load (Week 1 Phase C, Section 6): bundles a
group audio-reactive role_mode + per-bulb overrides (hue_offsets /
brightness_scales / band_assignments) under a friendly name so a favorite
multi-bulb arrangement can be re-applied with one call. Exercised through
the real /api/orchestration-presets* routes and through
/api/groups/{id}/audio-reactive/start's `orchestration_preset_id` field.
"""


def test_orchestration_presets_start_empty(client):
    assert client.get("/api/orchestration-presets").json() == []


def test_save_and_get_orchestration_preset(client):
    body = {
        "id": "chase-3", "name": "3-bulb chase", "role_mode": "phase_offset",
        "hue_offsets": [0.0, 120.0, 240.0],
    }
    resp = client.post("/api/orchestration-presets", json=body)
    assert resp.status_code == 200
    saved = resp.json()
    assert saved["role_mode"] == "phase_offset"
    assert saved["hue_offsets"] == [0.0, 120.0, 240.0]

    fetched = client.get("/api/orchestration-presets/chase-3").json()
    assert fetched["name"] == "3-bulb chase"

    listed = client.get("/api/orchestration-presets").json()
    assert len(listed) == 1


def test_save_orchestration_preset_rejects_unknown_role_mode(client):
    resp = client.post("/api/orchestration-presets", json={"id": "x", "name": "x", "role_mode": "not-a-mode"})
    assert resp.status_code == 400


def test_get_unknown_preset_404s(client):
    assert client.get("/api/orchestration-presets/nope").status_code == 404


def test_delete_orchestration_preset(client):
    client.post("/api/orchestration-presets", json={"id": "chase-3", "name": "3-bulb chase", "role_mode": "unison"})
    del_resp = client.delete("/api/orchestration-presets/chase-3")
    assert del_resp.status_code == 200
    assert client.get("/api/orchestration-presets").json() == []


def test_group_audio_reactive_start_applies_preset_overrides(client, fake_config, monkeypatch):
    import audio_reactive as ar_module

    client.post("/api/orchestration-presets", json={
        "id": "wave-preset", "name": "Living room wave", "role_mode": "wave",
        "brightness_scales": [1.0, 0.5], "wave_period_ticks": 20,
    })

    captured = {}

    def fake_start_group_session(group_id, controllers, device_index, mode, role_mode,
                                  sensitivity, monochrome_hue, min_dwell_ms,
                                  hue_offsets=None, brightness_scales=None, band_assignments=None,
                                  mirror_center_hue=0.0, wave_period_ticks=40):
        captured.update(role_mode=role_mode, brightness_scales=brightness_scales,
                        wave_period_ticks=wave_period_ticks)
        return object()

    monkeypatch.setattr(ar_module, "validate_device_index", lambda idx: (True, None))
    monkeypatch.setattr(ar_module, "start_group_session", fake_start_group_session)

    resp = client.post("/api/groups/all/audio-reactive/start", json={
        "device_index": 0, "orchestration_preset_id": "wave-preset",
    })
    assert resp.status_code == 200
    assert resp.json()["role_mode"] == "wave"
    assert captured["role_mode"] == "wave"
    assert captured["brightness_scales"] == [1.0, 0.5]
    assert captured["wave_period_ticks"] == 20


def test_group_audio_reactive_start_explicit_fields_win_over_preset(client, fake_config, monkeypatch):
    import audio_reactive as ar_module

    client.post("/api/orchestration-presets", json={
        "id": "wave-preset", "name": "Living room wave", "role_mode": "wave",
        "brightness_scales": [1.0, 0.5],
    })

    captured = {}

    def fake_start_group_session(group_id, controllers, device_index, mode, role_mode,
                                  sensitivity, monochrome_hue, min_dwell_ms,
                                  hue_offsets=None, brightness_scales=None, band_assignments=None,
                                  mirror_center_hue=0.0, wave_period_ticks=40):
        captured.update(role_mode=role_mode, brightness_scales=brightness_scales)
        return object()

    monkeypatch.setattr(ar_module, "validate_device_index", lambda idx: (True, None))
    monkeypatch.setattr(ar_module, "start_group_session", fake_start_group_session)

    resp = client.post("/api/groups/all/audio-reactive/start", json={
        "device_index": 0, "orchestration_preset_id": "wave-preset",
        "role_mode": "mirror", "brightness_scales": [0.2, 0.2],
    })
    assert resp.status_code == 200
    assert captured["role_mode"] == "mirror"
    assert captured["brightness_scales"] == [0.2, 0.2]


def test_group_audio_reactive_start_unknown_preset_404s(client, fake_config):
    resp = client.post("/api/groups/all/audio-reactive/start", json={
        "device_index": 0, "orchestration_preset_id": "does-not-exist",
    })
    assert resp.status_code == 404
