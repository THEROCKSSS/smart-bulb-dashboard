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
  - `reset_api_rate_limit` / `reset_audio_rate_limit` (both autouse) clear
    the two module-level rate limiters between tests.
  - `reset_reverse_proxy_settings` (autouse) forces reverse_proxy back to
    its shipped defaults, so neither a developer's own SBD_* environment
    nor a test that enables proxy trust can leak into anything else.
  - `security_audit_isolation` (autouse) redirects every path the
    security-event log writes to. This one is autouse and unconditional
    because remote_auth.log_audit_event() now forwards to security_audit,
    so ANY test that touches auth would otherwise append to the real
    backend/data/security_events.log and corrupt its hmac chain.
  - `backup_isolation` (autouse) points backup_restore at throwaway
    directories, so no test can read the real config.json into an archive
    or write one into the repo.
  - `clean_secret_env` (autouse) strips SBD_* secret env vars, so a
    developer's real `.env` can never change what a test sees.
  - `observability_reset` (autouse) clears the process-global metric,
    log-buffer and dependency-cache state and redirects every Week 2 Phase
    D state file (observability.json, network_state.json,
    remote_access.json) at a pytest tmp path, so no test can read or write
    the real backend/data/ copies.

The one fixture here that isolates nothing:
  - `check_all` runs a single assertion across a whole collection and
    reports every failure together. It exists so that "the same check over
    all 24 presets" is one test rather than 24 parametrised ones.
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
import reverse_proxy  # noqa: E402
import security_audit  # noqa: E402
import backup_restore  # noqa: E402
import secrets_env  # noqa: E402
import observability  # noqa: E402
import network_health  # noqa: E402
import remote_access_status  # noqa: E402
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
def clean_secret_env(monkeypatch):
    """A developer's real `.env` (loaded by main.py at import) must never
    change what a test sees. Strip every variable this project reads for a
    secret or a path before each test; tests that want one set it back."""
    for name in list(os.environ):
        if name.startswith(secrets_env.DEVICE_KEY_PREFIX):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("SBD_ENV_FILE", raising=False)
    monkeypatch.delenv("SBD_BACKUP_DIR", raising=False)


@pytest.fixture(autouse=True)
def security_audit_isolation(tmp_path, monkeypatch):
    """remote_auth.log_audit_event() forwards to security_audit, and
    config.save_config() writes a change-tracking event, so nearly every
    test in this suite now produces security events as a side effect.
    Without this they'd land in the real backend/data/security_events.log
    -- polluting it and, worse, extending its tamper-evident hmac chain
    with entries from a test run."""
    audit_dir = tmp_path / "security_audit"
    audit_dir.mkdir()
    monkeypatch.setattr(security_audit, "DATA_DIR", str(audit_dir))
    monkeypatch.setattr(security_audit, "EVENTS_LOG_PATH", str(audit_dir / "security_events.log"))
    monkeypatch.setattr(security_audit, "STATE_PATH", str(audit_dir / "security_audit_state.json"))
    monkeypatch.setattr(security_audit, "KEY_PATH", str(audit_dir / "security_audit_key"))
    monkeypatch.setattr(security_audit, "CONFIG_PATH", str(audit_dir / "security_audit_config.json"))
    monkeypatch.setattr(security_audit, "ALERTS_PATH", str(audit_dir / "security_alerts.json"))
    security_audit._threshold_hits.clear()
    yield
    security_audit._threshold_hits.clear()


@pytest.fixture(autouse=True)
def backup_isolation(tmp_path, monkeypatch):
    """Every path backup_restore reads or writes points somewhere
    throwaway. Autouse rather than opt-in: a backup route touched by
    accident would otherwise pull the developer's real config.json (device
    local_keys included) into an archive under the repo."""
    root = tmp_path / "backup_root"
    (root / "data").mkdir(parents=True)
    (root / "backups").mkdir()
    monkeypatch.setattr(backup_restore, "BACKEND_DIR", str(root))
    monkeypatch.setattr(backup_restore, "DATA_DIR", str(root / "data"))
    monkeypatch.setattr(backup_restore, "CONFIG_PATH", str(root / "config.json"))
    monkeypatch.setattr(backup_restore, "REMOTE_AUTH_PATH", str(root / "data" / "remote_auth.json"))
    monkeypatch.setattr(backup_restore, "BACKUP_DIR", str(root / "backups"))
    monkeypatch.setattr(backup_restore, "SETTINGS_PATH", str(root / "data" / "backup_settings.json"))
    return root


@pytest.fixture(autouse=True)
def reset_controllers():
    with bm._controllers_lock:
        bm._controllers.clear()
    yield
    with bm._controllers_lock:
        bm._controllers.clear()


@pytest.fixture(autouse=True)
def reset_api_rate_limit():
    # Same class of leak as reset_audio_rate_limit below, one layer up:
    # api_rate_limit's counters are module-level and every TestClient request
    # in the whole suite reports the same client host ("testclient"), so
    # without this the suite as a whole would eventually trip a 429 in some
    # unrelated test. Also restores the tier limits, since rate-limit tests
    # lower them deliberately.
    import api_rate_limit
    api_rate_limit.reset()
    yield
    api_rate_limit.reset()


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


@pytest.fixture(autouse=True)
def reset_reverse_proxy_settings():
    # reverse_proxy caches its parsed SBD_* config in a module global, so
    # without this a test that turns on proxy trust or the HTTPS redirect
    # would change what every later test's requests mean -- and a developer
    # who happens to have SBD_TRUSTED_PROXIES exported in their own shell
    # would get different results from CI. Empty env == the shipped
    # defaults (trust nothing, no HSTS, no redirect).
    reverse_proxy.reload_from_env(env={})
    yield
    reverse_proxy.reload_from_env(env={})


@pytest.fixture(autouse=True)
def observability_reset(tmp_path, monkeypatch):
    """Week 2 Phase D state is process-global (metrics counters, the log
    ring buffer, the dependency-probe cache) or disk-backed under
    backend/data/. Both leak across tests and, worse, the disk-backed half
    would have tests reading and overwriting the real machine's network /
    remote-access state -- the same class of bug `auth_reset` exists for.
    Everything gets a clean, throwaway home per test."""
    monkeypatch.setattr(observability, "SETTINGS_PATH", str(tmp_path / "observability.json"))
    monkeypatch.setattr(network_health, "NETWORK_STATE_PATH", str(tmp_path / "network_state.json"))
    monkeypatch.setattr(remote_access_status, "REMOTE_ACCESS_PATH", str(tmp_path / "remote_access.json"))
    observability.reset_metrics()
    observability.clear_log_buffer()
    observability.clear_template_cache()
    with network_health._latency_lock:
        network_health._latency.clear()
    yield
    observability.reset_metrics()
    observability.clear_log_buffer()
    observability.clear_template_cache()
    with network_health._latency_lock:
        network_health._latency.clear()


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
    # The audit log needs redirecting too, not just the state file: every
    # login/logout/enable/disable a test drives appends a line, and without
    # this those lines land in the real backend/data/auth_audit.log. (Found
    # by checking what the suite had actually written to backend/data/ after
    # a full run -- the state file was isolated, its log was not.)
    monkeypatch.setattr(remote_auth, "AUDIT_LOG_PATH", str(tmp_path / "auth_audit.log"))
    remote_auth._attempts.clear()
    remote_auth._rate_limit_buckets.clear()
    yield
    remote_auth._attempts.clear()
    remote_auth._rate_limit_buckets.clear()


# pytest.fail() raises Failed, which derives from BaseException, not
# Exception -- so a bare `except Exception` silently fails to catch the one
# signal a test is most likely to send. Named here so the catch below is
# explicit about it rather than resorting to `except BaseException`.
_PYTEST_FAILED = pytest.fail.Exception


@pytest.fixture
def check_all():
    """Run one assertion over many cases, reporting every failure at once.

    This replaces `@pytest.mark.parametrize` in the specific situation where
    the parameters are not distinct behaviours but "the same check, over
    every member of a collection" -- all 24 genre presets, all 20 audio
    modes, all 11 IP classifications.

    Parametrising those turns one behaviour into 24 collected tests. That
    inflates the suite count without adding a single assertion, and it
    reports failures one at a time: break something common to every preset
    and you get 24 red lines, then fix-and-rerun until they clear.

    Collecting instead gives one test and one report naming every casualty:

        AssertionError: 3 of 24 presets failed:
          [techno_dark] references unknown colours: ['warm_white']
          [lofi_study] min_dwell_ms 20 below floor 40
          [punk_garage] KeyError: 'palette'

    Coverage is identical -- every case still runs, and one failing case
    still fails the suite. What changes is that you see all of them at once.

    Args:
        cases: the collection to check. Consumed once, so a generator is fine.
        fn:    called with each case; raise (or assert) to fail that case.
        label: noun for the report, pluralised with a bare "s".
        name:  case -> short identifier for the report. Defaults to str().

    Usage:
        def test_every_preset_is_valid(check_all):
            check_all(ar.AUDIO_GENRE_PRESETS, _assert_valid,
                      label="preset", name=lambda p: p["id"])
    """
    def _check_all(cases, fn, label="case", name=str):
        cases = list(cases)
        failures = []
        for case in cases:
            try:
                fn(case)
            except (Exception, _PYTEST_FAILED) as exc:
                # An AssertionError's message is the whole point, so show it
                # bare. Anything else is a crash rather than a failed check,
                # so name the type -- "KeyError: 'palette'" reads very
                # differently from an assertion that simply did not hold.
                detail = (str(exc) if isinstance(exc, AssertionError)
                          else f"{type(exc).__name__}: {exc}")
                failures.append(f"  [{name(case)}] {detail}".rstrip())
        if failures:
            raise AssertionError(
                f"{len(failures)} of {len(cases)} {label}s failed:\n"
                + "\n".join(failures)
            )
    return _check_all
