"""General per-IP rate limiting for the public HTTP surface.

Three limiters exist in this codebase and they are deliberately separate:

  - `audio_reactive.check_rate_limit` caps audio-session start/stop churn per
    device/group. Feature-specific.
  - `remote_auth.check_login_rate_limit` caps request volume to the login
    endpoint. Auth-specific, and stricter than anything here.
  - this module, which caps overall request volume per client IP.

**W2-111.** This is enforced from an ASGI middleware and nowhere else, which
is what keeps it off the audio-reactive engine's back. The audio pipeline's
high-frequency work -- the per-bulb `BulbSender` threads pushing colour
updates dozens of times a second -- never travels through the HTTP stack;
it goes straight from an in-process queue to tinytuya. So there is no path
by which a running lightshow can consume this budget. Do not "helpfully"
call check() from service-layer code: the moment it's callable from inside
the app, internal dispatch starts counting against a limit meant for
external clients, and a long lightshow starts 429-ing the user's own
browser.

State (counters) is in-memory and resets on restart -- the same accepted
tradeoff already documented for the PIN gate's lockout tracker. Config is
in-memory too: env vars are the durable way to change a default, and
configure() is the runtime override.
"""

import collections
import os
import threading
import time

from net_utils import is_local_ip, normalize_ip

WINDOW_S = 60.0

# Per-minute allowances by endpoint sensitivity (W2-102). Read-only traffic
# gets a bigger budget than state-changing traffic because it's cheap and
# because a dashboard legitimately polls; "expensive" covers the handful of
# endpoints that each kick off seconds of real network I/O.
DEFAULT_LIMITS = {
    # The live-status endpoints the dashboard polls on a timer. The audio
    # panel polls its status route every 300ms while a session runs -- 200
    # requests/minute from one perfectly well-behaved browser tab -- so this
    # tier has to sit well above that or remote use of the audio panel
    # would 429 itself.
    "poll": int(os.environ.get("SBD_RATE_LIMIT_POLL", "600")),
    "read": int(os.environ.get("SBD_RATE_LIMIT_READ", "240")),
    "write": int(os.environ.get("SBD_RATE_LIMIT_WRITE", "120")),
    "expensive": int(os.environ.get("SBD_RATE_LIMIT_EXPENSIVE", "10")),
}

# Substring matches against the request path, checked before the
# method-based read/write split. Ordered most-specific-first.
TIER_BY_PATH = (
    ("/audio-reactive/status", "poll"),
    ("/api/system/health", "poll"),
    ("/api/system/scan", "expensive"),
    ("/rescan", "expensive"),
    ("/test-connection", "expensive"),
    ("/api/audio/calibrate", "expensive"),
)

# Not counted at all: the dashboard page and its assets (one page load is a
# burst of static requests that says nothing about API abuse -- a reverse
# proxy is the right layer for that), and the login endpoint, which has its
# own stricter limiter in remote_auth and must not have its 429 semantics
# muddled by this one.
EXEMPT_PREFIXES = ("/static/", "/docs", "/openapi.json", "/redoc")
# /api/stream is a single long-lived SSE connection, not a request rate. It
# is counted once at connect and then stays open for minutes -- billing it
# per-connection would let a page reload a handful of times and hit a limit
# meant for hundreds of ordinary calls. It is still behind the PIN gate; this
# only exempts it from the request counter.
EXEMPT_PATHS = {"/", "/favicon.ico", "/api/auth/login", "/api/stream"}

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

_lock = threading.Lock()
# (normalized_ip, tier) -> deque of request timestamps. Sliding window
# rather than fixed: a fixed window lets a client fire the full allowance
# twice in quick succession by straddling the reset boundary.
_hits = {}

_config = {
    "enabled": True,
    # Loopback and LAN clients are exempt by default (W2-104): the point of
    # this limiter is unattended public exposure, and throttling the user's
    # own phone on their own Wi-Fi is a bug, not a security win.
    "exempt_local": os.environ.get("SBD_RATE_LIMIT_EXEMPT_LOCAL", "1") != "0",
    "limits": dict(DEFAULT_LIMITS),
}

_metrics = {
    "allowed": 0,
    "blocked": 0,
    "exempt": 0,
    "blocked_by_tier": collections.Counter(),
    "blocked_by_ip": collections.Counter(),
    "last_blocked_at": None,
    "last_blocked_path": None,
}


def tier_for(method, path):
    """Which allowance bucket a request draws from. Path overrides win over
    the method split so a GET that costs an 18-second LAN scan isn't priced
    like a GET that reads a dict."""
    for needle, tier in TIER_BY_PATH:
        if needle in path:
            return tier
    return "read" if method.upper() in SAFE_METHODS else "write"


def is_exempt_path(path):
    return path in EXEMPT_PATHS or path.startswith(EXEMPT_PREFIXES)


def configure(enabled=None, exempt_local=None, limits=None):
    """Runtime tuning. Not persisted -- restart returns to the env-var
    defaults, which is the durable place to change these."""
    with _lock:
        if enabled is not None:
            _config["enabled"] = bool(enabled)
        if exempt_local is not None:
            _config["exempt_local"] = bool(exempt_local)
        for tier, value in (limits or {}).items():
            if tier not in _config["limits"]:
                raise ValueError(f"unknown rate-limit tier '{tier}'")
            if int(value) < 1:
                raise ValueError(f"limit for '{tier}' must be >= 1")
            _config["limits"][tier] = int(value)
        return _snapshot_config()


def _snapshot_config():
    return {
        "enabled": _config["enabled"],
        "exempt_local": _config["exempt_local"],
        "window_s": WINDOW_S,
        "limits": dict(_config["limits"]),
    }


def config():
    with _lock:
        return _snapshot_config()


def check(ip, method, path):
    """Account for one inbound public request.

    Returns (allowed, retry_after_seconds, tier). A blocked request is NOT
    counted against the window, so a client hammering while blocked doesn't
    push its own recovery time out indefinitely -- it recovers exactly when
    its oldest real request ages out.
    """
    if is_exempt_path(path):
        return True, 0.0, None

    tier = tier_for(method, path)
    now = time.time()

    with _lock:
        if not _config["enabled"]:
            return True, 0.0, tier
        if _config["exempt_local"] and is_local_ip(ip):
            _metrics["exempt"] += 1
            return True, 0.0, tier

        key = (normalize_ip(ip), tier)
        limit = _config["limits"][tier]
        dq = _hits.setdefault(key, collections.deque())
        while dq and now - dq[0] >= WINDOW_S:
            dq.popleft()
        if len(dq) >= limit:
            retry_after = max(0.0, WINDOW_S - (now - dq[0]))
            _metrics["blocked"] += 1
            _metrics["blocked_by_tier"][tier] += 1
            _metrics["blocked_by_ip"][key[0]] += 1
            _metrics["last_blocked_at"] = now
            _metrics["last_blocked_path"] = path
            return False, retry_after, tier
        dq.append(now)
        _metrics["allowed"] += 1
        return True, 0.0, tier


def metrics(top_ips=5):
    """Rate-limit counters for the diagnostics API (W2-109): how often
    limits are actually being hit, and by whom. `current` shows live window
    usage per tier for the busiest clients, which is what makes this useful
    for "is an attack happening right now" rather than just "has one ever
    happened"."""
    now = time.time()
    with _lock:
        usage = collections.Counter()
        for (ip, tier), dq in _hits.items():
            live = sum(1 for t in dq if now - t < WINDOW_S)
            if live:
                usage[f"{ip} [{tier}]"] = live
        return {
            "config": _snapshot_config(),
            "allowed": _metrics["allowed"],
            "blocked": _metrics["blocked"],
            "exempt": _metrics["exempt"],
            "blocked_by_tier": dict(_metrics["blocked_by_tier"]),
            "top_blocked_ips": [
                {"ip": ip, "blocked": n}
                for ip, n in _metrics["blocked_by_ip"].most_common(top_ips)
            ],
            "last_blocked_at": _metrics["last_blocked_at"],
            "last_blocked_path": _metrics["last_blocked_path"],
            "tracked_clients": len({ip for ip, _ in _hits}),
            "current_window_usage": [
                {"client": k, "requests": n} for k, n in usage.most_common(top_ips)
            ],
        }


def reset():
    """Drop all counters and metrics. Exists for the test suite -- this is
    module-level state shared by every test in a process, exactly like
    audio_reactive._rate_limit_hits, which leaked across tests until an
    autouse fixture cleared it."""
    with _lock:
        _hits.clear()
        _metrics.update({
            "allowed": 0,
            "blocked": 0,
            "exempt": 0,
            "blocked_by_tier": collections.Counter(),
            "blocked_by_ip": collections.Counter(),
            "last_blocked_at": None,
            "last_blocked_path": None,
        })
        _config["enabled"] = True
        _config["exempt_local"] = os.environ.get("SBD_RATE_LIMIT_EXEMPT_LOCAL", "1") != "0"
        _config["limits"] = dict(DEFAULT_LIMITS)
