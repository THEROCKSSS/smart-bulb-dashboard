import json
import os
import threading

CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
EXAMPLE_PATH = os.path.join(CONFIG_DIR, "config.example.json")

_lock = threading.Lock()


def _default_config():
    return {"devices": [], "groups": []}


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
