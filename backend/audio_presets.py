"""Audio-reactive session presets + last-known-good session persistence
(Week 1 Phase D, section 8). Named "session presets" throughout (routes,
storage file) to avoid colliding with the existing color `PRESET_COLORS`
concept in scenes_presets.py, which is a completely different feature.

Both are plain on-disk JSON, matching the convention already used by
schedule_engine.py (backend/data/*.json, no DB).
"""
import json
import os
import threading
import time
import uuid
import tempfile

import audio_settings

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)
SESSION_PRESETS_PATH = os.path.join(DATA_DIR, "audio_session_presets.json")
LAST_SESSION_PATH = os.path.join(DATA_DIR, "audio_last_session.json")

_lock = threading.RLock()

# The full set of fields a session preset / last-session record carries.
# Anything else passed in is dropped so the stored shape stays predictable.
CONFIG_FIELDS = tuple(audio_settings.DEFAULTS)


def _atomic_write(path, value):
    """A failed write leaves the last complete settings file readable."""
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=folder,
                                         prefix=".audio-", suffix=".tmp", delete=False) as f:
            temporary = f.name
            json.dump(value, f, indent=2, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def _sanitize_config(config):
    return audio_settings.normalize({k: config[k] for k in CONFIG_FIELDS if k in config})


def _load_presets():
    if not os.path.exists(SESSION_PRESETS_PATH):
        return []
    try:
        with open(SESSION_PRESETS_PATH, "r") as f:
            presets = json.load(f)
            for preset in presets:
                preset["config"] = _sanitize_config(preset.get("config", {}))
            return presets
    except (json.JSONDecodeError, OSError):
        return []


def _save_presets(presets):
    _atomic_write(SESSION_PRESETS_PATH, presets)


def save_preset(name, device_id, config):
    """Save an entire running (or about-to-run) session's config as a named,
    reusable preset. `config` should carry the fields in CONFIG_FIELDS;
    unknown keys are dropped, missing ones simply aren't stored (callers /
    the schedule-engine action apply their own defaults on load)."""
    with _lock:
        presets = _load_presets()
        preset = {
            "id": str(uuid.uuid4())[:8],
            "name": name,
            "device_id": device_id,
            "config": _sanitize_config(config),
            "created_at": time.time(),
        }
        presets.append(preset)
        _save_presets(presets)
        return preset


def list_presets(device_id=None):
    presets = _load_presets()
    if device_id:
        return [p for p in presets if p["device_id"] == device_id]
    return presets


def get_preset(preset_id):
    for p in _load_presets():
        if p["id"] == preset_id:
            return p
    return None


def delete_preset(preset_id):
    with _lock:
        presets = _load_presets()
        remaining = [p for p in presets if p["id"] != preset_id]
        found = len(remaining) != len(presets)
        _save_presets(remaining)
        return found


def update_preset(preset_id, name=None, config=None):
    with _lock:
        presets = _load_presets()
        preset = next((p for p in presets if p["id"] == preset_id), None)
        if preset is None:
            return None
        if name is not None:
            preset["name"] = name
        if config is not None:
            preset["config"] = _sanitize_config(config)
        preset["updated_at"] = time.time()
        _save_presets(presets)
        return preset


# --- "resume last session" ---------------------------------------------------
def save_last_session(device_id, config):
    """Persisted every time a session starts successfully, so a one-click
    'resume last session' can bring the bulb back to the same mode/config
    after a backend restart, without the user having to remember it."""
    with _lock:
        record = {"device_id": device_id, "config": _sanitize_config(config), "saved_at": time.time()}
        records = _load_session_records()
        records[device_id] = record
        _atomic_write(LAST_SESSION_PATH, {"schema_version": 2, "devices": records})
        return record


def _load_session_records():
    if not os.path.exists(LAST_SESSION_PATH):
        return {}
    try:
        with open(LAST_SESSION_PATH, "r") as f:
            record = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(record, dict):
        return {}
    if record.get("device_id") and isinstance(record.get("config"), dict):
        return {record["device_id"]: record}  # compatible legacy single-bulb record
    return record.get("devices", {})


def load_last_session(device_id=None):
    with _lock:
        records = _load_session_records()
    if device_id:
        return records.get(device_id)
    return max(records.values(), key=lambda r: r.get("saved_at", 0), default=None)


def clear_last_session():
    with _lock:
        if os.path.exists(LAST_SESSION_PATH):
            os.remove(LAST_SESSION_PATH)
