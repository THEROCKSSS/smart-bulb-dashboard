"""Device list endpoint: GET /api/devices (main.py list_devices)."""


def test_list_devices_returns_real_shape(client):
    resp = client.get("/api/devices")
    assert resp.status_code == 200
    data = resp.json()

    assert isinstance(data, list)
    assert len(data) == 2
    ids = {d["id"] for d in data}
    assert ids == {"bulb-1", "bulb-2"}

    bulb1 = next(d for d in data if d["id"] == "bulb-1")
    assert bulb1["name"] == "Living Room Bulb"
    assert bulb1["device_id"] == "dev-fake-1"
    assert bulb1["ip"] == "10.0.0.11"
    assert bulb1["version"] == 3.3

    # config.redact() must mask local_key before it ever leaves the server
    assert bulb1["local_key"] == "•" * len("fakekey1")
    assert bulb1["local_key"] != "fakekey1"


def test_list_devices_empty_when_no_devices_configured(client, fake_config):
    fake_config["devices"] = []
    resp = client.get("/api/devices")
    assert resp.status_code == 200
    assert resp.json() == []
