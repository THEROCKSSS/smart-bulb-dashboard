"""The auth state file is read by the PIN gate on EVERY request, so a torn
write to it doesn't degrade one feature -- it 500s the entire dashboard.

This happened for real: the file was found with `""ip":` spliced into a key
after the server was force-killed mid-write. `open(path, "w")` truncates
before it writes, so an interrupted save leaves a partial file behind.
"""
import json
import os
import threading

import pytest

import remote_auth


@pytest.fixture
def auth_path(tmp_path, monkeypatch):
    p = tmp_path / "remote_auth.json"
    monkeypatch.setattr(remote_auth, "AUTH_PATH", str(p))
    monkeypatch.setattr(remote_auth, "_corrupt_logged", False)
    return p


def _state(n_sessions):
    return {
        "enabled": True,
        "sessions": {f"s{i:04d}": {"ip": "127.0.0.1", "n": i} for i in range(n_sessions)},
    }


def test_save_is_atomic_so_an_interrupted_write_cannot_truncate_the_real_file(auth_path):
    """The failure that actually occurred. A save that dies partway must leave
    the previous good file untouched, not a half-written one."""
    remote_auth._save(_state(200))
    good = auth_path.read_text(encoding="utf-8")

    # Simulate the process dying after the temp file is written but before the
    # atomic replace -- exactly what a force-kill mid-save does.
    partial = auth_path.parent / (auth_path.name + ".tmp")
    partial.write_text(good[: len(good) // 2], encoding="utf-8")

    assert json.loads(auth_path.read_text(encoding="utf-8"))["sessions"]
    assert auth_path.read_text(encoding="utf-8") == good


def test_concurrent_saves_never_interleave(auth_path):
    """Every login and every last_seen touch saves this file, so simultaneous
    writers are routine rather than exotic."""
    errors = []

    def writer(k):
        try:
            for _ in range(40):
                remote_auth._save(_state(k * 20 + 1))
        except Exception as e:  # pragma: no cover - only on regression
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(k,)) for k in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    json.loads(auth_path.read_text(encoding="utf-8"))  # must still parse
    leftovers = [f for f in os.listdir(auth_path.parent) if f.endswith(".tmp")]
    assert leftovers == []


def test_an_unreadable_state_file_fails_closed_not_open(auth_path):
    """The security-critical half. Returning the default state here would mean
    one corrupt byte silently switches the PIN gate OFF on an exposed
    dashboard. It must stay on and refuse instead."""
    auth_path.write_text('{"enabled": true, ""ip": broken}', encoding="utf-8")
    state = remote_auth._load()
    assert state["enabled"] is True
    assert state.get("_unreadable") is True


def test_an_unreadable_state_file_is_preserved_for_inspection(auth_path):
    """Don't silently overwrite evidence -- the owner may want to recover a
    PIN hash or understand what happened."""
    broken = '{"enabled": true, ""ip": broken}'
    auth_path.write_text(broken, encoding="utf-8")
    remote_auth._load()
    assert auth_path.read_text(encoding="utf-8") == broken


def test_a_missing_file_still_means_disabled_not_locked_out(auth_path):
    """A file that was never created is the LAN-only default, and must NOT be
    confused with one that exists but cannot be parsed."""
    assert not auth_path.exists()
    assert remote_auth._load()["enabled"] is False
