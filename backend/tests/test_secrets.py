"""Secrets management + the systematic redaction audit (Week 2, W2-211..225).

This file is deliberately adversarial about the app's own output. The
existing redaction (`config.redact()` on the device endpoints) was verified
ad hoc; the point here is to have tests that would *catch a regression* --
so the sweep below walks the real route table rather than a hand-written
list, and the secret values it hunts for are the fixture keys from
conftest.py, which means any new endpoint that echoes a device dict fails
this file without anyone remembering to update it.

Covers:
  - W2-214 redaction audit: local_key in responses, errors, logs, history
  - W2-217 the PIN is never logged, at any level
  - W2-218 the session-signing key: entropy, and never logged/exposed
  - W2-211/215 env-var and `.env` support, including the round-trip trap
  - W2-220 the CI secret scanner
  - W2-223 documented sensitivity of each secret
"""
import json
import os
import subprocess
import sys

import pytest

import bulb_manager as bm
import config as cfgmod
import main as main_module
import remote_auth
import secrets_env
import security_audit

REPO_ROOT = os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))
SCANNER = os.path.join(REPO_ROOT, ".github", "scripts", "scan_secrets.py")

FIXTURE_KEYS = ("fakekey1", "fakekey2")


# ------------------------------------------------- W2-214: response sweep --

# Endpoints that never return -- an SSE stream stays open until the client
# disconnects, so a plain blocking GET against one hangs the whole suite.
# They are NOT exempt from the leak check: each one is swept separately, with
# a bounded read, by its own test below. Keep that pairing if anything is
# added here.
STREAMING_PATHS = {"/api/stream"}


def _parameterless_get_paths():
    """Every GET route with no path parameters, straight off the real app.
    Walking the route table rather than a hand-maintained list is the whole
    point: a future endpoint that leaks a device dict is caught without
    anyone remembering to add it here."""
    paths = []
    for route in main_module.app.routes:
        methods = getattr(route, "methods", set()) or set()
        path = getattr(route, "path", "")
        if "GET" not in methods or "{" in path or not path.startswith("/api/"):
            continue
        if path in STREAMING_PATHS:
            continue
        paths.append(path)
    return sorted(set(paths))


def test_streaming_endpoints_are_swept_too():
    """The sweep above must skip never-ending responses, so they get their own
    bounded check rather than quietly dropping out of coverage.

    Driven against the generator directly instead of through TestClient: a
    blocking client waits for the stream to END, and an SSE stream by
    definition does not, so any client-level assertion here deadlocks the
    suite. Exercising the generator is also closer to the risk -- a leak
    would come from what gets serialised into a frame."""
    import asyncio

    import live_stream

    async def drive():
        sub = live_stream.subscribe()
        frames = []

        async def never_disconnected():
            return False

        gen = live_stream.event_source(sub, never_disconnected)
        # The opening `ready` frame, then one real event pushed through the
        # same path a producer uses.
        frames.append(await gen.__anext__())
        live_stream._offer(sub, ("bulb", {"device_id": "bulb-1", "hex": "#112233"}))
        frames.append(await gen.__anext__())
        await gen.aclose()
        return "".join(frames)

    body = asyncio.run(drive())
    assert "event: ready" in body
    assert "event: bulb" in body
    for key in FIXTURE_KEYS:
        assert key not in body


def test_a_closed_stream_unsubscribes_itself():
    """A subscriber that outlived its connection would leak memory and keep
    receiving events forever."""
    import asyncio

    import live_stream

    async def drive():
        before = live_stream.subscriber_count()
        sub = live_stream.subscribe()

        async def never_disconnected():
            return False

        gen = live_stream.event_source(sub, never_disconnected)
        await gen.__anext__()
        assert live_stream.subscriber_count() == before + 1
        await gen.aclose()
        return before, live_stream.subscriber_count()

    before, after = asyncio.run(drive())
    assert after == before


def test_every_streaming_path_actually_exists():
    """Guards the skip list itself: a stale entry here would silently drop a
    real endpoint out of the sweep above."""
    registered = {getattr(r, "path", "") for r in main_module.app.routes}
    assert STREAMING_PATHS <= registered


def test_no_get_endpoint_ever_returns_a_local_key(client, fake_config):
    """The broad sweep. Endpoints that legitimately fail in a test
    environment (no audio hardware, no bulbs on the LAN) are allowed to
    error -- what is never allowed is a key in the bytes that come back."""
    exercised = 0
    for path in _parameterless_get_paths():
        resp = client.get(path)
        body = resp.text
        exercised += 1
        for key in FIXTURE_KEYS:
            assert key not in body, f"{path} leaked a local_key ({resp.status_code})"
    # A sweep that silently exercised nothing would pass forever.
    assert exercised >= 15, f"only swept {exercised} routes -- the route walk is broken"


DEVICE_GET_PATHS = [
    "/api/devices/{id}/status",
    "/api/devices/{id}/history",
    "/api/devices/{id}/favorites",
    "/api/devices/{id}/effects/current",
    "/api/devices/{id}/timers/sleep",
    "/api/devices/{id}/timers/wake",
    "/api/devices/{id}/schedule",
    "/api/devices/{id}/audio-reactive/status",
    "/api/devices/{id}/lightshow",
]


def test_no_per_device_endpoint_returns_a_local_key(client, fake_config, check_all):
    """Every per-device GET in one test rather than one per route.

    This is a leak sweep: the question it answers is "does any route expose
    the key", so a regression that adds the key to a shared serialiser
    should report every affected route at once, not just the first.
    """
    def _no_key(template):
        resp = client.get(template.format(id="bulb-1"))
        leaked = [key for key in FIXTURE_KEYS if key in resp.text]
        assert not leaked, f"response contains {leaked}"

    check_all(DEVICE_GET_PATHS, _no_key, label="device route")


def test_device_list_and_patch_responses_are_redacted(client, fake_config):
    listed = client.get("/api/devices").json()
    assert listed[0]["local_key"] == "•" * len("fakekey1")
    assert listed[0]["local_key_source"] == "config.json"

    patched = client.patch("/api/devices/bulb-1", json={"name": "Renamed"})
    assert patched.status_code == 200
    assert "fakekey1" not in patched.text
    assert patched.json()["local_key"].startswith("•")


def test_test_connection_and_rescan_do_not_echo_the_key(client, fake_config, fake_tuya):
    for path in ("/api/devices/bulb-1/test-connection", "/api/devices/bulb-1/rescan"):
        resp = client.post(path)
        for key in FIXTURE_KEYS:
            assert key not in resp.text, f"{path} leaked a local_key"


# ------------------------------------------- W2-214: errors and history ----

class _LeakyDevice:
    """A tinytuya stand-in whose exception text contains the local_key it
    was constructed with. Not hypothetical paranoia: this code has no
    control over how tinytuya (or the socket layer under it) words its
    errors, and `status()` puts that text straight into an API response and
    into the per-device history."""

    def __init__(self, device_id, ip, local_key, version=3.3):
        self.local_key = local_key

    def set_socketPersistent(self, val):
        pass

    def status(self):
        raise RuntimeError(f"handshake failed for key={self.local_key} at 10.0.0.11")


def test_an_exception_containing_the_key_is_redacted_before_it_reaches_a_client(
        client, fake_config, monkeypatch):
    monkeypatch.setattr(bm.tinytuya, "BulbDevice", _LeakyDevice)

    resp = client.get("/api/devices/bulb-1/status")
    assert resp.status_code == 200
    assert "fakekey1" not in resp.text
    assert "[redacted]" in resp.json()["error"]

    # ...and in the history log it writes as a side effect.
    history = client.get("/api/devices/bulb-1/history")
    assert "fakekey1" not in history.text

    # ...and in the cached last_error the diagnostics panel reads.
    assert "fakekey1" not in (bm.get_controller("bulb-1").last_error() or "")


def test_a_raw_error_payload_containing_the_key_is_redacted(client, fake_config, monkeypatch):
    """tinytuya can return an error as a dict rather than raise; that dict
    is passed through to the client as `raw`."""
    class _ErrDictDevice(_LeakyDevice):
        def status(self):
            return {"Error": "decode failure", "payload": f"key={self.local_key}"}

    monkeypatch.setattr(bm.tinytuya, "BulbDevice", _ErrDictDevice)
    resp = client.get("/api/devices/bulb-1/status")
    assert "fakekey1" not in resp.text


def test_effect_errors_are_redacted_in_history(fake_config, monkeypatch):
    controller = bm.get_controller("bulb-1")
    controller._log("effect_error", {"effect": "rainbow"}, ok=False,
                    error="boom while using fakekey1")
    assert "fakekey1" not in json.dumps(controller.history())
    assert "[redacted]" in controller.history()[0]["error"]


# ------------------------------------------------- W2-214/217: the logs ----

def test_local_key_never_reaches_the_security_event_log(client, fake_config, fake_tuya):
    """Drive a spread of real routes, then read the whole log back."""
    client.get("/api/devices")
    client.post("/api/devices/bulb-1/power", json={"on": True})
    client.patch("/api/devices/bulb-1", json={"name": "Renamed"})
    client.get("/api/security/events")

    log = security_audit.EVENTS_LOG_PATH
    raw = open(log, encoding="utf-8").read() if os.path.exists(log) else ""
    for key in FIXTURE_KEYS:
        assert key not in raw


def test_the_pin_is_never_written_to_either_audit_log(client, tmp_path, monkeypatch):
    """W2-217. Both logs, every event a login can produce: success, wrong
    PIN, lockout, and the enable that set it in the first place."""
    monkeypatch.setattr(remote_auth, "AUTH_PATH", str(tmp_path / "remote_auth.json"))
    monkeypatch.setattr(remote_auth, "AUDIT_LOG_PATH", str(tmp_path / "auth_audit.log"))
    remote_auth._attempts.clear()
    remote_auth._rate_limit_buckets.clear()

    pin = "5091827364"
    wrong = "1122334455"
    remote_auth.enable(pin)
    try:
        remote_auth.verify_pin(pin, "10.0.0.1")
        for _ in range(6):
            remote_auth.verify_pin(wrong, "10.0.0.2")
        remote_auth.log_audit_event("login_success", "success", ip="10.0.0.1")
        remote_auth.log_audit_event("login_failure", "failure", ip="10.0.0.2")
    finally:
        remote_auth.disable()

    for path in (remote_auth.AUDIT_LOG_PATH, security_audit.EVENTS_LOG_PATH):
        raw = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
        assert pin not in raw, f"PIN leaked into {os.path.basename(path)}"
        assert wrong not in raw, f"attempted PIN leaked into {os.path.basename(path)}"

    # The stored state must hold only a hash + salt, never the PIN itself.
    state = json.loads(open(str(tmp_path / "remote_auth.json"), encoding="utf-8").read())
    assert pin not in json.dumps(state)
    assert state["pin_hash"] != pin and len(state["pin_hash"]) == 64


def test_the_pin_is_not_echoed_by_the_login_endpoint_on_failure(client):
    """An error message that repeats what you typed is how a PIN ends up in
    a browser console, a screenshot, or a bug report."""
    remote_auth.enable("7788990011")
    try:
        resp = client.post("/api/auth/login", json={"pin": "0011223344"})
        assert resp.status_code == 401
        assert "0011223344" not in resp.text
    finally:
        client.cookies.clear()
        remote_auth.disable()


def test_the_session_signing_key_is_never_exposed_or_logged(client, tmp_path, monkeypatch):
    """W2-218, second half. The key is what forges a session cookie, so it
    must not appear in any API response or either log."""
    monkeypatch.setattr(remote_auth, "AUTH_PATH", str(tmp_path / "remote_auth.json"))
    monkeypatch.setattr(remote_auth, "AUDIT_LOG_PATH", str(tmp_path / "auth_audit.log"))
    remote_auth._attempts.clear()

    pin = "3344556677"
    remote_auth.enable(pin)
    try:
        secret = remote_auth._load()["secret_key"]
        login = client.post("/api/auth/login", json={"pin": pin})
        cookie = login.cookies.get(remote_auth.SESSION_COOKIE)
        client.cookies.set(remote_auth.SESSION_COOKIE, cookie)

        for path in ("/api/auth/status", "/api/auth/sessions",
                     "/api/system/remote-auth/status", "/api/security/secrets"):
            assert secret not in client.get(path).text, f"{path} exposed the signing key"

        # The session list may name a session, never the token that opens it.
        assert cookie not in client.get("/api/auth/sessions").text

        client.post("/api/auth/sessions/revoke-all")
        for path in (remote_auth.AUDIT_LOG_PATH, security_audit.EVENTS_LOG_PATH):
            raw = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
            assert secret not in raw
            assert cookie not in raw
    finally:
        client.cookies.clear()
        remote_auth.disable()


def test_session_signing_key_has_full_entropy(tmp_path, monkeypatch):
    """W2-218, first half. 32 random bytes from `secrets`, and genuinely
    fresh on every rotation -- a rotation that returned the same key would
    make revoke-all a no-op for anyone holding a forged cookie."""
    monkeypatch.setattr(remote_auth, "AUTH_PATH", str(tmp_path / "remote_auth.json"))

    keys = set()
    for _ in range(20):
        key = remote_auth._default_state()["secret_key"]
        assert len(key) == 64, "expected 32 bytes / 256 bits, hex-encoded"
        assert all(c in "0123456789abcdef" for c in key)
        keys.add(key)
    assert len(keys) == 20, "generated keys repeated -- not a CSPRNG"

    remote_auth.enable("9900112233")
    try:
        before = remote_auth._load()["secret_key"]
        remote_auth.revoke_all_sessions()
        after = remote_auth._load()["secret_key"]
        assert after != before
        assert len(after) == 64
    finally:
        remote_auth.disable()


def test_audit_chain_key_is_never_returned_by_any_api(client, fake_config):
    key = security_audit._get_key()
    for path in ("/api/security/config", "/api/security/secrets", "/api/security/verify",
                 "/api/security/events", "/api/security/digest"):
        assert key not in client.get(path).text, f"{path} exposed the audit chain key"


# ------------------------------------------------- W2-211/215: env vars ----

def test_env_var_supplies_a_local_key_that_config_json_leaves_blank(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "devices": [{"id": "bulb-1", "name": "Living Room", "local_key": "",
                     "device_id": "dev-1", "ip": "10.0.0.11"}],
        "groups": [],
    }), encoding="utf-8")
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", str(cfg_path))
    monkeypatch.setenv("SBD_LOCAL_KEY_BULB_1", "FromTheEnvironment1")

    device = cfgmod.get_device("bulb-1")
    assert device["local_key"] == "FromTheEnvironment1"
    assert device["_local_key_from_env"] is True


def test_env_sourced_key_is_never_written_back_into_config_json(tmp_path, monkeypatch):
    """The trap this design exists to avoid: load applies the env value, so
    a routine save (renaming a bulb through the UI) would otherwise persist
    the secret straight back into the file it was moved out of."""
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "devices": [{"id": "bulb-1", "name": "Living Room", "local_key": "",
                     "device_id": "dev-1", "ip": "10.0.0.11"}],
        "groups": [],
    }), encoding="utf-8")
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", str(cfg_path))
    monkeypatch.setenv("SBD_LOCAL_KEY_BULB_1", "FromTheEnvironment1")

    device = cfgmod.get_device("bulb-1")
    device["name"] = "Lounge"
    cfgmod.upsert_device(device)

    on_disk = cfg_path.read_text(encoding="utf-8")
    assert "FromTheEnvironment1" not in on_disk
    assert "_local_key_from_env" not in on_disk
    written = json.loads(on_disk)
    assert written["devices"][0]["local_key"] == ""
    assert written["devices"][0]["name"] == "Lounge"
    # ...and it's still applied on the next load.
    assert cfgmod.get_device("bulb-1")["local_key"] == "FromTheEnvironment1"


def test_a_config_json_key_is_left_alone_when_no_env_var_is_set(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "devices": [{"id": "bulb-1", "local_key": "StillInTheFile01"}], "groups": [],  # nosecret: synthetic
    }), encoding="utf-8")
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", str(cfg_path))

    device = cfgmod.get_device("bulb-1")
    assert device["local_key"] == "StillInTheFile01"
    assert "_local_key_from_env" not in device

    cfgmod.upsert_device(device)
    assert json.loads(cfg_path.read_text(encoding="utf-8"))[
        "devices"][0]["local_key"] == "StillInTheFile01"


def test_env_var_name_derivation():
    assert secrets_env.env_var_for_device("bulb-1") == "SBD_LOCAL_KEY_BULB_1"
    assert secrets_env.env_var_for_device("kitchen.top") == "SBD_LOCAL_KEY_KITCHEN_TOP"
    assert secrets_env.env_var_for_device("Bulb 2") == "SBD_LOCAL_KEY_BULB_2"


def test_dotenv_parsing_handles_the_usual_shapes(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# a comment\n"
        "\n"
        "SBD_LOCAL_KEY_BULB_1=plainvalue\n"
        'SBD_LOCAL_KEY_BULB_2="quoted value"\n'
        "export SBD_LOCAL_KEY_BULB_3='single'\n"
        "SBD_LOCAL_KEY_BULB_4=value # trailing comment\n"
        "not a valid line\n",
        encoding="utf-8")

    parsed = secrets_env.parse_env_file(str(env_file))
    assert parsed == {
        "SBD_LOCAL_KEY_BULB_1": "plainvalue",
        "SBD_LOCAL_KEY_BULB_2": "quoted value",
        "SBD_LOCAL_KEY_BULB_3": "single",
        "SBD_LOCAL_KEY_BULB_4": "value",
    }


def test_a_missing_env_file_is_not_an_error(tmp_path):
    assert secrets_env.parse_env_file(str(tmp_path / "nope.env")) == {}
    assert secrets_env.load_env_file(str(tmp_path / "nope.env")) == []


def test_the_real_environment_beats_the_env_file(tmp_path, monkeypatch):
    """A `.env` is a convenience, not an override of what an operator
    explicitly exported (or what a container was started with)."""
    env_file = tmp_path / ".env"
    env_file.write_text("SBD_LOCAL_KEY_BULB_1=from_file\n", encoding="utf-8")
    monkeypatch.setenv("SBD_LOCAL_KEY_BULB_1", "from_real_env")

    applied = secrets_env.load_env_file(str(env_file))

    assert applied == []
    assert os.environ["SBD_LOCAL_KEY_BULB_1"] == "from_real_env"


def test_load_env_file_returns_names_not_values(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("SBD_LOCAL_KEY_BULB_9=supersecretvalue\n", encoding="utf-8")

    applied = secrets_env.load_env_file(str(env_file))

    assert applied == ["SBD_LOCAL_KEY_BULB_9"]
    assert "supersecretvalue" not in json.dumps(applied)


# ---------------------------------------------- W2-223: secret inventory --

def test_secret_inventory_reports_source_but_no_values(client, fake_config):
    resp = client.get("/api/security/secrets")
    assert resp.status_code == 200
    body = resp.json()

    for key in FIXTURE_KEYS:
        assert key not in resp.text
    entry = next(d for d in body["devices"] if d["device_id"] == "bulb-1")
    assert entry["local_key_present"] is True
    assert entry["local_key_source"] == "config.json"
    assert entry["env_var"] == "SBD_LOCAL_KEY_BULB_1"
    # No value, and no length either -- a length is a free hint.
    assert "local_key" not in entry
    assert "length" not in json.dumps(entry)


def test_every_secret_in_this_system_has_a_documented_sensitivity(client):
    """W2-223: the answer to 'what does an attacker get from this one'
    lives in code next to the secret, so a doc can't drift away from it."""
    sensitivity = client.get("/api/security/secrets").json()["sensitivity"]
    assert set(sensitivity) == {"local_key", "pin", "session_secret", "security_audit_key"}
    for name, entry in sensitivity.items():
        assert entry["grants"], f"{name} has no stated impact"
        assert entry["rotate_by"], f"{name} has no rotation procedure"
        assert entry["severity"] in ("high", "critical")
    assert "PBKDF2" in sensitivity["pin"]["stored_in"]


def test_redact_secrets_helper(monkeypatch):
    assert secrets_env.redact_secrets("key=abcd1234", extra=["abcd1234"],
                                      include_config=False) == "key=[redacted]"
    # Too short to mask without shredding unrelated text.
    assert secrets_env.redact_secrets("abc", extra=["abc"], include_config=False) == "abc"
    assert secrets_env.redact_secrets(None) is None
    assert secrets_env.redact_secrets("", extra=["x"]) == ""


# ------------------------------------------------- W2-220: secret scanner --

def _run_scanner(*args):
    return subprocess.run([sys.executable, SCANNER, *args],
                          capture_output=True, text=True)


def test_the_scanner_exists_and_this_repo_scans_clean():
    """If this ever fails, a real secret has been committed -- treat it as
    an incident, not a flaky test."""
    assert os.path.isfile(SCANNER)
    result = _run_scanner()
    assert result.returncode == 0, result.stdout


def test_the_scanner_catches_a_committed_config_json(tmp_path):
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "config.json").write_text(
        json.dumps({"devices": [{"local_key": "Rb3xK9pQ2mZ7wT4a"}]}), encoding="utf-8")  # nosecret: synthetic

    result = _run_scanner("--root", str(tmp_path))
    assert result.returncode == 1
    assert "backend/config.json" in result.stdout
    assert "never be committed" in result.stdout


def test_the_scanner_catches_a_real_looking_key_pasted_into_a_normal_file(tmp_path):
    (tmp_path / "notes.md").write_text(
        'the key is "local_key": "Rb3xK9pQ2mZ7wT4a" for the lounge bulb\n', encoding="utf-8")  # nosecret: synthetic

    result = _run_scanner("--root", str(tmp_path))
    assert result.returncode == 1
    assert "real Tuya local_key" in result.stdout


def test_the_scanner_ignores_placeholders_and_honours_a_waiver(tmp_path):
    (tmp_path / "example.json").write_text(json.dumps({
        "local_key": "REPLACE_WITH_LOCAL_KEY"}), encoding="utf-8")
    (tmp_path / "docs.md").write_text('"local_key": "PASTE_LOCAL_KEY_HERE"\n', encoding="utf-8")
    (tmp_path / "waived.py").write_text(
        'KEY = "local_key": "Rb3xK9pQ2mZ7wT4a"  # nosecret: fixture\n', encoding="utf-8")

    result = _run_scanner("--root", str(tmp_path))
    assert result.returncode == 0, result.stdout


def test_the_scanner_catches_a_signing_key_or_data_directory(tmp_path):
    (tmp_path / "leak.json").write_text(json.dumps({"secret_key": "ab" * 32}), encoding="utf-8")
    result = _run_scanner("--root", str(tmp_path))
    assert result.returncode == 1
    assert "signing key" in result.stdout

    other = tmp_path / "other"
    (other / "backend" / "data").mkdir(parents=True)
    (other / "backend" / "data" / "remote_auth.json").write_text("{}", encoding="utf-8")
    result = _run_scanner("--root", str(other))
    assert result.returncode == 1
    assert "backend/data/remote_auth.json" in result.stdout


# --------------------------------------------------- repo hygiene checks --

def test_env_example_is_committed_and_holds_no_real_values():
    example = os.path.join(REPO_ROOT, ".env.example")
    assert os.path.isfile(example), ".env.example is part of W2-215"
    text = open(example, encoding="utf-8").read()

    assert "SBD_LOCAL_KEY_" in text
    # Every assignment must be commented out, so copying the file to `.env`
    # can never silently activate a placeholder key.
    for line in text.splitlines():
        stripped = line.strip()
        if "=" in stripped and not stripped.startswith("#"):
            pytest.fail(f"uncommented assignment in .env.example: {stripped}")


def test_gitignore_covers_env_files_and_backups():
    ignored = open(os.path.join(REPO_ROOT, ".gitignore"), encoding="utf-8").read()
    for pattern in (".env", "backend/config.json", "backend/data/", "backend/backups/"):
        assert pattern in ignored, f"{pattern} is not git-ignored"


def test_cryptography_is_a_declared_dependency():
    """backup_restore.py imports AESGCM directly; relying on it arriving
    via tinytuya's own requirements is how encrypted backups break on an
    unrelated dependency bump."""
    requirements = open(os.path.join(REPO_ROOT, "backend", "requirements.txt"),
                        encoding="utf-8").read()
    assert "cryptography==" in requirements
