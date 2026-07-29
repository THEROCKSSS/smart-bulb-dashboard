"""Shared fixtures for the backend pytest suite.

None of these tests touch real hardware, the real backend/config.json, or
the real backend/data/*.json files:
  - `fake_config` replaces `config.py`'s disk-backed load/get/upsert/delete
    with an in-memory dict so device lookups resolve to synthetic devices.
  - `fake_tuya` replaces `tinytuya.BulbDevice` (as referenced by
    `bulb_manager`) with `FakeTuyaBulbDevice`, which records every call so
    tests can assert the route actually drove the device layer.
  - `reset_controllers` (autouse) clears bulb_manager's process-wide
    controller cache before/after every test so state never leaks across
    tests.
  - `auth_reset` points remote_auth's on-disk state file at a pytest tmp
    path instead of the real backend/data/remote_auth.json.
"""

import colorsys
import os
import sys

import pytest
from fastapi.testclient import TestClient

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import config as cfgmod  # noqa: E402
import bulb_manager as bm  # noqa: E402
import remote_auth  # noqa: E402
import main as main_module  # noqa: E402


FAKE_DEVICES = [
    {
        "id": "bulb-1",
        "name": "Living Room Bulb",
        "device_id": "dev-fake-1",
        "local_key": "fakekey1",
        "ip": "10.0.0.11",
        "version": 3.3,
        "gamma": 1.0,
    },
    {
        "id": "bulb-2",
        "name": "Bedroom Bulb",
        "device_id": "dev-fake-2",
        "local_key": "fakekey2",
        "ip": "10.0.0.12",
        "version": 3.3,
        "gamma": 1.0,
    },
]

FAKE_GROUPS = [
    {"id": "all", "name": "All Bulbs", "device_ids": ["bulb-1", "bulb-2"]},
]


class FakeTuyaBulbDevice:
    """Stand-in for tinytuya.BulbDevice. Tracks enough state (mirroring the
    real dp20/21/22/23/24 map bulb_manager._parse_dps reads) to answer
    status() realistically, and records every call so tests can assert the
    route actually reached the device layer with the right arguments."""

    def __init__(self, device_id, ip, local_key, version=3.3):
        self.device_id = device_id
        self.ip = ip
        self.local_key = local_key
        self.version = version
        self.calls = []
        self._on = False
        self._h, self._s, self._v = 0, 1000, 1000
        self._mode = "white"

    def set_socketPersistent(self, val):
        self.calls.append(("set_socketPersistent", val))

    def turn_on(self):
        self.calls.append(("turn_on",))
        self._on = True
        return {"dps": {"20": True}}

    def turn_off(self):
        self.calls.append(("turn_off",))
        self._on = False
        return {"dps": {"20": False}}

    def set_colour(self, r, g, b):
        self.calls.append(("set_colour", r, g, b))
        h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        self._h, self._s, self._v = int(h * 360) % 360, int(s * 1000), int(v * 1000)
        self._mode = "colour"
        return {"dps": {"24": f"{self._h:04x}{self._s:04x}{self._v:04x}"}}

    def set_value(self, dp, raw):
        self.calls.append(("set_value", dp, raw))
        return {"dps": {str(dp): raw}}

    def set_white(self):
        self.calls.append(("set_white",))
        self._mode = "white"
        return {"dps": {"21": "white"}}

    def set_colourtemp(self, raw):
        self.calls.append(("set_colourtemp", raw))
        return {"dps": {"23": raw}}

    def status(self):
        colour_hex = f"{self._h:04x}{self._s:04x}{self._v:04x}"
        return {
            "dps": {
                "20": self._on,
                "21": self._mode,
                "22": 1000,
                "23": 500,
                "24": colour_hex,
            }
        }


@pytest.fixture
def fake_config(monkeypatch):
    state = {"devices": [dict(d) for d in FAKE_DEVICES], "groups": [dict(g) for g in FAKE_GROUPS]}

    def load_config():
        return state

    def get_device(device_id):
        return next((d for d in state["devices"] if d["id"] == device_id), None)

    def upsert_device(device):
        for i, d in enumerate(state["devices"]):
            if d["id"] == device["id"]:
                state["devices"][i] = device
                return
        state["devices"].append(device)

    def delete_device(device_id):
        state["devices"] = [d for d in state["devices"] if d["id"] != device_id]

    monkeypatch.setattr(cfgmod, "load_config", load_config)
    monkeypatch.setattr(cfgmod, "get_device", get_device)
    monkeypatch.setattr(cfgmod, "upsert_device", upsert_device)
    monkeypatch.setattr(cfgmod, "delete_device", delete_device)
    return state


@pytest.fixture(autouse=True)
def reset_controllers():
    with bm._controllers_lock:
        bm._controllers.clear()
    yield
    with bm._controllers_lock:
        bm._controllers.clear()


@pytest.fixture
def fake_tuya(monkeypatch):
    created = {}

    def factory(device_id, ip, local_key, version=3.3):
        dev = FakeTuyaBulbDevice(device_id, ip, local_key, version)
        created[device_id] = dev
        return dev

    monkeypatch.setattr(bm.tinytuya, "BulbDevice", factory)
    return created


@pytest.fixture
def client(fake_config, fake_tuya):
    return TestClient(main_module.app)


@pytest.fixture
def auth_reset(tmp_path, monkeypatch):
    fake_path = tmp_path / "remote_auth.json"
    monkeypatch.setattr(remote_auth, "AUTH_PATH", str(fake_path))
    remote_auth._attempts.clear()
    yield
    remote_auth._attempts.clear()
