"""Per-bulb config overrides (Week 1 Phase C, Section 7):
  - `max_brightness_pct`: a per-bulb safety cap, enforced at both
    BulbController.set_brightness (white-mode path) and set_hsv (colour-mode
    path, since HSV brightness lives in the V component and bypasses
    set_brightness entirely) -- so it applies uniformly whether a bulb is
    driven manually or via an audio-reactive/orchestration session.
  - `hue_calibration_offset`: a per-bulb hue correction baked into set_hsv,
    for bulbs that render hue slightly differently from their siblings
    (matters most for group modes like `unison` where every bulb is asked
    for the identical hue).
  - `audio_reactive_eligible`: excludes a bulb from ever being
    auto-included when a GROUP audio-reactive session is started, even if
    it's a member of that group -- exercised through the real
    /api/groups/{id}/audio-reactive/start route.

Uses the same `client`/`fake_tuya`/`fake_config` fixtures as the rest of
this suite (see conftest.py) -- no real hardware or real config.json.
"""
import colorsys

import bulb_manager as bm


def _hsv_from_calls(dev):
    """Recover the (h, s, v) the fake device last received via set_colour,
    the same way BulbController.status()/_parse_dps effectively does."""
    _, r, g, b = [c for c in dev.calls if c[0] == "set_colour"][-1]
    h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
    return h * 360, s * 100, v * 100


# ------------------------------------------------------- max_brightness_pct
def test_set_brightness_is_clamped_by_per_bulb_cap(fake_tuya):
    cfg = {"id": "bulb-1", "device_id": "dev-fake-1", "local_key": "k", "ip": "10.0.0.11",
           "version": 3.3, "gamma": 1.0, "max_brightness_pct": 40}
    controller = bm.BulbController(cfg)
    controller.set_brightness(90)  # request way above the cap
    dev = fake_tuya["dev-fake-1"]
    # white-mode path: set_value(22, raw) where raw = (pct/100)*1000
    set_value_calls = [c for c in dev.calls if c[0] == "set_value"]
    assert set_value_calls, f"expected a set_value call, got {dev.calls}"
    _, dp, raw = set_value_calls[-1]
    assert dp == 22
    assert raw == 400, f"40% cap should clamp 90% request to raw 400, got {raw}"


def test_set_brightness_below_cap_is_unaffected(fake_tuya):
    cfg = {"id": "bulb-1", "device_id": "dev-fake-1", "local_key": "k", "ip": "10.0.0.11",
           "version": 3.3, "gamma": 1.0, "max_brightness_pct": 80}
    controller = bm.BulbController(cfg)
    controller.set_brightness(30)  # well under the cap
    dev = fake_tuya["dev-fake-1"]
    _, dp, raw = [c for c in dev.calls if c[0] == "set_value"][-1]
    assert raw == 300


def test_no_cap_configured_defaults_to_100(fake_tuya):
    cfg = {"id": "bulb-1", "device_id": "dev-fake-1", "local_key": "k", "ip": "10.0.0.11",
           "version": 3.3, "gamma": 1.0}
    controller = bm.BulbController(cfg)
    controller.set_brightness(100)
    dev = fake_tuya["dev-fake-1"]
    _, dp, raw = [c for c in dev.calls if c[0] == "set_value"][-1]
    assert raw == 1000


def test_set_hsv_brightness_is_also_clamped_by_cap(fake_tuya):
    # HSV brightness lives in the colour_data V component and bypasses
    # set_brightness -- the cap must be enforced here too or a group
    # audio-reactive session could blow right past a configured safety cap.
    cfg = {"id": "bulb-1", "device_id": "dev-fake-1", "local_key": "k", "ip": "10.0.0.11",
           "version": 3.3, "gamma": 1.0, "max_brightness_pct": 50}
    controller = bm.BulbController(cfg)
    controller.set_hsv(120.0, 100, 100)  # request full 100% brightness
    dev = fake_tuya["dev-fake-1"]
    _, _, v = _hsv_from_calls(dev)
    assert abs(v - 50) < 1.5, f"expected V clamped to ~50%, got {v}"


# ---------------------------------------------------- hue_calibration_offset
def test_set_hsv_applies_hue_calibration_offset(fake_tuya):
    cfg = {"id": "bulb-1", "device_id": "dev-fake-1", "local_key": "k", "ip": "10.0.0.11",
           "version": 3.3, "gamma": 1.0, "hue_calibration_offset": 15.0}
    controller = bm.BulbController(cfg)
    controller.set_hsv(100.0, 100, 100)
    dev = fake_tuya["dev-fake-1"]
    h, _, _ = _hsv_from_calls(dev)
    assert abs(h - 115.0) < 1.5, f"expected hue shifted by +15 deg to ~115, got {h}"


def test_set_hsv_calibration_offset_wraps_past_360(fake_tuya):
    cfg = {"id": "bulb-1", "device_id": "dev-fake-1", "local_key": "k", "ip": "10.0.0.11",
           "version": 3.3, "gamma": 1.0, "hue_calibration_offset": 20.0}
    controller = bm.BulbController(cfg)
    controller.set_hsv(350.0, 100, 100)
    dev = fake_tuya["dev-fake-1"]
    h, _, _ = _hsv_from_calls(dev)
    assert abs(h - 10.0) < 1.5, f"expected hue to wrap to ~10 deg (350+20-360), got {h}"


def test_no_calibration_configured_leaves_hue_unchanged(fake_tuya):
    cfg = {"id": "bulb-1", "device_id": "dev-fake-1", "local_key": "k", "ip": "10.0.0.11",
           "version": 3.3, "gamma": 1.0}
    controller = bm.BulbController(cfg)
    controller.set_hsv(200.0, 100, 100)
    dev = fake_tuya["dev-fake-1"]
    h, _, _ = _hsv_from_calls(dev)
    assert abs(h - 200.0) < 1.5


# ---------------------------------------------------- audio_reactive_eligible
def test_group_audio_reactive_start_excludes_ineligible_bulbs(client, fake_config, fake_tuya, monkeypatch):
    import audio_reactive as ar

    # Make bulb-2 ineligible for auto-inclusion in group sessions.
    for d in fake_config["devices"]:
        if d["id"] == "bulb-2":
            d["audio_reactive_eligible"] = False

    started = {}

    def fake_start_group_session(group_id, controllers, device_index, mode, role_mode,
                                  sensitivity, monochrome_hue, min_dwell_ms, *extra, **kw):
        started["controllers"] = controllers
        return object()

    monkeypatch.setattr(ar, "validate_device_index", lambda idx: (True, None))
    monkeypatch.setattr(ar, "start_group_session", fake_start_group_session)

    resp = client.post("/api/groups/all/audio-reactive/start", json={"device_index": 0})
    assert resp.status_code == 200
    body = resp.json()
    assert body["bulb_count"] == 1

    ids = [c.cfg["id"] for c in started["controllers"]]
    assert ids == ["bulb-1"], f"bulb-2 is audio_reactive_eligible=False and must be excluded, got {ids}"


def test_group_audio_reactive_start_all_ineligible_returns_400(client, fake_config, monkeypatch):
    import audio_reactive as ar

    for d in fake_config["devices"]:
        d["audio_reactive_eligible"] = False

    monkeypatch.setattr(ar, "validate_device_index", lambda idx: (True, None))
    resp = client.post("/api/groups/all/audio-reactive/start", json={"device_index": 0})
    assert resp.status_code == 400


def test_group_audio_reactive_start_defaults_eligible_true_when_unset(client, fake_config, fake_tuya, monkeypatch):
    # Neither bulb sets audio_reactive_eligible at all -- both must still
    # be included (the flag defaults to eligible=True when absent).
    import audio_reactive as ar

    started = {}

    def fake_start_group_session(group_id, controllers, device_index, mode, role_mode,
                                  sensitivity, monochrome_hue, min_dwell_ms, *extra, **kw):
        started["controllers"] = controllers
        return object()

    monkeypatch.setattr(ar, "validate_device_index", lambda idx: (True, None))
    monkeypatch.setattr(ar, "start_group_session", fake_start_group_session)

    resp = client.post("/api/groups/all/audio-reactive/start", json={"device_index": 0})
    assert resp.status_code == 200
    assert resp.json()["bulb_count"] == 2
