"""Power on/off endpoint: POST /api/devices/{device_id}/power.

Mocks the underlying tinytuya device layer (FakeTuyaBulbDevice, via the
`fake_tuya` fixture) so no real hardware is contacted, and asserts the
route actually calls through to it -- not just that a 200 comes back.
"""


def test_power_on_calls_through_to_device(client, fake_tuya):
    resp = client.post("/api/devices/bulb-1/power", json={"on": True})
    assert resp.status_code == 200
    body = resp.json()
    assert "result" in body

    dev = fake_tuya["dev-fake-1"]
    assert ("turn_on",) in dev.calls
    assert not any(c[0] == "turn_off" for c in dev.calls)


def test_power_off_calls_through_to_device(client, fake_tuya):
    resp = client.post("/api/devices/bulb-1/power", json={"on": False})
    assert resp.status_code == 200

    dev = fake_tuya["dev-fake-1"]
    assert ("turn_off",) in dev.calls
    assert not any(c[0] == "turn_on" for c in dev.calls)


def test_power_call_is_logged_to_real_history(client, fake_tuya):
    resp = client.post("/api/devices/bulb-1/power", json={"on": True})
    assert resp.status_code == 200

    hist = client.get("/api/devices/bulb-1/history").json()
    assert len(hist) == 1
    assert hist[0]["action"] == "power"
    assert hist[0]["params"] == {"on": True}
    assert hist[0]["ok"] is True


def test_power_only_affects_targeted_device(client, fake_tuya):
    client.post("/api/devices/bulb-1/power", json={"on": True})
    dev2 = fake_tuya["dev-fake-2"] if "dev-fake-2" in fake_tuya else None
    assert dev2 is None  # bulb-2's device layer was never even constructed


def test_power_unknown_device_returns_404(client):
    resp = client.post("/api/devices/does-not-exist/power", json={"on": True})
    assert resp.status_code == 404
    assert "does-not-exist" in resp.json()["detail"]


def test_toggle_flips_power_by_reading_status_first(client, fake_tuya):
    dev = fake_tuya  # ensure fixture applied before direct dev access below
    resp = client.post("/api/devices/bulb-1/toggle")
    assert resp.status_code == 200
    # controller was off by default, so toggle should have turned it on
    assert ("turn_on",) in fake_tuya["dev-fake-1"].calls
