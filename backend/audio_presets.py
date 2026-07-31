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

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)
SESSION_PRESETS_PATH = os.path.join(DATA_DIR, "audio_session_presets.json")
LAST_SESSION_PATH = os.path.join(DATA_DIR, "audio_last_session.json")

_lock = threading.Lock()

# The full set of fields a session preset / last-session record carries.
# Anything else passed in is dropped so the stored shape stays predictable.
CONFIG_FIELDS = (
    "device_index", "mode", "sensitivity", "monochrome_hue", "n_bands",
    "min_dwell_ms", "max_duration_s", "warmup_s", "auto_resume_grace_s",
    "max_flash_rate_hz", "disable_flash_heavy",
)


def _sanitize_config(config):
    return {k: config[k] for k in CONFIG_FIELDS if k in config and config[k] is not None}


def _load_presets():
    if not os.path.exists(SESSION_PRESETS_PATH):
        return []
    try:
        with open(SESSION_PRESETS_PATH, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def _save_presets(presets):
    with open(SESSION_PRESETS_PATH, "w") as f:
        json.dump(presets, f, indent=2)


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


# --- "resume last session" ---------------------------------------------------
def save_last_session(device_id, config):
    """Persisted every time a session starts successfully, so a one-click
    'resume last session' can bring the bulb back to the same mode/config
    after a backend restart, without the user having to remember it."""
    with _lock:
        record = {"device_id": device_id, "config": _sanitize_config(config), "saved_at": time.time()}
        with open(LAST_SESSION_PATH, "w") as f:
            json.dump(record, f, indent=2)
        return record


def load_last_session(device_id=None):
    if not os.path.exists(LAST_SESSION_PATH):
        return None
    try:
        with open(LAST_SESSION_PATH, "r") as f:
            record = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    if device_id and record.get("device_id") != device_id:
        return None
    return record


def clear_last_session():
    with _lock:
        if os.path.exists(LAST_SESSION_PATH):
            os.remove(LAST_SESSION_PATH)
