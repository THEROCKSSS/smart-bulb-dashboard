import os
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

import config as cfgmod
import bulb_manager as bm
import schedule_engine
import discovery
import audio_reactive
import audio_signal
import audio_presets
import audio_safety
import audio_lightshow
import remote_auth
import reverse_proxy
import analytics
from scenes_presets import PRESET_COLORS, SCENES, EFFECTS

APP_VERSION = "0.3.0"
START_TIME = time.time()

app = FastAPI(title="Smart Bulb Dashboard", version=APP_VERSION)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def pin_gate(request: Request, call_next):
    """No-op unless remote_auth has been explicitly enabled (Settings ->
    Remote Access) -- the default LAN-only setup is never gated. When
    enabled (meant for DuckDNS/Tailscale exposure), every route except the
    login endpoint itself requires a valid signed session cookie."""
    if remote_auth.path_requires_auth(request.url.path):
        token = request.cookies.get(remote_auth.SESSION_COOKIE)
        if not remote_auth.verify_session_token(token):
            return JSONResponse({"detail": "authentication required"}, status_code=401)
    return await call_next(request)


# Registered AFTER pin_gate, which in Starlette means it wraps it: a
# plain-HTTP request gets redirected before the gate bothers checking a
# session, and the HSTS header lands on every response including the
# gate's own 401s. Both halves are no-ops until explicitly enabled --
# see backend/reverse_proxy.py for why neither defaults to on.
@app.middleware("http")
async def https_enforcement(request: Request, call_next):
    if reverse_proxy.should_redirect_to_https(request):
        # 307, not 301/308: permanent redirects get cached hard by
        # browsers, and "I turned this on to try it and now my LAN
        # dashboard won't load over HTTP anymore" is a support problem
        # nobody needs. 307 also preserves the method/body, so an API
        # client mid-POST isn't silently downgraded to a GET.
        return RedirectResponse(reverse_proxy.https_redirect_url(request), status_code=307)
    response = await call_next(request)
    if reverse_proxy.request_is_https(request):
        hsts = reverse_proxy.hsts_header_value()
        if hsts:
            response.headers["Strict-Transport-Security"] = hsts
    return response


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


class DiscoveryIntervalBody(BaseModel):
    hours: int


class AudioReactiveStartBody(BaseModel):
    device_index: int
    mode: str = "band_fixed"
    # None means "no explicit override" -- falls back to this input
    # device's saved calibration (see /api/audio/devices/{index}/calibration),
    # or 1.0 if none is saved. Passing an explicit value (including 1.0)
    # always wins over the saved calibration.
    sensitivity: float | None = None
    monochrome_hue: float = 280.0
    n_bands: int = 3
    min_dwell_ms: int = audio_reactive.DEFAULT_MIN_DWELL_MS
    beat_sensitivity: str = audio_reactive.DEFAULT_BEAT_SENSITIVITY
    # --- signal conditioning (Section 4) -- all optional, backward compatible ---
    agc_enabled: bool = False
    noise_gate_enabled: bool = True
    dc_removal_enabled: bool = True
    noise_gate_floor: float | None = None  # None = use saved calibration if any, else the module default
    agc_target_rms: float | None = None
    agc_attack_ms: float | None = None
    agc_release_ms: float | None = None
    band_gains: list[float] | None = None
    use_saved_calibration: bool = True
    # --- session management / reliability / safety (Sections 8/9/13) ---
    max_duration_s: float | None = None
    warmup_s: float = 0.0
    auto_resume_grace_s: float = audio_reactive.DEFAULT_AUTO_RESUME_GRACE_S
    max_flash_rate_hz: float | None = None
    disable_flash_heavy: bool = False
    max_brightness_swing: float | None = None
    silence_auto_off: bool = True
    fallback_device_index: int | None = None
    force: bool = False  # override an active-group conflict on this device


class AudioCalibrateBody(BaseModel):
    device_index: int
    duration_s: float = 3.0
    save: bool = True


class GroupAudioReactiveStartBody(BaseModel):
    device_index: int
    mode: str = "band_fixed"
    role_mode: str = "unison"
    sensitivity: float | None = None
    monochrome_hue: float = 280.0
    min_dwell_ms: int = audio_reactive.DEFAULT_MIN_DWELL_MS
    beat_sensitivity: str = audio_reactive.DEFAULT_BEAT_SENSITIVITY
    # Per-bulb orchestration overrides, indexed the same as the group's
    # device_ids. All optional -- omit for the existing default behavior.
    hue_offsets: list[float | None] | None = None
    brightness_scales: list[float | None] | None = None
    band_assignments: list[int | None] | None = None
    mirror_center_hue: float = 0.0
    wave_period_ticks: int = 40
    # If set, load role_mode + per-bulb overrides from a saved
    # orchestration preset first; any field explicitly set above still
    # wins over what the preset stored.
    orchestration_preset_id: str | None = None
    # --- session management / reliability / safety (Sections 8/9/13) ---
    max_duration_s: float | None = None
    warmup_s: float = 0.0
    max_flash_rate_hz: float | None = None
    disable_flash_heavy: bool = False
    silence_auto_off: bool = True
    fallback_device_index: int | None = None
    force: bool = False  # stop conflicting solo sessions on these devices first


class TapTempoBody(BaseModel):
    timestamp: float | None = None


class BeatSensitivityBody(BaseModel):
    preset: str


class ApplyAudioPresetBody(BaseModel):
    preset_id: str
    device_index: int


class GroupApplyAudioPresetBody(BaseModel):
    preset_id: str
    device_index: int
    role_mode: str = "unison"


# Genre/mood preset bundles (Section 3) -- a *named style* (mode + sensitivity
# + dwell + palette). Distinct from AudioSessionPresetSaveBody below, which
# snapshots one specific running session's exact config (Section 8). Both
# ship this round; reconciling them into a single preset system is a real
# follow-up, not done here to avoid conflating two genuinely different ideas
# under merge pressure.
class AudioCustomPresetBody(BaseModel):
    id: str | None = None
    name: str
    mode: str
    sensitivity: float = 1.0
    min_dwell_ms: int = audio_reactive.DEFAULT_MIN_DWELL_MS
    n_bands: int = 3
    monochrome_hue: float = 280.0
    beat_sensitivity: str = audio_reactive.DEFAULT_BEAT_SENSITIVITY
    palette: list[str] = []
    description: str = ""


class ZoneCreateBody(BaseModel):
    id: str
    name: str
    device_ids: list[str] = []
    group_ids: list[str] = []


class ZoneDeviceBody(BaseModel):
    device_id: str


class OrchestrationPresetBody(BaseModel):
    id: str
    name: str
    role_mode: str = "unison"
    hue_offsets: list[float | None] | None = None
    brightness_scales: list[float | None] | None = None
    band_assignments: list[int | None] | None = None
    mirror_center_hue: float = 0.0
    wave_period_ticks: int = 40


class AudioCalibrationBody(BaseModel):
    sensitivity: float
    name: str | None = None


class AudioSessionPresetSaveBody(BaseModel):
    name: str
    device_index: int
    mode: str = "band_fixed"
    sensitivity: float = 1.0
    monochrome_hue: float = 280.0
    n_bands: int = 3
    min_dwell_ms: int = audio_reactive.DEFAULT_MIN_DWELL_MS
    max_duration_s: float | None = None
    warmup_s: float = 0.0
    auto_resume_grace_s: float = audio_reactive.DEFAULT_AUTO_RESUME_GRACE_S
    max_flash_rate_hz: float | None = None
    disable_flash_heavy: bool = False


class AudioSessionPresetApplyBody(BaseModel):
    preset_id: str
    device_index: int | None = None  # override the preset's own stored capture device


class SafetyMaxFlashRateBody(BaseModel):
    max_flash_rate_hz: float


class SafetyDisableFlashHeavyBody(BaseModel):
    disabled: bool


class LightshowExportBody(BaseModel):
    name: str


class LightshowReplayBody(BaseModel):
    lightshow_id: str
    loop: bool = False


class PinLoginBody(BaseModel):
    pin: str


class RemoteAuthEnableBody(BaseModel):
    pin: str
    session_ttl_s: int | None = None


class RevokeSessionBody(BaseModel):
    session_id: str


class LoginRateLimitBody(BaseModel):
    max_attempts: int
    window_s: int


# --------------------------------------------------------------- system ---
@app.get("/healthz")
def healthz():
    """Liveness probe for infrastructure -- a reverse proxy's upstream
    health check, a Docker HEALTHCHECK, a systemd watchdog. Deliberately
    separate from /api/system/health so the two can diverge: this one is
    the endpoint most likely to end up reachable from the public internet
    (a proxy probes it before any auth runs), so it returns the absolute
    minimum -- no version, no uptime, nothing that helps someone fingerprint
    the install. /api/system/health stays the app's own richer status
    endpoint and is free to grow dependency checks that would be wrong to
    expose here."""
    return {"status": "ok"}


@app.get("/api/system/health")
def health():
    return {"ok": True, "uptime_seconds": round(time.time() - START_TIME, 1)}


@app.get("/api/system/proxy-status")
def proxy_status(request: Request):
    """What the backend currently believes about this request's real client
    IP and TLS state, and the trusted-proxy/HSTS/redirect settings behind
    that belief. The point is to make a reverse-proxy deployment verifiable
    from the outside: hit this through the proxy and confirm `client_ip` is
    your actual address, not the proxy's -- otherwise the per-IP lockout is
    silently keying every remote user into one shared bucket."""
    return reverse_proxy.proxy_status(request)


@app.get("/api/system/info")
def info():
    return {
        "version": APP_VERSION,
        "uptime_seconds": round(time.time() - START_TIME, 1),
        "presets_count": len(PRESET_COLORS),
        "scenes_count": len(SCENES),
        "effects_count": len(EFFECTS),
    }


# ----------------------------------------------------------- pin auth ---
@app.post("/api/auth/login")
def auth_login(body: PinLoginBody, request: Request):
    # Not request.client.host directly: behind a trusted reverse proxy that
    # is the proxy's address, and keying the lockout/rate limiter to it
    # would throttle every remote user as one. See reverse_proxy.py -- the
    # forwarded header is only believed from an explicitly trusted peer.
    ip = reverse_proxy.client_ip(request)

    allowed, retry_after = remote_auth.check_login_rate_limit(ip)
    if not allowed:
        remote_auth.log_audit_event("login_rate_limited", "blocked", ip=ip)
        raise HTTPException(429, "too many login attempts -- slow down",
                             headers={"Retry-After": str(int(retry_after) + 1)})

    ok, detail = remote_auth.verify_pin(body.pin, ip)
    if not ok:
        event = "login_lockout" if detail and "locked out" in detail else "login_failure"
        remote_auth.log_audit_event(event, "failure", ip=ip)
        raise HTTPException(401, detail)

    token = remote_auth.create_session_token(ip=ip)
    remote_auth.log_audit_event("login_success", "success", ip=ip)
    resp = JSONResponse({"ok": True})
    # `secure` is conditional, not always-on, and that's load-bearing: a
    # Secure cookie is silently dropped by the browser over plain HTTP, so
    # hardcoding it would lock every LAN user out of a dashboard that has
    # no HTTPS to fall back to. Conversely it must be set the moment the
    # browser really is on HTTPS, or the session cookie stays eligible to
    # leak over a downgraded/plaintext request.
    resp.set_cookie(remote_auth.SESSION_COOKIE, token, httponly=True, samesite="lax",
                     secure=reverse_proxy.request_is_https(request),
                     max_age=remote_auth.get_session_ttl())
    return resp


@app.post("/api/auth/logout")
def auth_logout(request: Request):
    token = request.cookies.get(remote_auth.SESSION_COOKIE)
    if token:
        # Real server-side invalidation (the allowlist entry is revoked),
        # not just deleting the cookie client-side -- a copy of the cookie
        # captured before logout is rejected on its next use too.
        revoked = remote_auth.revoke_current_session(token)
        if revoked:
            remote_auth.log_audit_event("logout", "success")
    resp = JSONResponse({"ok": True})
    # Attributes must mirror the ones set at login: a browser only replaces
    # a cookie when name/path/domain match, and a Secure-flagged deletion
    # sent over plain HTTP would itself be discarded, leaving the stale
    # cookie in place. (The session is revoked server-side above either
    # way, so this is about not leaving a dead cookie lying around.)
    resp.delete_cookie(remote_auth.SESSION_COOKIE, httponly=True, samesite="lax",
                        secure=reverse_proxy.request_is_https(request))
    return resp


@app.get("/api/auth/status")
def auth_status(request: Request):
    state = remote_auth.status()
    token = request.cookies.get(remote_auth.SESSION_COOKIE)
    authenticated = (not state["enabled"]) or remote_auth.verify_session_token(token)
    return {"enabled": state["enabled"], "authenticated": authenticated}


@app.post("/api/system/remote-auth/enable")
def remote_auth_enable(body: RemoteAuthEnableBody):
    try:
        remote_auth.enable(body.pin, body.session_ttl_s)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/system/remote-auth/disable")
def remote_auth_disable():
    remote_auth.disable()
    return {"ok": True}


@app.get("/api/system/remote-auth/status")
def remote_auth_status():
    return remote_auth.status()


@app.post("/api/system/remote-auth/rate-limit")
def remote_auth_set_rate_limit(body: LoginRateLimitBody):
    try:
        remote_auth.set_login_rate_limit(body.max_attempts, body.window_s)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return remote_auth.status()


# ------------------------------------------------------- session mgmt ---
@app.get("/api/auth/sessions")
def auth_sessions_list():
    return {"sessions": remote_auth.list_sessions()}


@app.post("/api/auth/sessions/revoke")
def auth_sessions_revoke(body: RevokeSessionBody):
    found = remote_auth.revoke_session(body.session_id)
    remote_auth.log_audit_event(
        "session_revoked", "success" if found else "not_found", session_id=body.session_id,
    )
    if not found:
        raise HTTPException(404, "session not found")
    return {"ok": True}


@app.post("/api/auth/sessions/revoke-all")
def auth_sessions_revoke_all():
    count = remote_auth.revoke_all_sessions()
    remote_auth.log_audit_event("session_revoke_all", "success", count=count)
    return {"ok": True, "revoked": count}


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
    result = c.power(body.on)
    audio_reactive.notify_manual_command(device_id)
    return {"result": result}


@app.post("/api/devices/{device_id}/toggle")
def device_toggle(device_id: str):
    c = get_controller_or_404(device_id)
    result = c.toggle()
    audio_reactive.notify_manual_command(device_id)
    return {"result": result}


@app.post("/api/devices/{device_id}/brightness")
def device_brightness(device_id: str, body: BrightnessBody):
    c = get_controller_or_404(device_id)
    result = c.set_brightness(body.value)
    audio_reactive.notify_manual_command(device_id)
    return {"result": result}


@app.post("/api/devices/{device_id}/color")
def device_color(device_id: str, body: RGBBody):
    c = get_controller_or_404(device_id)
    result = c.set_rgb(body.r, body.g, body.b)
    audio_reactive.notify_manual_command(device_id)
    return {"result": result}


@app.post("/api/devices/{device_id}/color/hsv")
def device_color_hsv(device_id: str, body: HSVBody):
    c = get_controller_or_404(device_id)
    result = c.set_hsv(body.h, body.s, body.v)
    audio_reactive.notify_manual_command(device_id)
    return {"result": result}


@app.post("/api/devices/{device_id}/color/random")
def device_color_random(device_id: str):
    c = get_controller_or_404(device_id)
    result = c.random_color()
    audio_reactive.notify_manual_command(device_id)
    return {"result": result}


@app.post("/api/devices/{device_id}/white")
def device_white(device_id: str, body: WhiteBody):
    c = get_controller_or_404(device_id)
    result = c.set_white(body.brightness, body.color_temp)
    audio_reactive.notify_manual_command(device_id)
    return {"result": result}


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
        result = c.apply_preset(body.preset_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    audio_reactive.notify_manual_command(device_id)
    return result


@app.post("/api/devices/{device_id}/scenes/apply")
def apply_scene(device_id: str, body: SceneApplyBody):
    c = get_controller_or_404(device_id)
    try:
        result = c.apply_scene(body.scene_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    audio_reactive.notify_manual_command(device_id)
    return result


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


# --------------------------------------------------------- audio-reactive -
def _device_key_for_index(device_index):
    """Resolves an input device_index to a stable calibration key (its
    name) -- device indices can shift on device reconnect/replug, but the
    name is what a saved noise-gate calibration should actually travel
    with. Falls back to the raw index (stringified) if the device can't be
    found (e.g. it was unplugged since /api/audio/devices was last called)."""
    try:
        devices = audio_reactive.list_input_devices()
    except Exception:
        return str(device_index)
    match = next((d for d in devices if d["index"] == device_index), None)
    return match["name"] if match else str(device_index)


@app.get("/api/audio/devices")
def audio_devices():
    try:
        return {
            "devices": audio_reactive.list_input_devices(),
            "modes": audio_reactive.MODES,
            "role_modes": audio_reactive.ROLE_MODES,
            "default_min_dwell_ms": audio_reactive.DEFAULT_MIN_DWELL_MS,
            "min_dwell_floor_ms": audio_reactive.MIN_DWELL_FLOOR_MS,
            "beat_sensitivity_presets": list(audio_reactive.BEAT_SENSITIVITY_PRESETS.keys()),
            "default_beat_sensitivity": audio_reactive.DEFAULT_BEAT_SENSITIVITY,
        }
    except Exception as e:
        raise HTTPException(500, f"could not list audio devices: {e}")


@app.post("/api/audio/calibrate")
def audio_calibrate(body: AudioCalibrateBody):
    """Samples `duration_s` seconds of room silence from `device_index` and
    computes a recommended noise-gate floor (Section 4's "calibrate from
    silence" flow). Saved per-device (keyed by device name) so future
    audio-reactive/start calls on the same input device pick it up
    automatically unless the caller overrides noise_gate_floor explicitly."""
    if body.duration_s <= 0 or body.duration_s > 15:
        raise HTTPException(400, "duration_s must be between 0 and 15 seconds")
    try:
        result = audio_signal.calibrate_from_device(body.device_index, body.duration_s)
    except Exception as e:
        raise HTTPException(500, f"calibration capture failed: {e}")
    device_key = _device_key_for_index(body.device_index)
    saved = None
    if body.save:
        saved = audio_signal.save_device_calibration(
            device_key, result["noise_gate_floor"], sample_rms=result["sample_rms"])
    return {"ok": True, "device_key": device_key, "result": result, "saved": saved}


@app.get("/api/audio/calibration")
def audio_calibration_list():
    return {"devices": audio_signal.list_calibrations()}


@app.delete("/api/audio/calibration/{device_key}")
def audio_calibration_delete(device_key: str):
    existed = audio_signal.delete_calibration(device_key)
    if not existed:
        raise HTTPException(404, "no calibration saved for that device")
    return {"ok": True}


@app.get("/api/audio/devices/{device_index}/health")
def audio_device_health(device_index: int):
    """Test a specific input device's basic capture without starting a
    full audio-reactive session -- lets the UI validate a device selection
    (or diagnose a "why isn't it reacting" report) up front."""
    return audio_reactive.device_health_check(device_index)


@app.get("/api/audio/calibrations")
def list_audio_calibrations():
    return cfgmod.list_audio_input_calibrations()


@app.put("/api/audio/devices/{device_index}/calibration")
def set_audio_calibration(device_index: int, body: AudioCalibrationBody):
    if not (0.1 <= body.sensitivity <= 5.0):
        raise HTTPException(400, "sensitivity must be between 0.1 and 5.0")
    return cfgmod.set_audio_input_calibration(device_index, body.sensitivity, body.name)


@app.delete("/api/audio/devices/{device_index}/calibration")
def delete_audio_calibration(device_index: int):
    cfgmod.delete_audio_input_calibration(device_index)
    return {"ok": True}


@app.get("/api/audio/modes/info")
def audio_modes_info():
    """Section 13: per-mode metadata (flash-heavy vs. ambient) so a
    frontend can label modes without hardcoding the classification."""
    settings = audio_safety.get_safety_settings()
    return {
        "modes": audio_safety.mode_metadata(audio_reactive.MODES),
        "hard_max_flash_rate_hz": audio_safety.HARD_MAX_FLASH_RATE_HZ,
        "flash_rate_standard": (
            "WCAG 2.3.1 'Three Flashes or Below Threshold' (max 3 flashes/second); "
            "see also ITU-R BT.1702 guidance for programme makers on photosensitive epilepsy."
        ),
        "settings": settings,
    }


@app.post("/api/audio/safety/max-flash-rate")
def audio_safety_set_max_flash_rate(body: SafetyMaxFlashRateBody):
    """Configurable max flash rate as an explicit safety ceiling, distinct
    from the dwell slider — always clamped to <= the hard, non-bypassable
    HARD_MAX_FLASH_RATE_HZ regardless of what's requested here."""
    return audio_safety.set_max_flash_rate(body.max_flash_rate_hz)


@app.post("/api/audio/safety/disable-flash-heavy")
def audio_safety_set_disable_flash_heavy(body: SafetyDisableFlashHeavyBody):
    """One-click 'disable all flash-heavy modes' toggle."""
    return audio_safety.set_disable_flash_heavy(body.disabled)


@app.get("/api/audio/safety/reduced-motion-profile")
def audio_safety_reduced_motion_profile():
    """A ready-to-use 'reduced motion' preset: gentle mode, no strobe-style
    modes, capped brightness swing/flash rate."""
    return audio_safety.reduced_motion_profile()


@app.post("/api/devices/{device_id}/audio-reactive/start")
def audio_reactive_start(device_id: str, body: AudioReactiveStartBody):
    c = get_controller_or_404(device_id)
    if body.mode not in audio_reactive.MODES:
        raise HTTPException(400, f"unknown mode '{body.mode}', expected one of {audio_reactive.MODES}")
    if body.min_dwell_ms < audio_reactive.MIN_DWELL_FLOOR_MS:
        raise HTTPException(400, f"min_dwell_ms below the safety floor of {audio_reactive.MIN_DWELL_FLOOR_MS}ms")
    if body.beat_sensitivity not in audio_reactive.BEAT_SENSITIVITY_PRESETS:
        raise HTTPException(400, f"unknown beat_sensitivity '{body.beat_sensitivity}', "
                                  f"expected one of {list(audio_reactive.BEAT_SENSITIVITY_PRESETS)}")
    ok, err = audio_reactive.validate_device_index(body.device_index)
    if not ok:
        raise HTTPException(400, err)
    if not audio_reactive.check_rate_limit(f"start:{device_id}"):
        raise HTTPException(429, "too many audio-reactive start/stop requests for this device — slow down")

    # Section 8: conflict check — this device already inside an active
    # *group* session would mean two independent senders fighting the bulb.
    conflicts = audio_reactive.check_solo_conflict(device_id)
    if conflicts and not body.force:
        raise HTTPException(
            409,
            f"device '{device_id}' is already in active group session(s) {conflicts} — "
            f"pass force=true to stop them and start a solo session instead",
        )
    for gid in conflicts:
        audio_reactive.stop_group_session(gid)

    device_key = _device_key_for_index(body.device_index)
    # Per-device-index sensitivity calibration (Section 11) is a separate,
    # coarser fallback from the per-device-key signal-conditioning
    # calibration (Section 4, use_saved_calibration/device_key above) -- an
    # explicit sensitivity always wins over both.
    sensitivity = body.sensitivity
    if sensitivity is None:
        sensitivity = cfgmod.get_audio_input_calibration(body.device_index)
        if sensitivity is None:
            sensitivity = 1.0

    try:
        session = audio_reactive.start_session(
            c, body.device_index, body.mode, sensitivity, body.monochrome_hue, body.n_bands, body.min_dwell_ms,
            beat_sensitivity=body.beat_sensitivity,
            device_key=device_key, agc_enabled=body.agc_enabled, noise_gate_enabled=body.noise_gate_enabled,
            dc_removal_enabled=body.dc_removal_enabled, noise_gate_floor=body.noise_gate_floor,
            agc_target_rms=body.agc_target_rms, agc_attack_ms=body.agc_attack_ms, agc_release_ms=body.agc_release_ms,
            band_gains=body.band_gains, use_saved_calibration=body.use_saved_calibration,
            max_duration_s=body.max_duration_s, warmup_s=body.warmup_s,
            auto_resume_grace_s=body.auto_resume_grace_s, max_flash_rate_hz=body.max_flash_rate_hz,
            disable_flash_heavy=body.disable_flash_heavy, max_brightness_swing=body.max_brightness_swing,
            silence_auto_off=body.silence_auto_off, fallback_device_index=body.fallback_device_index,
        )
    except audio_reactive.AudioConfigError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "device_key": device_key, **session.confirmation()}


@app.post("/api/devices/{device_id}/audio-reactive/stop")
def audio_reactive_stop(device_id: str):
    if not audio_reactive.check_rate_limit(f"stop:{device_id}"):
        raise HTTPException(429, "too many audio-reactive start/stop requests for this device — slow down")
    audio_reactive.stop_session(device_id)
    return {"ok": True}


@app.post("/api/devices/{device_id}/audio-reactive/tap-tempo")
def audio_reactive_tap_tempo(device_id: str, body: TapTempoBody = TapTempoBody()):
    tap_bpm = audio_reactive.tap_session_tempo(device_id, body.timestamp)
    if tap_bpm is None and not audio_reactive.get_session_status(device_id).get("active"):
        raise HTTPException(404, "no active audio-reactive session for this device")
    return {"tap_bpm": tap_bpm}


@app.post("/api/devices/{device_id}/audio-reactive/beat-sensitivity")
def audio_reactive_beat_sensitivity(device_id: str, body: BeatSensitivityBody):
    if body.preset not in audio_reactive.BEAT_SENSITIVITY_PRESETS:
        raise HTTPException(400, f"unknown beat_sensitivity preset '{body.preset}', "
                                  f"expected one of {list(audio_reactive.BEAT_SENSITIVITY_PRESETS)}")
    if not audio_reactive.set_session_beat_sensitivity(device_id, body.preset):
        raise HTTPException(404, "no active audio-reactive session for this device")
    return {"ok": True, "beat_sensitivity": body.preset}


@app.post("/api/devices/{device_id}/audio-reactive/apply-preset")
def audio_reactive_apply_preset(device_id: str, body: ApplyAudioPresetBody):
    c = get_controller_or_404(device_id)
    preset = audio_reactive.find_genre_preset(body.preset_id)
    if not preset:
        cfg = cfgmod.load_config()
        preset = next((p for p in cfg.get("audio_custom_presets", []) if p["id"] == body.preset_id), None)
    if not preset:
        raise HTTPException(404, f"preset '{body.preset_id}' not found")
    audio_reactive.start_session(c, body.device_index, preset["mode"], preset["sensitivity"],
                                  preset["monochrome_hue"], preset["n_bands"], preset["min_dwell_ms"],
                                  preset["beat_sensitivity"])
    return {"ok": True, "preset_id": preset["id"], "mode": preset["mode"]}


# ----------------------------------------------------------- audio presets -
# NOTE: named list_audio_genre_presets, not audio_presets -- that name is the
# imported `audio_presets` module (session-config presets, Section 8,
# used just below); a route handler of the same name would shadow the
# module import for the rest of this file.
@app.get("/api/audio/presets")
def list_audio_genre_presets():
    cfg = cfgmod.load_config()
    builtins = [{**p, "custom": False} for p in audio_reactive.AUDIO_GENRE_PRESETS]
    custom = cfg.get("audio_custom_presets", [])
    return {"presets": builtins + custom}


@app.post("/api/audio/presets/custom")
def audio_presets_save_custom(body: AudioCustomPresetBody):
    try:
        preset = audio_reactive.build_custom_preset(
            body.name, body.mode, body.sensitivity, body.min_dwell_ms, body.n_bands,
            body.monochrome_hue, body.beat_sensitivity, body.palette, body.description, body.id,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    cfg = cfgmod.load_config()
    custom = [p for p in cfg.get("audio_custom_presets", []) if p["id"] != preset["id"]]
    custom.append(preset)
    cfg["audio_custom_presets"] = custom
    cfgmod.save_config(cfg)
    return {"ok": True, "preset": preset}


@app.delete("/api/audio/presets/custom/{preset_id}")
def audio_presets_delete_custom(preset_id: str):
    cfg = cfgmod.load_config()
    custom = cfg.get("audio_custom_presets", [])
    remaining = [p for p in custom if p["id"] != preset_id]
    if len(remaining) == len(custom):
        raise HTTPException(404, "custom preset not found")
    cfg["audio_custom_presets"] = remaining
    cfgmod.save_config(cfg)
    return {"ok": True}


@app.get("/api/audio/presets/suggest")
def audio_presets_suggest(bpm: float):
    preset_id = audio_reactive.suggest_preset_for_bpm(bpm)
    preset = audio_reactive.find_genre_preset(preset_id) if preset_id else None
    return {"bpm": bpm, "suggested_preset_id": preset_id, "preset": preset}


@app.post("/api/devices/{device_id}/audio-reactive/resume-last")
def audio_reactive_resume_last(device_id: str):
    """Section 8: one-click 'resume last session' after a restart."""
    c = get_controller_or_404(device_id)
    session = audio_reactive.resume_last_session(c)
    if not session:
        raise HTTPException(404, f"no last-known-good audio-reactive session saved for '{device_id}'")
    return {"ok": True, **session.confirmation()}


# ----------------------------------------------- audio session presets ----
@app.post("/api/devices/{device_id}/audio-reactive/session-presets")
def save_audio_session_preset(device_id: str, body: AudioSessionPresetSaveBody):
    """Section 8: save an entire running session's config (mode,
    sensitivity, dwell, n_bands, device, ...) as a named, reusable preset."""
    get_controller_or_404(device_id)
    config = body.model_dump(exclude={"name"})
    return audio_presets.save_preset(body.name, device_id, config)


@app.get("/api/audio/session-presets")
def list_audio_session_presets(device_id: str | None = None):
    return audio_presets.list_presets(device_id)


@app.delete("/api/audio/session-presets/{preset_id}")
def delete_audio_session_preset(preset_id: str):
    found = audio_presets.delete_preset(preset_id)
    if not found:
        raise HTTPException(404, "session preset not found")
    return {"ok": True}


@app.post("/api/devices/{device_id}/audio-reactive/session-presets/apply")
def apply_audio_session_preset(device_id: str, body: AudioSessionPresetApplyBody):
    c = get_controller_or_404(device_id)
    preset = audio_presets.get_preset(body.preset_id)
    if not preset:
        raise HTTPException(404, "session preset not found")
    cfg = preset["config"]
    device_index = body.device_index if body.device_index is not None else cfg.get("device_index")
    try:
        session = audio_reactive.start_session(
            c, device_index, cfg.get("mode", "band_fixed"), cfg.get("sensitivity", 1.0),
            cfg.get("monochrome_hue", 280.0), cfg.get("n_bands", 3),
            cfg.get("min_dwell_ms", audio_reactive.DEFAULT_MIN_DWELL_MS),
            max_duration_s=cfg.get("max_duration_s"), warmup_s=cfg.get("warmup_s", 0.0),
            auto_resume_grace_s=cfg.get("auto_resume_grace_s", audio_reactive.DEFAULT_AUTO_RESUME_GRACE_S),
            max_flash_rate_hz=cfg.get("max_flash_rate_hz"),
            disable_flash_heavy=cfg.get("disable_flash_heavy", False),
        )
    except audio_reactive.AudioConfigError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "preset_id": body.preset_id, **session.confirmation()}


# --------------------------------------------------- audio-reactive groups -
@app.post("/api/groups/{group_id}/audio-reactive/start")
def group_audio_reactive_start(group_id: str, body: GroupAudioReactiveStartBody):
    cfg = cfgmod.load_config()
    group = next((g for g in cfg.get("groups", []) if g["id"] == group_id), None)
    if not group:
        raise HTTPException(404, "group not found")

    role_mode = body.role_mode
    hue_offsets = body.hue_offsets
    brightness_scales = body.brightness_scales
    band_assignments = body.band_assignments
    mirror_center_hue = body.mirror_center_hue
    wave_period_ticks = body.wave_period_ticks
    if body.orchestration_preset_id:
        preset = cfgmod.get_orchestration_preset(body.orchestration_preset_id)
        if not preset:
            raise HTTPException(404, f"orchestration preset '{body.orchestration_preset_id}' not found")
        # Explicit body fields always win over the preset's stored values --
        # only fall back to the preset when the caller left a field at its
        # own default/unset value.
        role_mode = body.role_mode if body.role_mode != "unison" else preset.get("role_mode", role_mode)
        hue_offsets = hue_offsets if hue_offsets is not None else preset.get("hue_offsets")
        brightness_scales = brightness_scales if brightness_scales is not None else preset.get("brightness_scales")
        band_assignments = band_assignments if band_assignments is not None else preset.get("band_assignments")
        mirror_center_hue = mirror_center_hue if mirror_center_hue != 0.0 else preset.get("mirror_center_hue", 0.0)
        wave_period_ticks = wave_period_ticks if wave_period_ticks != 40 else preset.get("wave_period_ticks", 40)

    if body.mode not in audio_reactive.MODES:
        raise HTTPException(400, f"unknown mode '{body.mode}', expected one of {audio_reactive.MODES}")
    if role_mode not in audio_reactive.ROLE_MODES:
        raise HTTPException(400, f"unknown role_mode '{role_mode}', expected one of {audio_reactive.ROLE_MODES}")
    if body.beat_sensitivity not in audio_reactive.BEAT_SENSITIVITY_PRESETS:
        raise HTTPException(400, f"unknown beat_sensitivity '{body.beat_sensitivity}', "
                                  f"expected one of {list(audio_reactive.BEAT_SENSITIVITY_PRESETS)}")
    ok, err = audio_reactive.validate_device_index(body.device_index)
    if not ok:
        raise HTTPException(400, err)
    if not audio_reactive.check_rate_limit(f"start:group:{group_id}"):
        raise HTTPException(429, "too many audio-reactive start/stop requests for this group — slow down")

    # Bulbs flagged audio_reactive_eligible=False never get auto-included
    # in a group session, even when they're a member of this group.
    device_ids = [
        d for d in group["device_ids"]
        if (cfgmod.get_device(d) or {}).get("audio_reactive_eligible", True)
    ]
    controllers = [bm.get_controller(d) for d in device_ids]
    controllers = [c for c in controllers if c is not None]
    if not controllers:
        raise HTTPException(400, "group has no resolvable, audio-reactive-eligible devices")

    # Section 8: conflict check — warn/reject if any bulb in this group is
    # already running its own solo audio-reactive session.
    conflicts = audio_reactive.check_group_conflict(device_ids)
    if conflicts and not body.force:
        raise HTTPException(
            409,
            f"device(s) {conflicts} already have an active solo audio-reactive session — "
            f"pass force=true to stop them and start the group session instead",
        )
    for device_id in conflicts:
        audio_reactive.stop_session(device_id)

    sensitivity = body.sensitivity
    if sensitivity is None:
        sensitivity = cfgmod.get_audio_input_calibration(body.device_index)
        if sensitivity is None:
            sensitivity = 1.0

    try:
        audio_reactive.start_group_session(
            group_id, controllers, body.device_index, body.mode, role_mode,
            sensitivity, body.monochrome_hue, body.min_dwell_ms,
            beat_sensitivity=body.beat_sensitivity,
            hue_offsets=hue_offsets, brightness_scales=brightness_scales,
            band_assignments=band_assignments,
            mirror_center_hue=mirror_center_hue, wave_period_ticks=wave_period_ticks,
            max_duration_s=body.max_duration_s, warmup_s=body.warmup_s,
            max_flash_rate_hz=body.max_flash_rate_hz, disable_flash_heavy=body.disable_flash_heavy,
            silence_auto_off=body.silence_auto_off, fallback_device_index=body.fallback_device_index,
        )
    except audio_reactive.AudioConfigError as e:
        raise HTTPException(400, str(e))
    return {
        "ok": True, "mode": body.mode, "role_mode": role_mode, "bulb_count": len(controllers),
        "sensitivity": sensitivity,
    }


@app.post("/api/groups/{group_id}/audio-reactive/stop")
def group_audio_reactive_stop(group_id: str):
    if not audio_reactive.check_rate_limit(f"stop:group:{group_id}"):
        raise HTTPException(429, "too many audio-reactive start/stop requests for this group — slow down")
    audio_reactive.stop_group_session(group_id)
    return {"ok": True}


@app.post("/api/groups/{group_id}/audio-reactive/tap-tempo")
def group_audio_reactive_tap_tempo(group_id: str, body: TapTempoBody = TapTempoBody()):
    tap_bpm = audio_reactive.tap_group_tempo(group_id, body.timestamp)
    return {"tap_bpm": tap_bpm}


@app.post("/api/groups/{group_id}/audio-reactive/beat-sensitivity")
def group_audio_reactive_beat_sensitivity(group_id: str, body: BeatSensitivityBody):
    if body.preset not in audio_reactive.BEAT_SENSITIVITY_PRESETS:
        raise HTTPException(400, f"unknown beat_sensitivity preset '{body.preset}', "
                                  f"expected one of {list(audio_reactive.BEAT_SENSITIVITY_PRESETS)}")
    if not audio_reactive.set_group_beat_sensitivity(group_id, body.preset):
        raise HTTPException(404, "no active audio-reactive session for this group")
    return {"ok": True, "beat_sensitivity": body.preset}


@app.post("/api/groups/{group_id}/audio-reactive/apply-preset")
def group_audio_reactive_apply_preset(group_id: str, body: GroupApplyAudioPresetBody):
    cfg = cfgmod.load_config()
    group = next((g for g in cfg.get("groups", []) if g["id"] == group_id), None)
    if not group:
        raise HTTPException(404, "group not found")
    preset = audio_reactive.find_genre_preset(body.preset_id)
    if not preset:
        preset = next((p for p in cfg.get("audio_custom_presets", []) if p["id"] == body.preset_id), None)
    if not preset:
        raise HTTPException(404, f"preset '{body.preset_id}' not found")
    controllers = [bm.get_controller(d) for d in group["device_ids"]]
    controllers = [c for c in controllers if c is not None]
    if not controllers:
        raise HTTPException(400, "group has no resolvable devices")
    audio_reactive.start_group_session(group_id, controllers, body.device_index, preset["mode"], body.role_mode,
                                        preset["sensitivity"], preset["monochrome_hue"], preset["min_dwell_ms"],
                                        preset["beat_sensitivity"])
    return {"ok": True, "preset_id": preset["id"], "mode": preset["mode"], "role_mode": body.role_mode}


@app.get("/api/groups/{group_id}/audio-reactive/status")
def group_audio_reactive_status(group_id: str):
    return {"data_source": "LIVE DATA", **audio_reactive.get_group_session_status(group_id)}


@app.get("/api/devices/{device_id}/audio-reactive/status")
def audio_reactive_status(device_id: str):
    return {"data_source": "LIVE DATA", **audio_reactive.get_session_status(device_id)}


# ------------------------------------------------------------ lightshow ---
@app.post("/api/devices/{device_id}/lightshow/export")
def lightshow_export(device_id: str, body: LightshowExportBody):
    """Section 12: export the color-over-time sequence a session actually
    sent (captured live if still running, or from the most recent session
    if it was just stopped) as a replayable light show."""
    session = audio_reactive.get_active_session(device_id)
    if session:
        points = session.sender.get_captured_points()
    else:
        points = audio_reactive.get_last_capture(device_id)
    try:
        record = audio_lightshow.export_lightshow(device_id, body.name, points)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {k: v for k, v in record.items() if k != "points"}


@app.get("/api/devices/{device_id}/lightshow")
def lightshow_list(device_id: str):
    return audio_lightshow.list_lightshows(device_id)


@app.delete("/api/lightshow/{lightshow_id}")
def lightshow_delete(lightshow_id: str):
    found = audio_lightshow.delete_lightshow(lightshow_id)
    if not found:
        raise HTTPException(404, "lightshow not found")
    return {"ok": True}


@app.post("/api/devices/{device_id}/lightshow/replay")
def lightshow_replay_start(device_id: str, body: LightshowReplayBody):
    c = get_controller_or_404(device_id)
    try:
        audio_lightshow.start_replay(device_id, c, body.lightshow_id, loop=body.loop)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"ok": True, "lightshow_id": body.lightshow_id, "loop": body.loop}


@app.post("/api/devices/{device_id}/lightshow/replay/stop")
def lightshow_replay_stop(device_id: str):
    audio_lightshow.stop_replay(device_id)
    return {"ok": True}


@app.get("/api/devices/{device_id}/lightshow/replay/status")
def lightshow_replay_status(device_id: str):
    return {"data_source": "LIVE DATA", **audio_lightshow.get_replay_status(device_id)}


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


# ----------------------------------------------------------------- zones --
# A Zone sits above groups (e.g. "Living Room" containing several groups
# and/or loose bulbs) -- real CRUD + membership, persisted in config.json
# the same way groups already are.
@app.get("/api/zones")
def list_zones():
    cfg = cfgmod.load_config()
    return cfg.get("zones", [])


@app.post("/api/zones")
def create_zone(body: ZoneCreateBody):
    if cfgmod.get_zone(body.id):
        raise HTTPException(400, f"zone '{body.id}' already exists")
    zone = body.model_dump()
    cfgmod.upsert_zone(zone)
    return zone


@app.get("/api/zones/{zone_id}")
def get_zone_route(zone_id: str):
    zone = cfgmod.get_zone(zone_id)
    if not zone:
        raise HTTPException(404, "zone not found")
    return {**zone, "resolved_device_ids": cfgmod.zone_resolved_device_ids(zone)}


@app.patch("/api/zones/{zone_id}")
def update_zone(zone_id: str, body: dict):
    zone = cfgmod.get_zone(zone_id)
    if not zone:
        raise HTTPException(404, "zone not found")
    zone.update(body)
    cfgmod.upsert_zone(zone)
    return zone


@app.delete("/api/zones/{zone_id}")
def delete_zone_route(zone_id: str):
    cfgmod.delete_zone(zone_id)
    return {"ok": True}


@app.post("/api/zones/{zone_id}/devices")
def zone_add_device(zone_id: str, body: ZoneDeviceBody):
    zone = cfgmod.get_zone(zone_id)
    if not zone:
        raise HTTPException(404, "zone not found")
    zone.setdefault("device_ids", [])
    if body.device_id not in zone["device_ids"]:
        zone["device_ids"].append(body.device_id)
        cfgmod.upsert_zone(zone)
    return zone


@app.delete("/api/zones/{zone_id}/devices/{device_id}")
def zone_remove_device(zone_id: str, device_id: str):
    zone = cfgmod.get_zone(zone_id)
    if not zone:
        raise HTTPException(404, "zone not found")
    zone["device_ids"] = [d for d in zone.get("device_ids", []) if d != device_id]
    cfgmod.upsert_zone(zone)
    return zone


# ------------------------------------------------- orchestration presets --
@app.get("/api/orchestration-presets")
def list_orchestration_presets():
    return cfgmod.list_orchestration_presets()


@app.post("/api/orchestration-presets")
def save_orchestration_preset(body: OrchestrationPresetBody):
    if body.role_mode not in audio_reactive.ROLE_MODES:
        raise HTTPException(400, f"unknown role_mode '{body.role_mode}', expected one of {audio_reactive.ROLE_MODES}")
    preset = body.model_dump()
    cfgmod.upsert_orchestration_preset(preset)
    return preset


@app.get("/api/orchestration-presets/{preset_id}")
def get_orchestration_preset_route(preset_id: str):
    preset = cfgmod.get_orchestration_preset(preset_id)
    if not preset:
        raise HTTPException(404, "orchestration preset not found")
    return preset


@app.delete("/api/orchestration-presets/{preset_id}")
def delete_orchestration_preset_route(preset_id: str):
    cfgmod.delete_orchestration_preset(preset_id)
    return {"ok": True}


@app.on_event("startup")
def on_startup():
    schedule_engine.start_scheduler(bm.get_controller)
    discovery.start_scheduler()


# ------------------------------------------------------------ discovery ---
@app.get("/api/system/discovery")
def discovery_state():
    return discovery.get_state()


@app.post("/api/system/scan")
def discovery_scan_now():
    return discovery.scan_now()


@app.post("/api/system/discovery/interval")
def discovery_set_interval(body: DiscoveryIntervalBody):
    return discovery.set_interval_hours(body.hours)


@app.post("/api/system/discovery/{device_id}/ignore")
def discovery_ignore(device_id: str):
    discovery.ignore_device(device_id)
    return {"ok": True}


@app.post("/api/system/discovery/{device_id}/unignore")
def discovery_unignore(device_id: str):
    discovery.unignore_device(device_id)
    return {"ok": True}


@app.delete("/api/system/discovery/{device_id}")
def discovery_forget(device_id: str):
    discovery.forget_discovered(device_id)
    return {"ok": True}


# -------------------------------------------------------------- analytics -
@app.get("/api/analytics/usage")
def analytics_usage(period: str = "today"):
    try:
        return analytics.usage_summary(period)
    except ValueError as e:
        raise HTTPException(400, str(e))


# --------------------------------------------------------- static files ---
FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend")
if os.path.isdir(FRONTEND_DIR):
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

    @app.get("/")
    def index():
        return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))
