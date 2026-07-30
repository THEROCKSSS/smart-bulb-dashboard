import json
import os
import socket
import threading
import time
import uuid
from collections import deque
from datetime import datetime

import tinytuya

import config as cfgmod
from scenes_presets import PRESET_COLORS, SCENES, find_preset, find_scene

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)
FAVORITES_PATH = os.path.join(DATA_DIR, "favorites.json")
HISTORY_LIMIT = 200


def _load_favorites():
    if not os.path.exists(FAVORITES_PATH):
        return {}
    with open(FAVORITES_PATH, "r") as f:
        return json.load(f)


def _save_favorites(data):
    with open(FAVORITES_PATH, "w") as f:
        json.dump(data, f, indent=2)


def hsv_to_rgb_hue_deg(h_deg, s=1.0, v=1.0):
    import colorsys
    r, g, b = colorsys.hsv_to_rgb((h_deg % 360) / 360.0, s, v)
    return int(r * 255), int(g * 255), int(b * 255)


class BulbController:
    """Wraps one tinytuya BulbDevice with effects, timers, history, diagnostics."""

    def __init__(self, device_cfg):
        self.cfg = device_cfg
        self._device_lock = threading.RLock()
        self._effect_thread = None
        self._effect_stop = threading.Event()
        self._current_effect = None
        self._sleep_timer_thread = None
        self._sleep_timer_stop = threading.Event()
        self._sleep_timer_ends_at = None
        self._wake_timer_thread = None
        self._wake_timer_stop = threading.Event()
        self._wake_timer_info = None
        self._history = deque(maxlen=HISTORY_LIMIT)
        self._last_error = None
        self._dev = None
        self._last_dps = {}

    # -- connection -----------------------------------------------------
    def _get_device(self):
        if self._dev is None:
            self._dev = tinytuya.BulbDevice(
                self.cfg["device_id"],
                self.cfg["ip"],
                self.cfg["local_key"],
                version=self.cfg.get("version", 3.3),
            )
            self._dev.set_socketPersistent(True)
        return self._dev

    def reset_connection(self):
        with self._device_lock:
            self._dev = None

    # -- history ----------------------------------------------------------
    def _log(self, action, params=None, ok=True, error=None):
        self._history.appendleft({
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "action": action,
            "params": params or {},
            "ok": ok,
            "error": error,
        })

    def history(self):
        return list(self._history)

    # -- core status --------------------------------------------------
    def status(self):
        with self._device_lock:
            try:
                dev = self._get_device()
                raw = dev.status()
                if isinstance(raw, dict) and raw.get("Error"):
                    self._last_error = raw.get("Error")
                    self._log("status", ok=False, error=raw.get("Error"))
                    return {"online": False, "error": raw.get("Error"), "raw": raw}
                dps = raw.get("dps", {})
                self._last_dps.update(dps)
                parsed = self._parse_dps(self._last_dps)
                self._last_error = None
                return {"online": True, "raw": raw, **parsed}
            except Exception as e:
                self._last_error = str(e)
                self._log("status", ok=False, error=str(e))
                return {"online": False, "error": str(e)}

    def _parse_dps(self, dps):
        power = dps.get("20")
        mode = dps.get("21")
        brightness_raw = dps.get("22")
        color_temp_raw = dps.get("23")
        colour_data = dps.get("24")
        h = s = v = None
        if colour_data and len(colour_data) >= 12:
            try:
                h = int(colour_data[0:4], 16)
                s = int(colour_data[4:8], 16)
                v = int(colour_data[8:12], 16)
            except ValueError:
                pass
        return {
            "power": bool(power) if power is not None else None,
            "mode": mode,
            "brightness_pct": round((brightness_raw / 1000) * 100) if brightness_raw is not None else None,
            "color_temp_pct": round((color_temp_raw / 1000) * 100) if color_temp_raw is not None else None,
            "hue": h,
            "saturation_pct": round((s / 1000) * 100) if s is not None else None,
            "value_pct": round((v / 1000) * 100) if v is not None else None,
        }

    # -- basic control --------------------------------------------------
    def power(self, on):
        with self._device_lock:
            dev = self._get_device()
            result = dev.turn_on() if on else dev.turn_off()
            self._log("power", {"on": on})
            return result

    def toggle(self):
        st = self.status()
        return self.power(not st.get("power", False))

    def _max_brightness_cap(self):
        """Per-bulb safety cap (e.g. a bulb near a bed you never want at
        full 100%). 100 (no cap) when the device config doesn't set one."""
        return max(1, min(100, self.cfg.get("max_brightness_pct", 100)))

    def set_brightness(self, pct):
        """Mode-aware brightness: dp22 (bright_value) only applies in white
        mode. In colour mode the brightness lives in the V component of the
        HSV colour_data (dp24), so writing dp22 there silently flips the
        bulb into white mode instead of just dimming the current color."""
        pct = max(1, min(100, pct, self._max_brightness_cap()))
        gamma = self.cfg.get("gamma", 1.0)
        adjusted = pct ** gamma / (100 ** (gamma - 1)) if gamma != 1.0 else pct

        with self._device_lock:
            dev = self._get_device()
            current = self.status()
            if current.get("mode") == "colour":
                h = current.get("hue") or 0
                s = current.get("saturation_pct")
                s = 100 if s is None else s
                result = self.set_hsv(h, s, adjusted)
            else:
                raw = int(max(1, min(1000, (adjusted / 100) * 1000)))
                result = dev.set_value(22, raw)
            self._log("brightness", {"pct": pct})
            return result

    def set_rgb(self, r, g, b):
        with self._device_lock:
            dev = self._get_device()
            result = dev.set_colour(r, g, b)
            self._log("color_rgb", {"r": r, "g": g, "b": b})
            return result

    def set_hsv(self, h, s_pct, v_pct):
        """Applies two per-bulb config overrides before ever touching the
        network:
          - `hue_calibration_offset`: some bulbs render hue slightly warm/
            cool of the requested value, so a correction in degrees can be
            baked in per-bulb (matters most for group audio-reactive modes
            like `unison`, where every bulb is asked for the same hue and
            should actually *look* the same).
          - `max_brightness_pct`: this call bypasses set_brightness (the
            usual choke point for that cap) since HSV brightness lives in
            the V component, not dp22 -- so the cap is re-applied here too.
        """
        h = (h + self.cfg.get("hue_calibration_offset", 0.0)) % 360
        v_pct = min(v_pct, self._max_brightness_cap())
        r, g, b = hsv_to_rgb_hue_deg(h, s_pct / 100.0, v_pct / 100.0)
        return self.set_rgb(r, g, b)

    def set_white(self, brightness_pct=None, color_temp_pct=None):
        with self._device_lock:
            dev = self._get_device()
            dev.set_white()
            if color_temp_pct is not None:
                raw_temp = int(max(0, min(1000, (color_temp_pct / 100) * 1000)))
                dev.set_colourtemp(raw_temp)
            if brightness_pct is not None:
                self.set_brightness(brightness_pct)
            self._log("white", {"brightness": brightness_pct, "color_temp": color_temp_pct})
            return {"ok": True}

    def random_color(self):
        import random
        r, g, b = random.randint(0, 255), random.randint(0, 255), random.randint(0, 255)
        self.set_rgb(r, g, b)
        return {"r": r, "g": g, "b": b}

    def identify(self):
        """Blink 3 times so the user can spot which physical bulb this is."""
        self.stop_effect()
        st = self.status()
        was_on = st.get("power", True)
        for _ in range(3):
            self.power(False)
            time.sleep(0.4)
            self.power(True)
            time.sleep(0.4)
        if not was_on:
            self.power(False)
        self._log("identify")
        return {"ok": True}

    def flash_alert(self, r=255, g=0, b=0, times=3):
        self.stop_effect()
        prev = self.status()
        for _ in range(times):
            self.set_rgb(r, g, b)
            time.sleep(0.3)
            self.set_brightness(10)
            time.sleep(0.3)
            self.set_brightness(100)
        self._log("flash_alert", {"r": r, "g": g, "b": b, "times": times})
        return {"ok": True}

    # -- presets & scenes -------------------------------------------------
    def apply_preset(self, preset_id):
        preset = find_preset(preset_id)
        if not preset:
            raise ValueError("unknown preset")
        self.stop_effect()
        r, g, b = preset["rgb"]
        self.set_rgb(r, g, b)
        self._log("preset", {"preset_id": preset_id})
        return preset

    def apply_scene(self, scene_id):
        scene = find_scene(scene_id)
        if not scene:
            raise ValueError("unknown scene")
        self.stop_effect()
        self.power(True)
        if scene["mode"] == "white":
            self.set_white(brightness_pct=scene["brightness"], color_temp_pct=scene["color_temp"])
        else:
            r, g, b = scene["rgb"]
            self.set_rgb(r, g, b)
            self.set_brightness(scene["brightness"])
        self._log("scene", {"scene_id": scene_id})
        return scene

    def favorites(self):
        return _load_favorites().get(self.cfg["id"], [])

    def save_favorite(self, name, rgb):
        favs = _load_favorites()
        device_favs = favs.setdefault(self.cfg["id"], [])
        entry = {"id": str(uuid.uuid4())[:8], "name": name, "rgb": rgb}
        device_favs.append(entry)
        _save_favorites(favs)
        self._log("save_favorite", {"name": name, "rgb": rgb})
        return entry

    def delete_favorite(self, favorite_id):
        favs = _load_favorites()
        device_favs = favs.get(self.cfg["id"], [])
        favs[self.cfg["id"]] = [f for f in device_favs if f["id"] != favorite_id]
        _save_favorites(favs)
        self._log("delete_favorite", {"id": favorite_id})

    # -- effects (background thread) -------------------------------------
    def stop_effect(self):
        if self._effect_thread and self._effect_thread.is_alive():
            self._effect_stop.set()
            self._effect_thread.join(timeout=2)
        self._current_effect = None

    def current_effect(self):
        return self._current_effect

    def start_effect(self, effect_id, speed=1.0, color_a=None, color_b=None):
        self.stop_effect()
        self._effect_stop = threading.Event()
        self._current_effect = effect_id
        self._log("effect_start", {"effect": effect_id, "speed": speed})

        def runner():
            try:
                if effect_id == "rainbow":
                    self._run_rainbow(speed)
                elif effect_id == "pulse":
                    self._run_pulse(speed)
                elif effect_id == "strobe":
                    self._run_strobe(speed)
                elif effect_id == "candle":
                    self._run_candle(speed)
                elif effect_id == "fade":
                    self._run_fade(speed, color_a or [255, 0, 0], color_b or [0, 0, 255])
                elif effect_id == "color_loop":
                    self._run_color_loop(speed)
                elif effect_id == "random":
                    self._run_random(speed)
            except Exception as e:
                self._log("effect_error", {"effect": effect_id}, ok=False, error=str(e))

        self._effect_thread = threading.Thread(target=runner, daemon=True)
        self._effect_thread.start()

    def _run_rainbow(self, speed):
        hue = 0
        step = max(1, int(5 * speed))
        delay = max(0.05, 0.3 / speed)
        while not self._effect_stop.is_set():
            self.set_hsv(hue, 100, 100)
            hue = (hue + step) % 360
            self._effect_stop.wait(delay)

    def _run_pulse(self, speed):
        st = self.status()
        h = st.get("hue") or 0
        delay = max(0.02, 0.05 / speed)
        while not self._effect_stop.is_set():
            for b in list(range(10, 101, 5)) + list(range(100, 9, -5)):
                if self._effect_stop.is_set():
                    return
                self.set_hsv(h, 100, b)
                self._effect_stop.wait(delay)

    def _run_strobe(self, speed):
        delay = max(0.05, 0.5 / speed)
        while not self._effect_stop.is_set():
            self.power(True)
            self._effect_stop.wait(delay)
            self.power(False)
            self._effect_stop.wait(delay)
        self.power(True)

    def _run_candle(self, speed):
        import random
        delay = max(0.05, 0.15 / speed)
        while not self._effect_stop.is_set():
            flicker = random.randint(50, 100)
            self.set_rgb(255, int(140 * flicker / 100), int(40 * flicker / 100))
            self._effect_stop.wait(delay)

    def _run_fade(self, speed, color_a, color_b):
        steps = 30
        delay = max(0.02, 2.0 / speed / steps)
        while not self._effect_stop.is_set():
            for i in range(steps + 1):
                if self._effect_stop.is_set():
                    return
                t = i / steps
                r = int(color_a[0] + (color_b[0] - color_a[0]) * t)
                g = int(color_a[1] + (color_b[1] - color_a[1]) * t)
                b = int(color_a[2] + (color_b[2] - color_a[2]) * t)
                self.set_rgb(r, g, b)
                self._effect_stop.wait(delay)
            color_a, color_b = color_b, color_a

    def _run_color_loop(self, speed):
        delay = max(0.3, 3.0 / speed)
        i = 0
        while not self._effect_stop.is_set():
            preset = PRESET_COLORS[i % len(PRESET_COLORS)]
            r, g, b = preset["rgb"]
            self.set_rgb(r, g, b)
            i += 1
            self._effect_stop.wait(delay)

    def _run_random(self, speed):
        delay = max(0.3, 3.0 / speed)
        while not self._effect_stop.is_set():
            self.random_color()
            self._effect_stop.wait(delay)

    # -- timers -----------------------------------------------------------
    def start_sleep_timer(self, minutes):
        self.cancel_sleep_timer()
        self._sleep_timer_stop = threading.Event()
        ends_at = time.time() + minutes * 60
        self._sleep_timer_ends_at = ends_at

        def runner():
            fade_start = ends_at - min(60, minutes * 60 * 0.2)
            while not self._sleep_timer_stop.is_set():
                now = time.time()
                if now >= ends_at:
                    self.power(False)
                    self._log("sleep_timer_fired", {"minutes": minutes})
                    return
                if now >= fade_start:
                    remaining = ends_at - now
                    total_fade = ends_at - fade_start
                    pct = max(1, int(100 * (remaining / total_fade))) if total_fade > 0 else 1
                    try:
                        self.set_brightness(pct)
                    except Exception:
                        pass
                self._sleep_timer_stop.wait(2)

        self._sleep_timer_thread = threading.Thread(target=runner, daemon=True)
        self._sleep_timer_thread.start()
        self._log("sleep_timer_start", {"minutes": minutes})
        return {"ends_at": ends_at}

    def cancel_sleep_timer(self):
        if self._sleep_timer_thread and self._sleep_timer_thread.is_alive():
            self._sleep_timer_stop.set()
            self._sleep_timer_thread.join(timeout=2)
        self._sleep_timer_ends_at = None

    def sleep_timer_status(self):
        if self._sleep_timer_ends_at:
            return {"active": True, "seconds_remaining": max(0, int(self._sleep_timer_ends_at - time.time()))}
        return {"active": False}

    def start_wake_timer(self, target_hhmm, brightness=100, color_temp=70, fade_minutes=10):
        self.cancel_wake_timer()
        self._wake_timer_stop = threading.Event()
        self._wake_timer_info = {"target": target_hhmm, "brightness": brightness, "color_temp": color_temp, "fade_minutes": fade_minutes}

        def seconds_until(hhmm):
            now = datetime.now()
            hh, mm = map(int, hhmm.split(":"))
            target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if target <= now:
                target = target.replace(day=target.day + 1)
            return (target - now).total_seconds()

        def runner():
            wait_s = seconds_until(target_hhmm)
            fade_start_wait = max(0, wait_s - fade_minutes * 60)
            if not self._wake_timer_stop.wait(fade_start_wait):
                self.power(True)
                self.set_white(brightness_pct=1, color_temp_pct=color_temp)
                steps = 20
                for i in range(1, steps + 1):
                    if self._wake_timer_stop.is_set():
                        return
                    pct = int((i / steps) * brightness)
                    self.set_brightness(max(1, pct))
                    self._wake_timer_stop.wait(max(1, (fade_minutes * 60) / steps))
                self._log("wake_timer_fired", {"target": target_hhmm})

        self._wake_timer_thread = threading.Thread(target=runner, daemon=True)
        self._wake_timer_thread.start()
        self._log("wake_timer_start", {"target": target_hhmm})
        return self._wake_timer_info

    def cancel_wake_timer(self):
        if self._wake_timer_thread and self._wake_timer_thread.is_alive():
            self._wake_timer_stop.set()
            self._wake_timer_thread.join(timeout=2)
        self._wake_timer_info = None

    def wake_timer_status(self):
        if self._wake_timer_info:
            return {"active": True, **self._wake_timer_info}
        return {"active": False}

    # -- diagnostics --------------------------------------------------
    def test_connection(self):
        result = {"ip": self.cfg["ip"], "device_id": self.cfg["device_id"]}
        start = time.time()
        try:
            sock = socket.create_connection((self.cfg["ip"], 6668), timeout=3)
            sock.close()
            result["tcp_6668_reachable"] = True
        except Exception as e:
            result["tcp_6668_reachable"] = False
            result["tcp_error"] = str(e)
        result["tcp_latency_ms"] = round((time.time() - start) * 1000, 1)

        start = time.time()
        st = self.status()
        result["status_latency_ms"] = round((time.time() - start) * 1000, 1)
        result["status_ok"] = st.get("online", False)
        if not result["status_ok"]:
            result["status_error"] = st.get("error")
        return result

    def last_error(self):
        return self._last_error


_controllers = {}
_controllers_lock = threading.Lock()


def get_controller(device_id):
    with _controllers_lock:
        if device_id not in _controllers:
            device_cfg = cfgmod.get_device(device_id)
            if not device_cfg:
                return None
            _controllers[device_id] = BulbController(device_cfg)
        return _controllers[device_id]


def refresh_controller(device_id):
    with _controllers_lock:
        _controllers.pop(device_id, None)
    return get_controller(device_id)


def all_controllers():
    cfg = cfgmod.load_config()
    return [get_controller(d["id"]) for d in cfg["devices"]]
