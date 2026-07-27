import json
import os
import threading
import time
import uuid
from datetime import datetime

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)
SCHEDULE_PATH = os.path.join(DATA_DIR, "schedules.json")

_lock = threading.Lock()
_fired_today_cache = {}


def _load():
    if not os.path.exists(SCHEDULE_PATH):
        return []
    with open(SCHEDULE_PATH, "r") as f:
        return json.load(f)


def _save(rules):
    with open(SCHEDULE_PATH, "w") as f:
        json.dump(rules, f, indent=2)


def list_rules(device_id=None):
    rules = _load()
    if device_id:
        return [r for r in rules if r["device_id"] == device_id]
    return rules


def add_rule(device_id, time_hhmm, days, action, params):
    with _lock:
        rules = _load()
        rule = {
            "id": str(uuid.uuid4())[:8],
            "device_id": device_id,
            "time": time_hhmm,
            "days": days,  # list of 0-6 (Mon=0) or ["daily"]
            "action": action,  # "power_on" | "power_off" | "scene" | "preset"
            "params": params or {},
            "enabled": True,
        }
        rules.append(rule)
        _save(rules)
        return rule


def delete_rule(rule_id):
    with _lock:
        rules = _load()
        rules = [r for r in rules if r["id"] != rule_id]
        _save(rules)


def set_enabled(rule_id, enabled):
    with _lock:
        rules = _load()
        for r in rules:
            if r["id"] == rule_id:
                r["enabled"] = enabled
        _save(rules)


def _apply_action(controller, rule):
    action = rule["action"]
    params = rule["params"]
    if action == "power_on":
        controller.power(True)
    elif action == "power_off":
        controller.power(False)
    elif action == "scene":
        controller.apply_scene(params.get("scene_id"))
    elif action == "preset":
        controller.apply_preset(params.get("preset_id"))


def _tick(get_controller_fn):
    now = datetime.now()
    key_minute = now.strftime("%Y-%m-%d %H:%M")
    rules = _load()
    for rule in rules:
        if not rule.get("enabled", True):
            continue
        if rule["time"] != now.strftime("%H:%M"):
            continue
        days = rule.get("days", ["daily"])
        weekday = now.weekday()
        if "daily" not in days and weekday not in days:
            continue
        cache_key = f"{rule['id']}|{key_minute}"
        if _fired_today_cache.get(rule["id"]) == key_minute:
            continue
        _fired_today_cache[rule["id"]] = key_minute
        controller = get_controller_fn(rule["device_id"])
        if controller:
            try:
                _apply_action(controller, rule)
                controller._log("schedule_fired", {"rule_id": rule["id"], "action": rule["action"]})
            except Exception as e:
                controller._log("schedule_error", {"rule_id": rule["id"]}, ok=False, error=str(e))


def start_scheduler(get_controller_fn):
    def loop():
        while True:
            try:
                _tick(get_controller_fn)
            except Exception:
                pass
            time.sleep(20)

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t
