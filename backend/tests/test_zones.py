"""Zone CRUD + zone-scoped membership (Week 1 Phase C, Section 7).

A Zone sits above groups (e.g. "Living Room" containing several groups
and/or loose bulbs), persisted in config.json the same way `groups` already
is. Exercised through the real `/api/zones*` routes against the `client`
fixture (fake_config + fake_tuya), matching this suite's existing
conventions (see test_devices.py / test_scenes.py).
"""


def test_zone_starts_empty_when_none_configured(client):
    resp = client.get("/api/zones")
    assert resp.status_code == 200
    assert resp.json() == []


def test_create_zone_persists_and_lists(client):
    resp = client.post("/api/zones", json={"id": "living-room", "name": "Living Room", "device_ids": ["bulb-1"]})
    assert resp.status_code == 200
    zone = resp.json()
    assert zone["id"] == "living-room"
    assert zone["name"] == "Living Room"
    assert zone["device_ids"] == ["bulb-1"]
    assert zone["group_ids"] == []

    listed = client.get("/api/zones").json()
    assert len(listed) == 1
    assert listed[0]["id"] == "living-room"


def test_create_zone_duplicate_id_rejected(client):
    client.post("/api/zones", json={"id": "living-room", "name": "Living Room"})
    resp = client.post("/api/zones", json={"id": "living-room", "name": "Duplicate"})
    assert resp.status_code == 400


def test_add_and_remove_bulb_membership(client):
    client.post("/api/zones", json={"id": "living-room", "name": "Living Room", "device_ids": ["bulb-1"]})

    add_resp = client.post("/api/zones/living-room/devices", json={"device_id": "bulb-2"})
    assert add_resp.status_code == 200
    assert set(add_resp.json()["device_ids"]) == {"bulb-1", "bulb-2"}

    # Adding the same device again must not duplicate it.
    client.post("/api/zones/living-room/devices", json={"device_id": "bulb-2"})
    zone = client.get("/api/zones/living-room").json()
    assert zone["device_ids"].count("bulb-2") == 1

    remove_resp = client.delete("/api/zones/living-room/devices/bulb-1")
    assert remove_resp.status_code == 200
    assert remove_resp.json()["device_ids"] == ["bulb-2"]

    zone_after = client.get("/api/zones/living-room").json()
    assert zone_after["device_ids"] == ["bulb-2"]
    assert zone_after["resolved_device_ids"] == ["bulb-2"]


def test_zone_resolves_membership_via_referenced_groups(client, fake_config):
    # fake_config already ships group "all" -> ["bulb-1", "bulb-2"]
    client.post("/api/zones", json={
        "id": "whole-house", "name": "Whole House", "device_ids": [], "group_ids": ["all"],
    })
    zone = client.get("/api/zones/whole-house").json()
    assert zone["resolved_device_ids"] == ["bulb-1", "bulb-2"]


def test_update_zone_via_patch(client):
    client.post("/api/zones", json={"id": "living-room", "name": "Living Room"})
    resp = client.patch("/api/zones/living-room", json={"name": "Lounge"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "Lounge"
    assert client.get("/api/zones/living-room").json()["name"] == "Lounge"


def test_delete_zone(client):
    client.post("/api/zones", json={"id": "living-room", "name": "Living Room"})
    del_resp = client.delete("/api/zones/living-room")
    assert del_resp.status_code == 200
    assert client.get("/api/zones").json() == []
    assert client.get("/api/zones/living-room").status_code == 404


def test_zone_not_found_returns_404_for_mutations(client):
    assert client.patch("/api/zones/nope", json={"name": "x"}).status_code == 404
    assert client.post("/api/zones/nope/devices", json={"device_id": "bulb-1"}).status_code == 404
    assert client.delete("/api/zones/nope/devices/bulb-1").status_code == 404
