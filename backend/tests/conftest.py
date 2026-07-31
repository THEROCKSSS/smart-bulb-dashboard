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
    state = {
        "devices": [dict(d) for d in FAKE_DEVICES],
        "groups": [dict(g) for g in FAKE_GROUPS],
        "zones": [],
        "orchestration_presets": [],
        "audio_input_calibrations": [],
    }

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

    # -- zones (Section 7) -- same in-memory-only pattern as devices above,
    # so no test ever touches the real backend/config.json on disk.
    def get_zone(zone_id):
        return next((z for z in state["zones"] if z["id"] == zone_id), None)

    def upsert_zone(zone):
        for i, z in enumerate(state["zones"]):
            if z["id"] == zone["id"]:
                state["zones"][i] = zone
                return
        state["zones"].append(zone)

    def delete_zone(zone_id):
        state["zones"] = [z for z in state["zones"] if z["id"] != zone_id]

    def zone_resolved_device_ids(zone):
        groups_by_id = {g["id"]: g for g in state["groups"]}
        seen = []
        for d in zone.get("device_ids", []):
            if d not in seen:
                seen.append(d)
        for gid in zone.get("group_ids", []):
            g = groups_by_id.get(gid)
            if not g:
                continue
            for d in g.get("device_ids", []):
                if d not in seen:
                    seen.append(d)
        return seen

    # -- orchestration presets (Section 6) --
    def list_orchestration_presets():
        return state["orchestration_presets"]

    def get_orchestration_preset(preset_id):
        return next((p for p in state["orchestration_presets"] if p["id"] == preset_id), None)

    def upsert_orchestration_preset(preset):
        for i, p in enumerate(state["orchestration_presets"]):
            if p["id"] == preset["id"]:
                state["orchestration_presets"][i] = preset
                return
        state["orchestration_presets"].append(preset)

    def delete_orchestration_preset(preset_id):
        state["orchestration_presets"] = [p for p in state["orchestration_presets"] if p["id"] != preset_id]

    # -- audio input calibration (Section 11) --
    def list_audio_input_calibrations():
        return state["audio_input_calibrations"]

    def get_audio_input_calibration(device_index):
        for c in state["audio_input_calibrations"]:
            if c["device_index"] == device_index:
                return c["sensitivity"]
        return None

    def set_audio_input_calibration(device_index, sensitivity, name=None):
        for c in state["audio_input_calibrations"]:
            if c["device_index"] == device_index:
                c["sensitivity"] = sensitivity
                if name is not None:
                    c["name"] = name
                return c
        entry = {"device_index": device_index, "sensitivity": sensitivity}
        if name is not None:
            entry["name"] = name
        state["audio_input_calibrations"].append(entry)
        return entry

    def delete_audio_input_calibration(device_index):
        state["audio_input_calibrations"] = [
            c for c in state["audio_input_calibrations"] if c["device_index"] != device_index
        ]

    monkeypatch.setattr(cfgmod, "load_config", load_config)
    monkeypatch.setattr(cfgmod, "get_device", get_device)
    monkeypatch.setattr(cfgmod, "upsert_device", upsert_device)
    monkeypatch.setattr(cfgmod, "delete_device", delete_device)
    monkeypatch.setattr(cfgmod, "get_zone", get_zone)
    monkeypatch.setattr(cfgmod, "upsert_zone", upsert_zone)
    monkeypatch.setattr(cfgmod, "delete_zone", delete_zone)
    monkeypatch.setattr(cfgmod, "zone_resolved_device_ids", zone_resolved_device_ids)
    monkeypatch.setattr(cfgmod, "list_orchestration_presets", list_orchestration_presets)
    monkeypatch.setattr(cfgmod, "get_orchestration_preset", get_orchestration_preset)
    monkeypatch.setattr(cfgmod, "upsert_orchestration_preset", upsert_orchestration_preset)
    monkeypatch.setattr(cfgmod, "delete_orchestration_preset", delete_orchestration_preset)
    monkeypatch.setattr(cfgmod, "list_audio_input_calibrations", list_audio_input_calibrations)
    monkeypatch.setattr(cfgmod, "get_audio_input_calibration", get_audio_input_calibration)
    monkeypatch.setattr(cfgmod, "set_audio_input_calibration", set_audio_input_calibration)
    monkeypatch.setattr(cfgmod, "delete_audio_input_calibration", delete_audio_input_calibration)
    return state


@pytest.fixture(autouse=True)
def reset_controllers():
    with bm._controllers_lock:
        bm._controllers.clear()
    yield
    with bm._controllers_lock:
        bm._controllers.clear()


@pytest.fixture(autouse=True)
def reset_audio_rate_limit():
    # Real bug, found by actually running the full suite after merging Week 1
    # Phase D's per-endpoint rate limiter: audio_reactive._rate_limit_hits is
    # module-level global state, so tests in the same file hitting the same
    # group/device repeatedly (well within a real 10s window) tripped a 429
    # that had nothing to do with what that test was actually checking.
    import audio_reactive as ar_module
    with ar_module._rate_limit_lock:
        ar_module._rate_limit_hits.clear()
    yield
    with ar_module._rate_limit_lock:
        ar_module._rate_limit_hits.clear()


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
def client(fake_config, fake_tuya, auth_reset):
    return TestClient(main_module.app)


@pytest.fixture
def auth_reset(tmp_path, monkeypatch):
    # Real bug, found by actually running this suite against a machine with
    # the PIN gate enabled for real (Tailscale exposure): without this, these
    # tests read the REAL backend/data/remote_auth.json, so a real "enabled"
    # gate makes every route in this file 401 instead of the expected
    # 200/400/404. `client` now depends on this so no route call in this
    # module ever sees whatever the real local machine's auth state happens
    # to be. (test_remote_auth.py defines its own module-local `client`
    # fixture that shadows this one entirely, and manages its own auth
    # isolation deliberately, since it's specifically testing that system.)
    fake_path = tmp_path / "remote_auth.json"
    monkeypatch.setattr(remote_auth, "AUTH_PATH", str(fake_path))
    remote_auth._attempts.clear()
    yield
    remote_auth._attempts.clear()
