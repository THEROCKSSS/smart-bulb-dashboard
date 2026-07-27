import os
import time

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

import config as cfgmod
import bulb_manager as bm
import schedule_engine
from scenes_presets import PRESET_COLORS, SCENES, EFFECTS

APP_VERSION = "0.1.0-prototype"
START_TIME = time.time()

app = FastAPI(title="Smart Bulb Dashboard", version=APP_VERSION)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_controller_or_404(device_id):
    c = bm.get_controller(device_id)
    if not c:
        raise HTTPException(status_code=404, detail=f"device '{device_id}' not found")
    return c


# ---------------------------------------------------------------- models --
class PowerBody(BaseModel):
    on: bool


class BrightnessBody(BaseModel):
    value: int


class RGBBody(BaseModel):
    r: int
    g: int
    b: int


class HSVBody(BaseModel):
    h: float
    s: float = 100
    v: float = 100


class WhiteBody(BaseModel):
    brightness: int | None = None
    color_temp: int | None = None


class PresetApplyBody(BaseModel):
    preset_id: str


class SceneApplyBody(BaseModel):
    scene_id: str


class FavoriteSaveBody(BaseModel):
    name: str
    r: int
    g: int
    b: int


class EffectStartBody(BaseModel):
    effect: str
    speed: float = 1.0
    color_a: list[int] | None = None
    color_b: list[int] | None = None


class FlashAlertBody(BaseModel):
    r: int = 255
    g: int = 0
    b: int = 0
    times: int = 3


class SleepTimerBody(BaseModel):
    minutes: int


class WakeTimerBody(BaseModel):
    time: str  # "HH:MM"
    brightness: int = 100
    color_temp: int = 70
    fade_minutes: int = 10


class ScheduleRuleBody(BaseModel):
    time: str
    days: list = ["daily"]
    action: str
    params: dict = {}


class DeviceCreateBody(BaseModel):
    id: str
    name: str
    device_id: str
    local_key: str
    ip: str
    version: float = 3.3
    gamma: float = 1.0


class GroupActionBody(BaseModel):
    device_ids: list[str]


# --------------------------------------------------------------- system ---
@app.get("/api/system/health")
def health():
    return {"ok": True, "uptime_seconds": round(time.time() - START_TIME, 1)}


@app.get("/api/system/info")
def info():
    return {
        "version": APP_VERSION,
        "uptime_seconds": round(time.time() - START_TIME, 1),
        "presets_count": len(PRESET_COLORS),
        "scenes_count": len(SCENES),
        "effects_count": len(EFFECTS),
    }


# -------------------------------------------------------------- devices ---
@app.get("/api/devices")
def list_devices():
    cfg = cfgmod.load_config()
    return [cfgmod.redact(d) for d in cfg["devices"]]


@app.post("/api/devices")
def create_device(body: DeviceCreateBody):
    cfgmod.upsert_device(body.model_dump())
    bm.refresh_controller(body.id)
    return {"ok": True}


@app.patch("/api/devices/{device_id}")
def update_device(device_id: str, body: dict):
    existing = cfgmod.get_device(device_id)
    if not existing:
        raise HTTPException(404, "device not found")
    existing.update(body)
    cfgmod.upsert_device(existing)
    bm.refresh_controller(device_id)
    return cfgmod.redact(existing)


@app.delete("/api/devices/{device_id}")
def remove_device(device_id: str):
    cfgmod.delete_device(device_id)
    return {"ok": True}


@app.get("/api/devices/{device_id}/status")
def device_status(device_id: str):
    c = get_controller_or_404(device_id)
    return {"data_source": "LIVE DATA", **c.status()}


@app.post("/api/devices/{device_id}/power")
def device_power(device_id: str, body: PowerBody):
    c = get_controller_or_404(device_id)
    return {"result": c.power(body.on)}


@app.post("/api/devices/{device_id}/toggle")
def device_toggle(device_id: str):
    c = get_controller_or_404(device_id)
    return {"result": c.toggle()}


@app.post("/api/devices/{device_id}/brightness")
def device_brightness(device_id: str, body: BrightnessBody):
    c = get_controller_or_404(device_id)
    return {"result": c.set_brightness(body.value)}


@app.post("/api/devices/{device_id}/color")
def device_color(device_id: str, body: RGBBody):
    c = get_controller_or_404(device_id)
    return {"result": c.set_rgb(body.r, body.g, body.b)}


@app.post("/api/devices/{device_id}/color/hsv")
def device_color_hsv(device_id: str, body: HSVBody):
    c = get_controller_or_404(device_id)
    return {"result": c.set_hsv(body.h, body.s, body.v)}


@app.post("/api/devices/{device_id}/color/random")
def device_color_random(device_id: str):
    c = get_controller_or_404(device_id)
    return {"result": c.random_color()}


@app.post("/api/devices/{device_id}/white")
def device_white(device_id: str, body: WhiteBody):
    c = get_controller_or_404(device_id)
    return {"result": c.set_white(body.brightness, body.color_temp)}


@app.post("/api/devices/{device_id}/identify")
def device_identify(device_id: str):
    c = get_controller_or_404(device_id)
    return c.identify()


@app.post("/api/devices/{device_id}/flash-alert")
def device_flash_alert(device_id: str, body: FlashAlertBody):
    c = get_controller_or_404(device_id)
    return c.flash_alert(body.r, body.g, body.b, body.times)


@app.get("/api/devices/{device_id}/history")
def device_history(device_id: str):
    c = get_controller_or_404(device_id)
    return c.history()


@app.post("/api/devices/{device_id}/rescan")
def device_rescan(device_id: str):
    c = get_controller_or_404(device_id)
    try:
        import tinytuya
        devices = tinytuya.deviceScan(verbose=False, maxretry=1)
        cfg_entry = cfgmod.get_device(device_id)
        found = devices.get(cfg_entry["device_id"])
        if found and found.get("ip"):
            cfg_entry["ip"] = found["ip"]
            cfgmod.upsert_device(cfg_entry)
            bm.refresh_controller(device_id)
            return {"found": True, "ip": found["ip"]}
        return {"found": False}
    except Exception as e:
        return {"found": False, "error": str(e)}


@app.post("/api/devices/{device_id}/test-connection")
def device_test_connection(device_id: str):
    c = get_controller_or_404(device_id)
    return c.test_connection()


# --------------------------------------------------------- presets/scenes -
@app.get("/api/presets")
def get_presets():
    return PRESET_COLORS


@app.get("/api/scenes")
def get_scenes():
    return SCENES


@app.get("/api/effects")
def get_effects():
    return EFFECTS


@app.post("/api/devices/{device_id}/presets/apply")
def apply_preset(device_id: str, body: PresetApplyBody):
    c = get_controller_or_404(device_id)
    try:
        return c.apply_preset(body.preset_id)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/devices/{device_id}/scenes/apply")
def apply_scene(device_id: str, body: SceneApplyBody):
    c = get_controller_or_404(device_id)
    try:
        return c.apply_scene(body.scene_id)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/devices/{device_id}/favorites")
def list_favorites(device_id: str):
    c = get_controller_or_404(device_id)
    return c.favorites()


@app.post("/api/devices/{device_id}/favorites")
def save_favorite(device_id: str, body: FavoriteSaveBody):
    c = get_controller_or_404(device_id)
    return c.save_favorite(body.name, [body.r, body.g, body.b])


@app.delete("/api/devices/{device_id}/favorites/{favorite_id}")
def delete_favorite(device_id: str, favorite_id: str):
    c = get_controller_or_404(device_id)
    c.delete_favorite(favorite_id)
    return {"ok": True}


# -------------------------------------------------------------- effects ---
@app.post("/api/devices/{device_id}/effects/start")
def start_effect(device_id: str, body: EffectStartBody):
    c = get_controller_or_404(device_id)
    c.start_effect(body.effect, body.speed, body.color_a, body.color_b)
    return {"ok": True, "effect": body.effect}


@app.post("/api/devices/{device_id}/effects/stop")
def stop_effect(device_id: str):
    c = get_controller_or_404(device_id)
    c.stop_effect()
    return {"ok": True}


@app.get("/api/devices/{device_id}/effects/current")
def current_effect(device_id: str):
    c = get_controller_or_404(device_id)
    return {"effect": c.current_effect()}


# --------------------------------------------------------------- timers ---
@app.post("/api/devices/{device_id}/timers/sleep")
def sleep_timer_start(device_id: str, body: SleepTimerBody):
    c = get_controller_or_404(device_id)
    return c.start_sleep_timer(body.minutes)


@app.delete("/api/devices/{device_id}/timers/sleep")
def sleep_timer_cancel(device_id: str):
    c = get_controller_or_404(device_id)
    c.cancel_sleep_timer()
    return {"ok": True}


@app.get("/api/devices/{device_id}/timers/sleep")
def sleep_timer_status(device_id: str):
    c = get_controller_or_404(device_id)
    return c.sleep_timer_status()


@app.post("/api/devices/{device_id}/timers/wake")
def wake_timer_start(device_id: str, body: WakeTimerBody):
    c = get_controller_or_404(device_id)
    return c.start_wake_timer(body.time, body.brightness, body.color_temp, body.fade_minutes)


@app.delete("/api/devices/{device_id}/timers/wake")
def wake_timer_cancel(device_id: str):
    c = get_controller_or_404(device_id)
    c.cancel_wake_timer()
    return {"ok": True}


@app.get("/api/devices/{device_id}/timers/wake")
def wake_timer_status(device_id: str):
    c = get_controller_or_404(device_id)
    return c.wake_timer_status()


# ------------------------------------------------------------- schedule ---
@app.get("/api/devices/{device_id}/schedule")
def list_schedule(device_id: str):
    return schedule_engine.list_rules(device_id)


@app.post("/api/devices/{device_id}/schedule")
def add_schedule(device_id: str, body: ScheduleRuleBody):
    return schedule_engine.add_rule(device_id, body.time, body.days, body.action, body.params)


@app.delete("/api/schedule/{rule_id}")
def delete_schedule(rule_id: str):
    schedule_engine.delete_rule(rule_id)
    return {"ok": True}


@app.patch("/api/schedule/{rule_id}")
def toggle_schedule(rule_id: str, body: dict):
    schedule_engine.set_enabled(rule_id, body.get("enabled", True))
    return {"ok": True}


# ---------------------------------------------------------------- groups --
@app.get("/api/groups")
def list_groups():
    cfg = cfgmod.load_config()
    return cfg.get("groups", [])


@app.post("/api/groups/{group_id}/power")
def group_power(group_id: str, body: PowerBody):
    cfg = cfgmod.load_config()
    group = next((g for g in cfg.get("groups", []) if g["id"] == group_id), None)
    if not group:
        raise HTTPException(404, "group not found")
    results = {}
    for dev_id in group["device_ids"]:
        c = bm.get_controller(dev_id)
        if c:
            results[dev_id] = c.power(body.on)
    return results


@app.post("/api/groups/{group_id}/color")
def group_color(group_id: str, body: RGBBody):
    cfg = cfgmod.load_config()
    group = next((g for g in cfg.get("groups", []) if g["id"] == group_id), None)
    if not group:
        raise HTTPException(404, "group not found")
    results = {}
    for dev_id in group["device_ids"]:
        c = bm.get_controller(dev_id)
        if c:
            results[dev_id] = c.set_rgb(body.r, body.g, body.b)
    return results


@app.on_event("startup")
def on_startup():
    schedule_engine.start_scheduler(bm.get_controller)


# --------------------------------------------------------- static files ---
FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend")
if os.path.isdir(FRONTEND_DIR):
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

    @app.get("/")
    def index():
        return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))
