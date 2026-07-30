import json
import os
import threading

CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
EXAMPLE_PATH = os.path.join(CONFIG_DIR, "config.example.json")

_lock = threading.Lock()


def _default_config():
    return {
        "devices": [], "groups": [], "zones": [],
        "orchestration_presets": [], "audio_input_calibrations": [],
    }


def load_config():
    with _lock:
        if not os.path.exists(CONFIG_PATH):
            if os.path.exists(EXAMPLE_PATH):
                raise FileNotFoundError(
                    "config.json not found. Copy config.example.json to config.json "
                    "and fill in your device_id/local_key/ip. See SETUP.md."
                )
            data = _default_config()
            save_config(data)
            return data
        with open(CONFIG_PATH, "r") as f:
            return json.load(f)


def save_config(data):
    with _lock:
        with open(CONFIG_PATH, "w") as f:
            json.dump(data, f, indent=2)


def get_device(device_id):
    cfg = load_config()
    for d in cfg["devices"]:
        if d["id"] == device_id:
            return d
    return None


def upsert_device(device):
    cfg = load_config()
    for i, d in enumerate(cfg["devices"]):
        if d["id"] == device["id"]:
            cfg["devices"][i] = device
            save_config(cfg)
            return
    cfg["devices"].append(device)
    save_config(cfg)


def delete_device(device_id):
    cfg = load_config()
    cfg["devices"] = [d for d in cfg["devices"] if d["id"] != device_id]
    save_config(cfg)


def redact(device):
    d = dict(device)
    if "local_key" in d:
        d["local_key"] = "•" * len(d["local_key"])
    return d


# ------------------------------------------------------------------ zones --
# A Zone sits ABOVE groups (e.g. "Living Room" containing several groups
# and/or loose bulbs) -- the same on-disk shape as `groups` (a list of
# dicts in config.json), just with an extra `group_ids` field so a zone can
# reference existing groups instead of only listing raw device ids.

def get_zone(zone_id):
    cfg = load_config()
    for z in cfg.get("zones", []):
        if z["id"] == zone_id:
            return z
    return None


def upsert_zone(zone):
    cfg = load_config()
    zones = cfg.setdefault("zones", [])
    for i, z in enumerate(zones):
        if z["id"] == zone["id"]:
            zones[i] = zone
            save_config(cfg)
            return
    zones.append(zone)
    save_config(cfg)


def delete_zone(zone_id):
    cfg = load_config()
    cfg["zones"] = [z for z in cfg.get("zones", []) if z["id"] != zone_id]
    save_config(cfg)


def zone_resolved_device_ids(zone):
    """A zone's *effective* bulb membership: its own `device_ids` plus the
    device_ids of every group it references via `group_ids`, de-duplicated
    and order-preserving. Reads groups fresh from config so a zone always
    reflects the current membership of any group it points to."""
    cfg = load_config()
    groups_by_id = {g["id"]: g for g in cfg.get("groups", [])}
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


# ------------------------------------------------------- orchestration presets
# Bundles a group audio-reactive role_mode plus its per-bulb overrides
# (hue_offsets / brightness_scales / band_assignments) under a friendly
# name, so a favorite multi-bulb arrangement can be re-applied with one
# call instead of re-specifying every override each time.

def list_orchestration_presets():
    cfg = load_config()
    return cfg.get("orchestration_presets", [])


def get_orchestration_preset(preset_id):
    cfg = load_config()
    for p in cfg.get("orchestration_presets", []):
        if p["id"] == preset_id:
            return p
    return None


def upsert_orchestration_preset(preset):
    cfg = load_config()
    presets = cfg.setdefault("orchestration_presets", [])
    for i, p in enumerate(presets):
        if p["id"] == preset["id"]:
            presets[i] = preset
            save_config(cfg)
            return
    presets.append(preset)
    save_config(cfg)


def delete_orchestration_preset(preset_id):
    cfg = load_config()
    cfg["orchestration_presets"] = [
        p for p in cfg.get("orchestration_presets", []) if p["id"] != preset_id
    ]
    save_config(cfg)


# ------------------------------------------------------ audio input calibration
# Remembers a sensitivity multiplier per *audio input device index* (not
# per bulb) -- e.g. "this USB mic always needs 1.4x gain" -- so re-selecting
# that device auto-applies the calibration instead of the caller having to
# remember and re-supply it every time.

def list_audio_input_calibrations():
    cfg = load_config()
    return cfg.get("audio_input_calibrations", [])


def get_audio_input_calibration(device_index):
    cfg = load_config()
    for c in cfg.get("audio_input_calibrations", []):
        if c["device_index"] == device_index:
            return c["sensitivity"]
    return None


def set_audio_input_calibration(device_index, sensitivity, name=None):
    cfg = load_config()
    cals = cfg.setdefault("audio_input_calibrations", [])
    for c in cals:
        if c["device_index"] == device_index:
            c["sensitivity"] = sensitivity
            if name is not None:
                c["name"] = name
            save_config(cfg)
            return c
    entry = {"device_index": device_index, "sensitivity": sensitivity}
    if name is not None:
        entry["name"] = name
    cals.append(entry)
    save_config(cfg)
    return entry


def delete_audio_input_calibration(device_index):
    cfg = load_config()
    cfg["audio_input_calibrations"] = [
        c for c in cfg.get("audio_input_calibrations", []) if c["device_index"] != device_index
    ]
    save_config(cfg)
