"""Color-set endpoints: POST /api/devices/{id}/color and .../color/hsv.

Verifies the HSV->RGB conversion path (bulb_manager.hsv_to_rgb_hue_deg,
exercised via BulbController.set_hsv) actually runs and produces the
expected RGB values the fake device layer receives -- not just a 200.
"""

import colorsys


def test_color_rgb_endpoint_calls_set_colour_with_given_values(client, fake_tuya):
    resp = client.post("/api/devices/bulb-1/color", json={"r": 10, "g": 20, "b": 30})
    assert resp.status_code == 200

    dev = fake_tuya["dev-fake-1"]
    assert ("set_colour", 10, 20, 30) in dev.calls


def test_color_hsv_endpoint_converts_green_hue_correctly(client, fake_tuya):
    # h=120deg, s=100%, v=100% is pure green in HSV -> (0, 255, 0) in RGB
    resp = client.post("/api/devices/bulb-1/color/hsv", json={"h": 120, "s": 100, "v": 100})
    assert resp.status_code == 200

    dev = fake_tuya["dev-fake-1"]
    calls = [c for c in dev.calls if c[0] == "set_colour"]
    assert len(calls) == 1
    _, r, g, b = calls[0]
    assert (r, g, b) == (0, 255, 0)


def test_color_hsv_endpoint_matches_colorsys_conversion_for_arbitrary_input(client, fake_tuya):
    h, s, v = 210.0, 60.0, 45.0
    resp = client.post("/api/devices/bulb-1/color/hsv", json={"h": h, "s": s, "v": v})
    assert resp.status_code == 200

    er, eg, eb = colorsys.hsv_to_rgb((h % 360) / 360.0, s / 100.0, v / 100.0)
    expected = (int(er * 255), int(eg * 255), int(eb * 255))

    dev = fake_tuya["dev-fake-1"]
    calls = [c for c in dev.calls if c[0] == "set_colour"]
    assert len(calls) == 1
    _, r, g, b = calls[0]
    assert (r, g, b) == expected


def test_color_hsv_defaults_full_saturation_and_value(client, fake_tuya):
    # HSVBody defaults s=100, v=100 when omitted
    resp = client.post("/api/devices/bulb-1/color/hsv", json={"h": 0})
    assert resp.status_code == 200

    dev = fake_tuya["dev-fake-1"]
    _, r, g, b = dev.calls[-1]
    assert (r, g, b) == (255, 0, 0)


def test_color_random_endpoint_calls_set_colour_and_returns_rgb(client, fake_tuya):
    resp = client.post("/api/devices/bulb-1/color/random")
    assert resp.status_code == 200
    body = resp.json()["result"]
    assert set(body.keys()) == {"r", "g", "b"}
    for channel in ("r", "g", "b"):
        assert 0 <= body[channel] <= 255

    dev = fake_tuya["dev-fake-1"]
    assert any(c[0] == "set_colour" for c in dev.calls)


def test_color_unknown_device_returns_404(client):
    resp = client.post("/api/devices/nope/color", json={"r": 1, "g": 2, "b": 3})
    assert resp.status_code == 404
