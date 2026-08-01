"""Tests for backup / restore (Week 2, W2-161..175).

The `backup_isolation` fixture in conftest.py points every path this module
touches at a pytest tmp dir, so no test reads the real backend/config.json
or writes an archive into the repo.

The load-bearing test in here is
`test_restore_never_changes_remote_access_state` (W2-175) -- everything
else can be re-derived from the code, but that one is a promise about what
a restore is allowed to do to the security posture of a live install.
"""
import json
import os
import zipfile

import pytest

import backup_restore as br
import security_audit


LOCAL_KEY = "Rb3xK9pQ2mZ7wT4a"  # nosecret: synthetic 16-char key, shaped like a real one

SAMPLE_CONFIG = {
    "devices": [
        {"id": "bulb-1", "name": "Living Room", "device_id": "dev-1",
         "local_key": LOCAL_KEY, "ip": "10.0.0.11", "version": 3.3},
    ],
    "groups": [{"id": "all", "name": "All Bulbs", "device_ids": ["bulb-1"]}],
    "zones": [],
    "orchestration_presets": [],
    "audio_input_calibrations": [],
}


@pytest.fixture
def populated(backup_isolation):
    """A realistic install: a config with a device credential, some runtime
    data, remote_auth state, and a live security-audit chain."""
    root = backup_isolation
    (root / "config.json").write_text(json.dumps(SAMPLE_CONFIG, indent=2), encoding="utf-8")
    data = root / "data"
    (data / "favorites.json").write_text(
        json.dumps({"bulb-1": [{"id": "f1", "name": "Warm", "rgb": [255, 180, 100]}]}),
        encoding="utf-8")
    (data / "schedules.json").write_text(json.dumps([{"id": "r1", "time": "07:00"}]),
                                         encoding="utf-8")
    (data / "discovery.json").write_text(json.dumps({"discovered": []}), encoding="utf-8")
    (data / "lightshows").mkdir()
    (data / "lightshows" / "show1.json").write_text(json.dumps({"frames": []}), encoding="utf-8")
    (data / "remote_auth.json").write_text(json.dumps({
        "enabled": True, "pin_hash": "a" * 64, "salt": "b" * 32,
        "secret_key": "c" * 64, "sessions": {}, "session_ttl_s": 86400,
    }), encoding="utf-8")
    (data / "security_events.log").write_text('{"seq": 1}\n', encoding="utf-8")
    (data / "security_audit_key").write_text("d" * 64, encoding="utf-8")
    (data / "security_audit_state.json").write_text('{"seq": 1}', encoding="utf-8")
    return root


def _names_in(path):
    with zipfile.ZipFile(path) as zf:
        return set(zf.namelist())


# ---------------------------------------------------------------- create --

def test_backup_contains_config_and_data_with_a_manifest(populated):
    result = br.create_backup()

    assert result["ok"] is True
    assert result["name"].endswith(".zip")
    names = _names_in(result["path"])
    assert "manifest.json" in names
    assert "config.json" in names
    assert "data/favorites.json" in names
    assert "data/lightshows/show1.json" in names

    manifest = result["manifest"]
    assert manifest["format_version"] == br.FORMAT_VERSION
    assert manifest["encrypted"] is False
    assert set(manifest["files"]) >= {"config.json", "data/favorites.json",
                                      "remote_auth_settings.json"}


def test_unencrypted_backup_returns_an_explicit_credential_warning(populated):
    """The tradeoff has to be visible where the choice is made, not only in
    the docs -- by the time someone reads the docs the plaintext archive is
    already in their Downloads folder."""
    result = br.create_backup()

    assert result["manifest"]["contains_device_credentials"] is True
    assert "warning" in result
    assert "NOT encrypted" in result["warning"]
    assert "local_key" in result["warning"]


def test_encrypted_backup_has_no_warning_and_is_not_a_readable_zip(populated):
    result = br.create_backup(password="correct horse battery staple")

    assert result["encrypted"] is True
    assert "warning" not in result
    assert result["name"].endswith(".sbdb")
    assert br.is_encrypted_file(result["path"]) is True
    with pytest.raises(zipfile.BadZipFile):
        zipfile.ZipFile(result["path"])

    # And the key must not be sitting in the file in the clear.
    with open(result["path"], "rb") as f:
        blob = f.read()
    assert LOCAL_KEY.encode() not in blob


def test_backup_never_includes_auth_secrets_or_the_audit_chain(populated):
    """remote_auth.json carries the PIN hash and the session signing key;
    the audit chain files are how tampering is detected. Neither belongs in
    an archive that might be unencrypted and might be restored."""
    result = br.create_backup()
    names = _names_in(result["path"])

    assert "data/remote_auth.json" not in names
    assert "data/security_events.log" not in names
    assert "data/security_audit_key" not in names
    assert "data/security_audit_state.json" not in names

    with zipfile.ZipFile(result["path"]) as zf:
        blob = b"".join(zf.read(n) for n in names)
    assert b"a" * 64 not in blob, "PIN hash leaked into the archive"
    assert b"c" * 64 not in blob, "session signing key leaked into the archive"

    # The non-secret settings ARE recorded, clearly labelled as reference.
    with zipfile.ZipFile(result["path"]) as zf:
        snapshot = json.loads(zf.read("remote_auth_settings.json"))
    assert snapshot["enabled"] is True
    assert "pin_hash" not in snapshot and "secret_key" not in snapshot


def test_exclusions_leave_the_named_items_out(populated):
    result = br.create_backup(exclude=["lightshows", "discovery.json"])
    names = _names_in(result["path"])

    assert not any(n.startswith("data/lightshows") for n in names)
    assert "data/discovery.json" not in names
    assert "data/favorites.json" in names
    assert result["manifest"]["excluded"] == ["discovery.json", "lightshows"]


def test_excluding_something_unknown_is_an_error(populated):
    with pytest.raises(ValueError, match="unknown item"):
        br.create_backup(exclude=["not-a-real-file.json"])


def test_creating_a_backup_logs_a_security_event(populated):
    br.create_backup()
    events = [e["event"] for e in security_audit.read_events()]
    assert "backup_created" in events


# -------------------------------------------------------------- verify ----

def test_verify_passes_on_a_fresh_backup(populated):
    name = br.create_backup()["name"]
    result = br.verify_backup(name)

    assert result["ok"] is True
    assert result["files_checked"] >= 3
    assert result["reason"] is None


def test_verify_detects_a_modified_file_inside_the_archive(populated):
    """W2-168: an archive whose contents no longer match the manifest must
    never be offered as restorable."""
    name = br.create_backup()["name"]
    path = os.path.join(br.BACKUP_DIR, name)

    with zipfile.ZipFile(path) as zf:
        entries = {n: zf.read(n) for n in zf.namelist()}
    entries["config.json"] = json.dumps({"devices": [{"id": "attacker"}]}).encode()
    with zipfile.ZipFile(path, "w") as zf:
        for arcname, data in entries.items():
            zf.writestr(arcname, data)

    result = br.verify_backup(name)
    assert result["ok"] is False
    assert "checksum mismatch" in result["reason"]


def test_verify_detects_a_truncated_archive(populated):
    name = br.create_backup()["name"]
    path = os.path.join(br.BACKUP_DIR, name)
    with open(path, "r+b") as f:
        f.truncate(os.path.getsize(path) // 2)

    result = br.verify_backup(name)
    assert result["ok"] is False


def test_verify_of_an_encrypted_backup_asks_for_a_password(populated):
    name = br.create_backup(password="hunter2hunter2")["name"]

    without = br.verify_backup(name)
    assert without["ok"] is False
    assert without["needs_password"] is True
    assert without["encrypted"] is True

    assert br.verify_backup(name, password="hunter2hunter2")["ok"] is True


def test_wrong_password_is_reported_without_an_oracle(populated):
    """A wrong password and a tampered file both fail AEAD authentication
    and must stay indistinguishable in the message."""
    name = br.create_backup(password="hunter2hunter2")["name"]
    result = br.verify_backup(name, password="not the password")

    assert result["ok"] is False
    assert "wrong password, or the archive has been modified" in result["reason"]


def test_encrypted_archive_rejects_a_tampered_header(populated):
    """The KDF parameters are AEAD associated data, so swapping the
    iteration count down can't be done without failing the tag."""
    name = br.create_backup(password="hunter2hunter2")["name"]
    path = os.path.join(br.BACKUP_DIR, name)
    with open(path, "rb") as f:
        blob = f.read()
    header, ciphertext = br._split_encrypted(blob)
    weakened = json.loads(header).copy()
    weakened["iterations"] = 1
    tampered = br.MAGIC + json.dumps(weakened, sort_keys=True).encode() + b"\n" + ciphertext
    with open(path, "wb") as f:
        f.write(tampered)

    assert br.verify_backup(name, password="hunter2hunter2")["ok"] is False


def test_backup_names_cannot_escape_the_backup_directory(populated):
    """The name arrives from a URL path, so traversal has to be rejected
    rather than merely unlikely."""
    for bad in ("../config.json", "..", "a/b", "/etc/passwd", ""):
        with pytest.raises((ValueError, FileNotFoundError)):
            br.verify_backup(bad)


# ------------------------------------------------------------- restore ----

def test_restore_refuses_without_explicit_confirmation(populated):
    name = br.create_backup()["name"]
    with pytest.raises(PermissionError, match="confirm"):
        br.restore_backup(name)


def test_restore_refuses_a_backup_that_fails_its_integrity_check(populated):
    name = br.create_backup()["name"]
    path = os.path.join(br.BACKUP_DIR, name)
    with open(path, "r+b") as f:
        f.truncate(64)

    with pytest.raises(ValueError, match="integrity check"):
        br.restore_backup(name, confirm=True)

    events = [e["event"] for e in security_audit.read_events()]
    assert "backup_restore_rejected" in events


def test_full_restore_puts_config_and_data_back(populated):
    name = br.create_backup()["name"]

    # Wreck the live state.
    (populated / "config.json").write_text(json.dumps({"devices": []}), encoding="utf-8")
    os.remove(populated / "data" / "favorites.json")

    result = br.restore_backup(name, confirm=True)

    assert result["ok"] is True
    restored_cfg = json.loads((populated / "config.json").read_text(encoding="utf-8"))
    assert restored_cfg["devices"][0]["id"] == "bulb-1"
    assert restored_cfg["devices"][0]["local_key"] == LOCAL_KEY
    assert (populated / "data" / "favorites.json").exists()
    assert result["touched_device_credentials"] is True


def test_restore_takes_a_pre_restore_safety_backup_first(populated):
    """W2-172: a restore is itself destructive and deserves the same undo
    it provides."""
    name = br.create_backup()["name"]
    (populated / "config.json").write_text(
        json.dumps({"devices": [{"id": "only-here-now"}]}), encoding="utf-8")

    result = br.restore_backup(name, confirm=True)

    safety = result["safety_backup"]
    assert safety and safety != name
    with zipfile.ZipFile(os.path.join(br.BACKUP_DIR, safety)) as zf:
        captured = json.loads(zf.read("config.json"))
    assert captured["devices"][0]["id"] == "only-here-now"


def test_restore_never_changes_remote_access_state(populated):
    """W2-175, both directions.

    The archive was taken while the gate was ENABLED. Restoring it onto an
    install where the gate is DISABLED must not turn it back on -- and the
    reverse must not turn it off. Structurally guaranteed (nothing here
    writes remote_auth.json) but asserted anyway, because 'we just don't
    touch that file' is exactly the kind of invariant a future refactor
    breaks silently.
    """
    auth_path = populated / "data" / "remote_auth.json"
    name = br.create_backup()["name"]
    with zipfile.ZipFile(os.path.join(br.BACKUP_DIR, name)) as zf:
        assert json.loads(zf.read("remote_auth_settings.json"))["enabled"] is True

    # gate now OFF locally; restoring a backup taken while it was ON
    state = json.loads(auth_path.read_text(encoding="utf-8"))
    state["enabled"] = False
    auth_path.write_text(json.dumps(state), encoding="utf-8")

    result = br.restore_backup(name, confirm=True)

    assert result["remote_access"]["enabled_before"] is False
    assert result["remote_access"]["enabled_after"] is False
    assert result["remote_access"]["changed"] is False
    assert json.loads(auth_path.read_text(encoding="utf-8"))["enabled"] is False
    # The secrets in that file must survive untouched too.
    assert json.loads(auth_path.read_text(encoding="utf-8"))["secret_key"] == "c" * 64

    # ...and the other direction: gate ON, restore a backup, still ON.
    state["enabled"] = True
    auth_path.write_text(json.dumps(state), encoding="utf-8")
    again = br.restore_backup(name, confirm=True)
    assert again["remote_access"] == {"enabled_before": True, "enabled_after": True,
                                      "changed": False}


def test_restore_does_not_rewind_the_security_event_log(populated):
    """Restoring the audit log would be a one-click way to erase the trail
    the tamper-evidence exists to protect."""
    name = br.create_backup()["name"]
    (populated / "data" / "security_events.log").write_text(
        '{"seq": 1}\n{"seq": 2}\n', encoding="utf-8")

    br.restore_backup(name, confirm=True)

    assert (populated / "data" / "security_events.log").read_text(
        encoding="utf-8").count("\n") == 2


def test_selective_restore_leaves_device_credentials_alone(populated):
    """W2-167, the important half: bring favourites and schedules back
    without the archive's device list (or its local_keys) overwriting the
    current one."""
    name = br.create_backup()["name"]

    live_cfg = json.loads(json.dumps(SAMPLE_CONFIG))
    live_cfg["devices"][0]["local_key"] = "NewKeyAfterRepair1"  # nosecret: synthetic
    live_cfg["devices"].append({"id": "bulb-2", "local_key": "Second1234567890"})  # nosecret
    (populated / "config.json").write_text(json.dumps(live_cfg), encoding="utf-8")
    (populated / "data" / "favorites.json").write_text(json.dumps({}), encoding="utf-8")

    result = br.restore_backup(name, confirm=True, sections=["favorites", "schedules"])

    assert result["touched_device_credentials"] is False
    after = json.loads((populated / "config.json").read_text(encoding="utf-8"))
    assert after["devices"][0]["local_key"] == "NewKeyAfterRepair1"
    assert len(after["devices"]) == 2, "selective restore must not drop a newer device"
    assert json.loads((populated / "data" / "favorites.json").read_text(
        encoding="utf-8"))["bulb-1"][0]["name"] == "Warm"


def test_selective_restore_of_groups_merges_without_touching_devices(populated):
    name = br.create_backup()["name"]
    live_cfg = json.loads(json.dumps(SAMPLE_CONFIG))
    live_cfg["groups"] = []
    live_cfg["devices"][0]["local_key"] = "KeepThisKey12345"  # nosecret: synthetic
    (populated / "config.json").write_text(json.dumps(live_cfg), encoding="utf-8")

    result = br.restore_backup(name, confirm=True, sections=["groups_zones"])

    after = json.loads((populated / "config.json").read_text(encoding="utf-8"))
    assert [g["id"] for g in after["groups"]] == ["all"]
    assert after["devices"][0]["local_key"] == "KeepThisKey12345"
    assert result["restored_config_keys"] == ["groups", "zones", "orchestration_presets"]


def test_selective_restore_of_devices_is_the_one_that_touches_credentials(populated):
    name = br.create_backup()["name"]
    live_cfg = json.loads(json.dumps(SAMPLE_CONFIG))
    live_cfg["devices"][0]["local_key"] = "WillBeOverwritten1"  # nosecret: synthetic
    (populated / "config.json").write_text(json.dumps(live_cfg), encoding="utf-8")

    result = br.restore_backup(name, confirm=True, sections=["devices"])

    assert result["touched_device_credentials"] is True
    after = json.loads((populated / "config.json").read_text(encoding="utf-8"))
    assert after["devices"][0]["local_key"] == LOCAL_KEY


def test_restoring_lightshows_brings_back_a_whole_directory(populated):
    name = br.create_backup()["name"]
    os.remove(populated / "data" / "lightshows" / "show1.json")

    br.restore_backup(name, confirm=True, sections=["lightshows"])

    assert (populated / "data" / "lightshows" / "show1.json").exists()


def test_unknown_section_is_rejected(populated):
    name = br.create_backup()["name"]
    with pytest.raises(ValueError, match="unknown section"):
        br.restore_backup(name, confirm=True, sections=["everything"])
    with pytest.raises(ValueError, match="no sections"):
        br.restore_backup(name, confirm=True, sections=[])


def test_encrypted_restore_round_trip(populated):
    password = "a long enough passphrase"
    name = br.create_backup(password=password)["name"]
    (populated / "config.json").write_text(json.dumps({"devices": []}), encoding="utf-8")

    with pytest.raises(PermissionError):
        br.restore_backup(name, confirm=True)

    br.restore_backup(name, confirm=True, password=password)
    after = json.loads((populated / "config.json").read_text(encoding="utf-8"))
    assert after["devices"][0]["local_key"] == LOCAL_KEY


def test_restore_refuses_to_write_outside_the_data_directory(populated):
    """Zip-slip: an archive is untrusted input even when the operator
    supplied it."""
    path = os.path.join(br.BACKUP_DIR, "evil.zip")
    manifest = {"format_version": 1, "files": {}}
    with zipfile.ZipFile(path, "w") as zf:
        payload = b"pwned"
        import hashlib
        manifest["files"]["data/../../escaped.txt"] = {
            "sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload)}
        zf.writestr("data/../../escaped.txt", payload)
        zf.writestr("manifest.json", json.dumps(manifest))

    with pytest.raises(ValueError, match="outside data/"):
        br.restore_backup("evil.zip", confirm=True)
    assert not os.path.exists(os.path.join(os.path.dirname(str(populated)), "escaped.txt"))


def test_restore_logs_a_security_event(populated):
    name = br.create_backup()["name"]
    br.restore_backup(name, confirm=True)

    entry = next(e for e in security_audit.read_events() if e["event"] == "backup_restored")
    assert entry["severity"] == "warning"
    assert entry["detail"]["name"] == name


# ------------------------------------------------------------ versioning --

def test_versioning_keeps_only_the_last_n(populated):
    br.update_settings(keep=3)
    for i in range(5):
        br.create_backup(note=f"n{i}")

    backups = br.list_backups()
    assert len(backups) == 3
    # Newest kept, oldest dropped.
    assert backups[0]["manifest"]["note"] == "n4"


def test_keep_setting_is_validated_and_persisted(populated):
    assert br.update_settings(keep=7)["keep"] == 7
    assert br.get_settings()["keep"] == 7
    with pytest.raises(ValueError):
        br.update_settings(keep=0)


def test_deleting_a_backup_overwrites_it_first(populated):
    """W2-219, with the honest caveat in the code: this removes the
    plaintext from the obvious place, it does not defeat wear levelling."""
    name = br.create_backup()["name"]
    path = os.path.join(br.BACKUP_DIR, name)
    br.delete_backup(name)

    assert not os.path.exists(path)
    assert "backup_deleted" in [e["event"] for e in security_audit.read_events()]


# ------------------------------------------------------------------ diff --

def test_diff_reports_changes_without_leaking_any_local_key(populated):
    """W2-171. A diff view that prints the two key values would undo every
    other bit of redaction in this app."""
    name = br.create_backup()["name"]
    live_cfg = json.loads(json.dumps(SAMPLE_CONFIG))
    live_cfg["devices"][0]["local_key"] = "RotatedKey123456"  # nosecret: synthetic
    live_cfg["devices"][0]["name"] = "Lounge"
    live_cfg["devices"].append({"id": "bulb-2", "local_key": "Another123456789"})  # nosecret
    live_cfg["groups"] = []
    (populated / "config.json").write_text(json.dumps(live_cfg), encoding="utf-8")

    diff = br.diff_backup(name)

    assert diff["devices"]["removed"] == ["bulb-2"]
    changed = diff["devices"]["changed"][0]
    assert changed["id"] == "bulb-1"
    assert changed["local_key_changed"] is True
    assert "name" in changed["fields_changed"]
    assert diff["groups"]["added"] == ["all"]

    blob = json.dumps(diff)
    assert LOCAL_KEY not in blob
    assert "RotatedKey123456" not in blob
    assert "Another123456789" not in blob


def test_preflight_bundles_verification_diff_and_sections(populated):
    name = br.create_backup()["name"]
    preflight = br.restore_preflight(name)

    assert preflight["verification"]["ok"] is True
    assert preflight["diff"]["config_in_backup"] is True
    section_ids = {s["id"] for s in preflight["sections"]}
    assert {"devices", "favorites", "schedules"} <= section_ids
    devices_section = next(s for s in preflight["sections"] if s["id"] == "devices")
    assert devices_section["touches_credentials"] is True


# ------------------------------------------------------------------ API ----

def test_backup_api_create_list_and_download(populated, client):
    created = client.post("/api/backups", json={}).json()
    assert created["ok"] is True
    assert "warning" in created

    listing = client.get("/api/backups").json()
    assert listing["backups"][0]["name"] == created["name"]
    assert listing["settings"]["keep"] == br._DEFAULT_SETTINGS["keep"]

    download = client.get(f"/api/backups/{created['name']}/download")
    assert download.status_code == 200
    assert "attachment; filename=" in download.headers["content-disposition"]
    assert download.content[:2] == b"PK"


def test_backup_api_options_lists_what_is_never_included(populated, client):
    options = client.get("/api/backups/options").json()
    assert "remote_auth.json" in options["never_included"]
    assert any(e["name"] == "lightshows" for e in options["exclusions"])
    assert any(s["id"] == "favorites" for s in options["sections"])


def test_backup_api_restore_requires_confirm_and_reports_remote_access(populated, client):
    name = client.post("/api/backups", json={}).json()["name"]

    unconfirmed = client.post(f"/api/backups/{name}/restore", json={"confirm": False})
    assert unconfirmed.status_code == 409

    confirmed = client.post(f"/api/backups/{name}/restore", json={"confirm": True})
    assert confirmed.status_code == 200
    assert confirmed.json()["remote_access"]["changed"] is False


def test_backup_api_verify_and_preflight_take_the_password_in_a_body(populated, client):
    """Not a query string: a password there lands in access logs, browser
    history and referrer headers."""
    name = client.post("/api/backups", json={"password": "topsecretpassword"}).json()["name"]

    assert client.post(f"/api/backups/{name}/verify", json={}).json()["needs_password"] is True
    ok = client.post(f"/api/backups/{name}/verify", json={"password": "topsecretpassword"})
    assert ok.json()["ok"] is True

    preflight = client.post(f"/api/backups/{name}/preflight",
                            json={"password": "topsecretpassword"})
    assert preflight.json()["verification"]["ok"] is True


def test_backup_api_rejects_a_traversal_name(populated, client):
    assert client.post("/api/backups/..%2Fconfig.json/verify", json={}).status_code in (400, 404)
    assert client.delete("/api/backups/nope.zip").status_code == 404


def test_backup_api_settings_round_trip(populated, client):
    assert client.post("/api/backups/settings", json={"keep": 4}).json()["keep"] == 4
    assert client.get("/api/backups/settings").json()["keep"] == 4
    assert client.post("/api/backups/settings", json={"keep": 0}).status_code == 400
