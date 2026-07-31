import os
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from pydantic import BaseModel

import secrets_env

# Before anything reads config: a `.env` may be supplying device local_keys
# that config.json deliberately leaves blank. Done here, at the entry point,
# rather than as an import side effect of secrets_env itself -- a module
# that mutates os.environ merely by being imported is a nasty surprise in a
# test run. See secrets_env.py and .env.example.
#
# reverse_proxy also reads its SBD_* settings from the environment, so it has
# to be imported after this call too, or a .env-supplied trusted-proxy list
# would be silently ignored.
secrets_env.load_env_file()

import config as cfgmod  # noqa: E402
import bulb_manager as bm  # noqa: E402
import schedule_engine  # noqa: E402
import discovery  # noqa: E402
import audio_reactive  # noqa: E402
import audio_signal  # noqa: E402
import audio_presets  # noqa: E402
import audio_safety  # noqa: E402
import audio_lightshow  # noqa: E402
import remote_auth  # noqa: E402
import api_rate_limit  # noqa: E402
import reverse_proxy  # noqa: E402
import analytics  # noqa: E402
import security_audit  # noqa: E402
import backup_restore  # noqa: E402
import observability  # noqa: E402
import network_health  # noqa: E402
import remote_access_status  # noqa: E402
from scenes_presets import PRESET_COLORS, SCENES, EFFECTS  # noqa: E402

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


# Added LAST, so it is the OUTERMOST middleware and runs before both
# https_enforcement and pin_gate. Deliberate: a flood should be turned away
# before it can make the gate do PBKDF2 work or touch the state file, and
# before we spend anything on a redirect.
#
# This middleware is also the ONLY place api_rate_limit.check() is called,
# and that's the whole of the W2-111 guarantee: the audio-reactive engine's
# internal per-bulb dispatch never enters the ASGI stack, so a running
# lightshow cannot spend a rate-limit budget meant for HTTP clients.
@app.middleware("http")
async def api_rate_limiter(request: Request, call_next):
    # reverse_proxy.client_ip, not request.client.host. Behind a trusted
    # proxy every request arrives from the proxy's own address, so keying
    # the limiter on the raw peer would drop every client into ONE shared
    # bucket -- one busy tab would then rate-limit the whole household.
    # Phase B's auth lockout already resolves the real client this way; the
    # limiter has to agree with it or the two disagree about who a caller is.
    ip = reverse_proxy.client_ip(request)
    allowed, retry_after, tier = api_rate_limit.check(ip, request.method, request.url.path)
    if not allowed:
        remote_auth.log_audit_event(
            "api_rate_limited", "blocked", ip=ip, tier=tier, path=request.url.path,
        )
        return JSONResponse(
            {"detail": f"rate limit exceeded for {tier} requests -- slow down"},
            status_code=429,
            headers={"Retry-After": str(int(retry_after) + 1)},
        )
    return await call_next(request)


# Added LAST of all, so it is the outermost middleware: it therefore observes
# everything the others do, including the gate's 401s AND the rate limiter's
# 429s. A burst of either is exactly the signal you want on the metrics page,
# and a limiter that silently dropped traffic before it was counted would make
# an attack look like a traffic lull.
@app.middleware("http")
async def observe_request(request: Request, call_next):
    """Per-request observability: adopt or mint a correlation id, time the
    request, and record it against its route *template* for the metrics
    endpoint. Also the one place that notices a request arriving from a
    globally-routable address, which is what arms the "you're exposed and
    the PIN gate is off" banner."""
    correlation_id = observability.adopt_correlation_id(
        request.headers.get(observability.CORRELATION_HEADER))
    started = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        response.headers[observability.CORRELATION_HEADER] = correlation_id
        return response
    finally:
        duration = time.perf_counter() - started
        template = observability.route_template(app.routes, request.method, request.url.path)
        observability.record_request(request.method, template, status_code, duration)
        if request.client:
            # Best-effort: a bookkeeping failure here must never affect the
            # response the client already got. Proxy-aware for the same reason
            # the limiter is -- behind a proxy the raw peer is the proxy's own
            # (usually private) address, so exposure detection would never see
            # the public client this check exists to notice.
            try:
                remote_access_status.note_client_ip(reverse_proxy.client_ip(request))
            except Exception:
                pass


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


class SecurityConfigBody(BaseModel):
    min_severity: str | None = None
    alert_min_severity: str | None = None
    severity_overrides: dict | None = None
    alert_thresholds: dict | None = None
    webhook_enabled: bool | None = None
    webhook_url: str | None = None
    local_alerts_enabled: bool | None = None
    max_log_bytes: int | None = None
    rotate_keep: int | None = None
    retention_days: int | None = None


class BackupCreateBody(BaseModel):
    # None means "no encryption" -- an explicit, acknowledged choice, not an
    # oversight. See the warning the create endpoint returns.
    password: str | None = None
    exclude: list[str] | None = None
    note: str | None = None


class BackupPasswordBody(BaseModel):
    password: str | None = None


class BackupRestoreBody(BaseModel):
    password: str | None = None
    confirm: bool = False
    sections: list[str] | None = None


class BackupSettingsBody(BaseModel):
    keep: int


class LoginRateLimitBody(BaseModel):
    max_attempts: int
    window_s: int


class LockoutPolicyBody(BaseModel):
    max_attempts: int | None = None
    base_seconds: int | None = None
    max_seconds: int | None = None


class SessionTtlBody(BaseModel):
    session_ttl_s: int


class PinCheckBody(BaseModel):
    pin: str


class PinChangeBody(BaseModel):
    pin: str


class GuestPinBody(BaseModel):
    pin: str
    label: str | None = None
    expires_in_s: int | None = None


class ApiRateLimitBody(BaseModel):
    enabled: bool | None = None
    exempt_local: bool | None = None
    limits: dict[str, int] | None = None
class LogLevelBody(BaseModel):
    level: str


class DuckDnsSyncBody(BaseModel):
    domain: str
    ip: str | None = None
    ok: bool = True
    detail: str | None = None


class ExposureBody(BaseModel):
    configured: bool
    source: str | None = None


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


@app.get("/api/system/diagnostics/rate-limit")
def diagnostics_rate_limit():
    """Live rate-limiting picture for the Diagnostics panel (W2-109): the
    general per-IP limiter's counters plus the auth side's lockout/login
    limiter counters, which are separate mechanisms and worth reading
    together when judging whether something is being attacked. All of it is
    in-memory and resets with the process."""
    return {
        "api": api_rate_limit.metrics(),
        "auth": remote_auth.auth_metrics(),
    }


@app.get("/api/system/rate-limit")
def get_api_rate_limit_config():
    return api_rate_limit.config()


@app.post("/api/system/rate-limit")
def set_api_rate_limit_config(body: ApiRateLimitBody):
    try:
        return api_rate_limit.configure(body.enabled, body.exempt_local, body.limits)
    except ValueError as e:
        raise HTTPException(400, str(e))


# --------------------------------------------------------- observability --
# Note on auth: /metrics is NOT in remote_auth.OPEN_PATHS, so it is gated
# along with everything else once the PIN gate is on. Endpoint names, call
# counts and error rates are a decent map of the install for anyone
# probing it, and the LAN-only default (gate off) already leaves it open
# for a local Prometheus. Scraping through an enabled gate needs a session
# cookie -- documented in SECURITY.md rather than silently exempted here.
@app.get("/metrics", response_class=PlainTextResponse)
def metrics():
    return PlainTextResponse(observability.prometheus_text(version=APP_VERSION),
                              media_type="text/plain; version=0.0.4; charset=utf-8")


@app.get("/api/system/metrics")
def metrics_json():
    return observability.metrics_snapshot()


@app.get("/api/system/dependencies")
def system_dependencies(refresh: bool = False):
    return observability.dependency_summary(force=refresh)


@app.get("/api/system/health-summary")
def health_summary():
    """The dashboard-level health page -- deliberately distinct from the
    per-device Diagnostics tab, which answers "is THIS bulb reachable".
    This one answers "is the backend itself healthy": uptime, dependency
    state, request/error rates, network mode, and whether anything is
    shouting in the logs."""
    metrics = observability.metrics_snapshot()
    dependencies = observability.dependency_summary()
    connectivity = network_health.connectivity_summary()
    recent_errors = observability.recent_logs(limit=20, level="ERROR")
    slowest = max(
        (e for e in metrics["endpoints"] if e["p95_ms"] is not None),
        key=lambda e: e["p95_ms"], default=None,
    )
    problems = []
    if not dependencies["ok"]:
        problems.append("a required dependency is missing or broken")
    if dependencies["degraded"]:
        problems.append(f"degraded: {', '.join(dependencies['degraded'])} unavailable")
    if not connectivity["bulb_control_available"]:
        problems.append("no LAN address -- bulb control cannot work")
    if metrics["totals"]["error_rate"] > 0.05:
        problems.append(f"server error rate is {metrics['totals']['error_rate']:.1%}")
    if recent_errors:
        problems.append(f"{len(recent_errors)} recent error log entr{'y' if len(recent_errors) == 1 else 'ies'}")

    return {
        "data_source": "LIVE DATA",
        "healthy": not problems,
        "problems": problems,
        "process": {
            "version": APP_VERSION,
            "uptime_seconds": round(time.time() - START_TIME, 1),
            "started_at": observability.STARTED_AT_ISO,
            "python": observability._python_version(),
            "log_level": observability.get_log_level(),
        },
        "dependencies": dependencies,
        "requests": metrics["totals"],
        "status_classes": metrics["status_classes"],
        "slowest_endpoint": slowest,
        "endpoints": metrics["endpoints"],
        "network": connectivity,
        "bulb_latency": network_health.all_latency_summaries(),
        "recent_errors": recent_errors,
    }


@app.get("/api/system/logs")
def system_logs(limit: int = 100, level: str | None = None):
    try:
        return {"data_source": "LIVE DATA", "log_level": observability.get_log_level(),
                "buffer_size": observability.LOG_BUFFER_SIZE,
                "entries": observability.recent_logs(limit=limit, level=level)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/system/log-level")
def get_log_level():
    return {"log_level": observability.get_log_level(), "levels": list(observability.LOG_LEVELS)}


@app.post("/api/system/log-level")
def set_log_level(body: LogLevelBody):
    try:
        return observability.set_log_level(body.level)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/system/diagnostic-report")
def diagnostic_report(log_limit: int = 200, history_limit: int = 20):
    """A shareable "here's my install" bundle with every secret stripped.
    Returned to the caller only -- never written to disk, so nothing
    generated here can end up sitting in the repo waiting to be committed."""
    return observability.diagnostic_report(log_limit=log_limit, history_limit=history_limit,
                                            version=APP_VERSION)


# ---------------------------------------------------------- network state -
@app.get("/api/system/network")
def system_network():
    return {
        "data_source": "LIVE DATA",
        "state": network_health.get_state(),
        "connectivity": network_health.connectivity_summary(),
        "firewall": {
            "lan_only_ports": network_health.LAN_ONLY_PORTS,
            "note": ("For LAN-only operation nothing needs to be open to the internet. "
                     "See docs/remote-access-security.md."),
        },
    }


@app.post("/api/system/network/refresh")
def system_network_refresh():
    """Take a reading now instead of waiting for the monitor's next tick --
    what you press after plugging the ethernet back in."""
    return network_health.poll()


@app.get("/api/devices/{device_id}/latency-history")
def device_latency_history(device_id: str, limit: int = 100):
    get_controller_or_404(device_id)
    return network_health.latency_history(device_id, limit=limit)


# --------------------------------------------------- remote-access status -
@app.get("/api/system/remote-access/status")
def remote_access_status_route(check_tailscale: bool = False):
    return remote_access_status.status(include_live_lookups=check_tailscale)


@app.get("/api/system/remote-access/tailscale")
def remote_access_tailscale():
    return remote_access_status.tailscale_status()


@app.post("/api/system/remote-access/detect-public-ip")
def remote_access_detect_public_ip():
    """The only outbound internet request this project makes, and only when
    a user explicitly asks for it. See SECURITY.md's no-telemetry section."""
    return remote_access_status.detect_public_ip()


@app.post("/api/system/remote-access/duckdns-sync")
def remote_access_duckdns_sync(body: DuckDnsSyncBody):
    """Called by whatever updates the user's DuckDNS record, so Settings can
    show a real "last synced" time. This project runs no updater itself."""
    remote_access_status.record_duckdns_sync(body.domain, body.ip, body.ok, body.detail)
    return remote_access_status.status()


@app.post("/api/system/remote-access/exposure")
def remote_access_set_exposure(body: ExposureBody):
    remote_access_status.mark_exposure(body.configured, body.source)
    return remote_access_status.status()


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

    ok, detail, pin_id = remote_auth.verify_pin(body.pin, ip)
    if not ok:
        locked = bool(detail and "locked out" in detail)
        event = "login_lockout" if locked else "login_failure"
        # W2-068: the API response already distinguishes these two; carry the
        # distinction into the audit log so a reviewer can tell a fumbled
        # PIN apart from a request that never got to try one. Field names
        # here deliberately avoid the substring "pin" -- the audit log is
        # asserted to be free of it, which is the cheapest possible check
        # that no PIN value ever lands in the file.
        remote_auth.log_audit_event(
            event, "failure", ip=ip, reason="locked_out" if locked else "wrong_credential",
        )
        raise HTTPException(401, detail)

    token = remote_auth.create_session_token(ip=ip, pin_id=pin_id)
    remote_auth.log_audit_event("login_success", "success", ip=ip, credential_id=pin_id)
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


@app.post("/api/system/remote-auth/lockout-policy")
def remote_auth_set_lockout_policy(body: LockoutPolicyBody):
    try:
        return remote_auth.set_lockout_policy(
            body.max_attempts, body.base_seconds, body.max_seconds,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/system/remote-auth/session-ttl")
def remote_auth_set_session_ttl(body: SessionTtlBody):
    try:
        remote_auth.set_session_ttl(body.session_ttl_s)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return remote_auth.status()


@app.post("/api/system/remote-auth/pin-strength")
def remote_auth_pin_strength(body: PinCheckBody):
    """Grade a candidate PIN without committing to it, so the Settings UI can
    warn before the user clicks Enable. Advisory only -- enable/change apply
    the same rules server-side and reject regardless of what the UI did."""
    return remote_auth.assess_pin(body.pin)


@app.post("/api/system/remote-auth/pin")
def remote_auth_change_pin(body: PinChangeBody, request: Request):
    """Change the household PIN. Every existing session dies with the old
    PIN (W2-063), including this caller's -- so a fresh cookie is issued in
    the same response rather than bouncing the user to the login screen for
    an action they just authenticated for."""
    try:
        revoked = remote_auth.change_pin(body.pin)
    except ValueError as e:
        raise HTTPException(400, str(e))
    # Proxy-aware, matching the login path and the rate limiter -- these all
    # have to agree on who the caller is, or an audit entry names one address
    # while the lockout counts another.
    ip = reverse_proxy.client_ip(request)
    remote_auth.log_audit_event("pin_changed", "success", ip=ip, revoked_sessions=revoked)
    token = remote_auth.create_session_token(ip=ip, pin_id=remote_auth.household_pin_id())
    resp = JSONResponse({"ok": True, "revoked_sessions": revoked})
    resp.set_cookie(remote_auth.SESSION_COOKIE, token, httponly=True, samesite="lax",
                     max_age=remote_auth.get_session_ttl())
    return resp


@app.get("/api/system/remote-auth/pins")
def remote_auth_list_pins():
    return {"pins": remote_auth.list_pins()}


@app.post("/api/system/remote-auth/pins")
def remote_auth_add_guest_pin(body: GuestPinBody, request: Request):
    try:
        created = remote_auth.add_guest_pin(body.pin, body.label, body.expires_in_s)
    except ValueError as e:
        raise HTTPException(400, str(e))
    # Proxy-aware, matching the login path and the rate limiter -- these all
    # have to agree on who the caller is, or an audit entry names one address
    # while the lockout counts another.
    ip = reverse_proxy.client_ip(request)
    remote_auth.log_audit_event("guest_pin_added", "success", ip=ip, credential_id=created["id"])
    return created


@app.delete("/api/system/remote-auth/pins/{pin_id}")
def remote_auth_revoke_pin(pin_id: str, request: Request):
    try:
        found, sessions = remote_auth.revoke_pin(pin_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    # Proxy-aware, matching the login path and the rate limiter -- these all
    # have to agree on who the caller is, or an audit entry names one address
    # while the lockout counts another.
    ip = reverse_proxy.client_ip(request)
    remote_auth.log_audit_event(
        "guest_pin_revoked", "success" if found else "not_found",
        ip=ip, credential_id=pin_id, revoked_sessions=sessions,
    )
    if not found:
        raise HTTPException(404, "PIN not found")
    return {"ok": True, "revoked_sessions": sessions}


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
    # Dependency check FIRST, before any thread is spawned: a missing
    # tinytuya should stop the process here with a message naming the fix,
    # not surface twenty minutes later as an opaque 500 on the first bulb
    # command. Raises DependencyError, which aborts startup.
    observability.startup_dependency_check()
    logger = observability.get_logger()
    logger.info("smart-bulb-dashboard %s starting (log level %s)",
                APP_VERSION, observability.get_log_level())
    network_health.start_monitor()
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


# ------------------------------------------------------- security audit ---
@app.get("/api/security/events")
def security_events(limit: int = 100, since: float | None = None, until: float | None = None,
                    event: str | None = None, min_severity: str | None = None,
                    outcome: str | None = None, q: str | None = None,
                    include_rotated: bool = False):
    try:
        events = security_audit.read_events(
            limit=limit, since=since, until=until, event=event,
            min_severity=min_severity, outcome=outcome, q=q,
            include_rotated=include_rotated,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {
        "events": events,
        "count": len(events),
        "severities": list(security_audit.SEVERITIES),
        "known_events": sorted(security_audit.DEFAULT_SEVERITIES),
        "data_source": "LIVE DATA (this host's security event log)",
    }


@app.get("/api/security/events/export")
def security_events_export(format: str = "json", limit: int = 0, since: float | None = None,
                           until: float | None = None, event: str | None = None,
                           min_severity: str | None = None, outcome: str | None = None,
                           q: str | None = None, include_rotated: bool = True):
    try:
        content, media_type, filename = security_audit.export_events(
            fmt=format, limit=limit, since=since, until=until, event=event,
            min_severity=min_severity, outcome=outcome, q=q, include_rotated=include_rotated,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return Response(content=content, media_type=media_type,
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/api/security/verify")
def security_verify(include_rotated: bool = True):
    """Tamper check (W2-152). Read-only -- it never repairs or re-seals the
    chain, because a 'fix it' button on a tamper-evidence feature would let
    the tampering be papered over with one click."""
    return security_audit.verify_chain(include_rotated=include_rotated)


@app.post("/api/security/events/rotate")
def security_rotate():
    security_audit.rotate_now()
    return {"ok": True, "removed": security_audit.apply_retention()}


@app.get("/api/security/config")
def security_config_get():
    return security_audit.get_config()


@app.post("/api/security/config")
def security_config_set(body: SecurityConfigBody):
    try:
        return security_audit.update_config(**body.model_dump(exclude_unset=True))
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e))


@app.get("/api/security/alerts")
def security_alerts(limit: int = 50, unacknowledged_only: bool = False):
    return {"alerts": security_audit.list_alerts(limit=limit,
                                                 unacknowledged_only=unacknowledged_only)}


@app.post("/api/security/alerts/ack")
def security_alerts_ack():
    return {"ok": True, "acknowledged": security_audit.acknowledge_alerts()}


@app.get("/api/security/digest")
def security_digest(days: int = 7):
    if days < 1:
        raise HTTPException(400, "days must be >= 1")
    return security_audit.digest(days=days)


@app.post("/api/security/self-test")
def security_self_test():
    return security_audit.self_test()


@app.get("/api/security/secrets")
def security_secrets():
    """Where each secret lives and what it's worth to an attacker (W2-223).
    Returns no secret values, lengths or prefixes -- only presence and
    source, which is everything a settings page legitimately needs."""
    return secrets_env.secret_inventory(cfgmod.load_config())


# ------------------------------------------------------ backup / restore ---
@app.get("/api/backups")
def backups_list():
    return {"backups": backup_restore.list_backups(), "settings": backup_restore.get_settings()}


@app.get("/api/backups/options")
def backups_options():
    """What can be excluded from a backup and what can be selectively
    restored -- so the UI never has to hardcode either list."""
    return {
        "exclusions": backup_restore.optional_exclusions(),
        "sections": [dict(backup_restore.SECTIONS[k], id=k) for k in backup_restore.SECTIONS],
        "never_included": sorted(backup_restore.HARD_EXCLUDED_DATA
                                 | set(backup_restore.HARD_EXCLUDED_PREFIXES)),
    }


@app.get("/api/backups/settings")
def backups_settings_get():
    return backup_restore.get_settings()


@app.post("/api/backups/settings")
def backups_settings_set(body: BackupSettingsBody):
    try:
        return backup_restore.update_settings(keep=body.keep)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/backups")
def backups_create(body: BackupCreateBody):
    try:
        return backup_restore.create_backup(password=body.password, exclude=body.exclude,
                                            note=body.note)
    except ValueError as e:
        raise HTTPException(400, str(e))


# POST, not GET, for everything below that takes a password: a password in a
# query string ends up in server logs, browser history and referrers.
@app.post("/api/backups/{name}/verify")
def backups_verify(name: str, body: BackupPasswordBody = BackupPasswordBody()):
    try:
        return backup_restore.verify_backup(name, password=body.password)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/backups/{name}/preflight")
def backups_preflight(name: str, body: BackupPasswordBody = BackupPasswordBody()):
    try:
        return backup_restore.restore_preflight(name, password=body.password)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/backups/{name}/diff")
def backups_diff(name: str, body: BackupPasswordBody = BackupPasswordBody()):
    try:
        return backup_restore.diff_backup(name, password=body.password)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except PermissionError as e:
        raise HTTPException(401, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/backups/{name}/download")
def backups_download(name: str):
    try:
        content, filename = backup_restore.export_bytes(name)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return Response(content=content, media_type="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.post("/api/backups/{name}/restore")
def backups_restore(name: str, body: BackupRestoreBody):
    try:
        return backup_restore.restore_backup(name, password=body.password,
                                             confirm=body.confirm, sections=body.sections)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except PermissionError as e:
        # Covers both "you didn't confirm" and "this archive needs a
        # password" -- 409 rather than 400 because nothing is wrong with the
        # request's shape, it's the state/consent that's missing.
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/backups/{name}")
def backups_delete(name: str):
    try:
        return backup_restore.delete_backup(name)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


# --------------------------------------------------------- static files ---
FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend")
if os.path.isdir(FRONTEND_DIR):
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

    @app.get("/")
    def index():
        return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))
