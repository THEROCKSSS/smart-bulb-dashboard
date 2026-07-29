"""Scene-apply endpoint: POST /api/devices/{device_id}/scenes/apply.

Exercises real scene definitions from scenes_presets.SCENES against the
fake device layer, so a real color-mode scene must reach set_colour, and a
real white-mode scene must reach set_white/set_colourtemp.
"""


def test_scene_apply_colour_scene_sets_power_and_rgb(client, fake_tuya):
    resp = client.post("/api/devices/bulb-1/scenes/apply", json={"scene_id": "movie_night"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "movie_night"
    assert body["rgb"] == [40, 50, 120]

    dev = fake_tuya["dev-fake-1"]
    assert ("turn_on",) in dev.calls
    assert ("set_colour", 40, 50, 120) in dev.calls


def test_scene_apply_white_scene_uses_white_mode_path(client, fake_tuya):
    resp = client.post("/api/devices/bulb-1/scenes/apply", json={"scene_id": "reading"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "reading"
    assert body["mode"] == "white"

    dev = fake_tuya["dev-fake-1"]
    assert ("turn_on",) in dev.calls
    assert any(c[0] == "set_white" for c in dev.calls)
    # color_temp_pct=60 -> raw = (60/100)*1000 = 600
    assert ("set_colourtemp", 600) in dev.calls
    # colour scenes should never be hit for a white-mode scene
    assert not any(c[0] == "set_colour" for c in dev.calls)


def test_scene_apply_is_logged_to_history(client, fake_tuya):
    client.post("/api/devices/bulb-1/scenes/apply", json={"scene_id": "party"})
    hist = client.get("/api/devices/bulb-1/history").json()
    scene_entries = [h for h in hist if h["action"] == "scene"]
    assert len(scene_entries) == 1
    assert scene_entries[0]["params"] == {"scene_id": "party"}


def test_scene_apply_unknown_scene_returns_400(client, fake_tuya):
    resp = client.post("/api/devices/bulb-1/scenes/apply", json={"scene_id": "does-not-exist"})
    assert resp.status_code == 400
    dev = fake_tuya.get("dev-fake-1")
    # device layer must never be constructed/called for an invalid scene id
    assert dev is None


def test_scene_apply_unknown_device_returns_404(client):
    resp = client.post("/api/devices/nope/scenes/apply", json={"scene_id": "party"})
    assert resp.status_code == 404
