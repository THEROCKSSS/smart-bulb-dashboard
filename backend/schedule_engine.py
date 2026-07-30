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
    elif action == "audio_reactive_preset":
        _start_audio_reactive_preset(controller, params)


def _start_audio_reactive_preset(controller, params):
    """Week 1 Phase D, section 8: a new schedule-engine action type that
    starts an audio-reactive session using a named session preset (saved
    via audio_presets.save_preset) rather than reinventing a parallel
    scheduling mechanism -- this reuses `add_rule`/`_tick`/`start_scheduler`
    exactly as any other rule does; the schedule engine doesn't know or
    care that "audio_reactive_preset" ends up starting a capture stream
    instead of flipping a relay.

    `params` must carry `preset_id` (the audio_presets.py session preset to
    apply); `device_index` may optionally override the preset's own stored
    capture device.
    """
    # Local imports: schedule_engine must not import audio_reactive/
    # audio_presets at module load time, since plain schedule-engine unit
    # tests (rule CRUD, _tick timing) shouldn't need sounddevice/numpy
    # available just to import this module.
    import audio_presets
    import audio_reactive

    preset_id = params.get("preset_id")
    preset = audio_presets.get_preset(preset_id)
    if not preset:
        raise ValueError(f"unknown audio session preset '{preset_id}'")
    cfg = preset["config"]
    device_index = params.get("device_index", cfg.get("device_index"))
    audio_reactive.start_session(
        controller,
        device_index,
        cfg.get("mode", "band_fixed"),
        cfg.get("sensitivity", 1.0),
        cfg.get("monochrome_hue", 280.0),
        cfg.get("n_bands", 3),
        cfg.get("min_dwell_ms", audio_reactive.DEFAULT_MIN_DWELL_MS),
        max_duration_s=cfg.get("max_duration_s"),
        warmup_s=cfg.get("warmup_s", 0.0),
        auto_resume_grace_s=cfg.get("auto_resume_grace_s", audio_reactive.DEFAULT_AUTO_RESUME_GRACE_S),
        max_flash_rate_hz=cfg.get("max_flash_rate_hz"),
        disable_flash_heavy=cfg.get("disable_flash_heavy", False),
    )


def _tick(get_controller_fn, now=None):
    """`now` is injectable so tests can fast-forward a rule's fire time
    without sleeping for real minutes (pass a crafted `datetime` instead of
    relying on the wall clock)."""
    now = now or datetime.now()
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
