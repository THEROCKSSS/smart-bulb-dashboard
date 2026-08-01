"""Backend observability (Week 2 Phase D; roadmap section 13, W2-226..240):
request metrics + latency percentiles + error rates, a Prometheus text
endpoint, per-request correlation IDs, a level-configurable in-memory log
buffer the dashboard can tail, startup dependency health checks, and a
secrets-redacted self-diagnostic report.

Everything counted here lives in memory only, on purpose. This is a
single-process dashboard for one household; a real time-series store would
be more machinery than the problem deserves. The cost is that metrics and
buffered logs reset on restart -- the same accepted tradeoff `remote_auth`
already makes for lockout state. `/metrics` exists precisely so anyone who
*does* want durable history can point a real Prometheus at this rather
than have this project grow its own storage layer.

This module deliberately imports nothing from the rest of the backend at
module scope (config/bulb_manager are imported lazily inside the functions
that need them). The dependency check below has to be able to report "the
audio stack is broken" while still being importable *in* a process whose
audio stack is broken.
"""

import contextvars
import json
import logging
import math
import os
import re
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)
SETTINGS_PATH = os.path.join(DATA_DIR, "observability.json")

START_TIME = time.time()
STARTED_AT_ISO = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

# Prometheus metric-name prefix. Short, project-specific, and stable --
# renaming it later silently breaks anyone's existing dashboards/alerts.
METRIC_PREFIX = "sbd"

# Per-endpoint latency is kept as a rolling window of the most recent
# samples rather than every sample ever seen: percentiles over "the last
# few hundred requests" is what actually answers "is it slow *now*", and it
# bounds memory per endpoint to a constant no matter how long the process
# has been up.
LATENCY_WINDOW = 512


# ------------------------------------------------------------- redaction --
REDACTED = "[REDACTED]"

# Field names whose *value* is a secret wherever it appears -- in a JSON
# body, a config dict, a log line's `key=value` fragment. Matched
# case-insensitively against the key, not the value.
SECRET_FIELD_NAMES = frozenset({
    "local_key", "localkey", "key",
    "pin", "new_pin", "old_pin", "pin_hash", "salt", "secret_key", "secret",
    "token", "session_token", "access_token", "sbd_session",
    "password", "passwd", "api_key", "apikey", "authorization", "cookie",
})

# `local_key: abc123`, `pin="1234"`, `secret_key = deadbeef` -- the shapes a
# secret actually takes when it lands in a free-form log line or traceback.
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(" + "|".join(sorted(SECRET_FIELD_NAMES, key=len, reverse=True)) + r")"
    r"(\"?\s*[:=]\s*\"?)"
    r"([^\s\"',;}\]]+)"
)

# Never scrub anything shorter than this even when it's a known live secret
# value. A 4-character PIN like "1234" also occurs as a perfectly innocent
# substring (a port, a timestamp fragment, a device id chunk); blanket-
# replacing it would corrupt the report while adding nothing -- the
# assignment-shaped regex above already catches the case that matters
# (`pin=1234`). Long values (local_key, secret_key, pin_hash) are unique
# enough that a bare occurrence really is a leak, and those DO get scrubbed.
MIN_BARE_SECRET_LEN = 12


def redact_text(text, extra_secrets=()):
    """Scrub secrets out of a free-form string. Two passes, deliberately:
    the assignment-shaped regex catches `local_key=...` even for a value
    this process has never seen, and `extra_secrets` catches a *bare*
    occurrence of a value we know is live (the actual local_key printed on
    its own by some third-party traceback). Neither alone is sufficient."""
    if not isinstance(text, str) or not text:
        return text
    out = _SECRET_ASSIGNMENT_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
    for secret in extra_secrets or ():
        if isinstance(secret, str) and len(secret) >= MIN_BARE_SECRET_LEN and secret in out:
            out = out.replace(secret, REDACTED)
    return out


def redact_obj(obj, extra_secrets=()):
    """Recursively redact a JSON-ish structure. A key matching
    SECRET_FIELD_NAMES has its whole value replaced (regardless of type, so
    a list of keys doesn't slip through); every other string still goes
    through redact_text so a secret embedded in prose is caught too."""
    if isinstance(obj, dict):
        result = {}
        for k, v in obj.items():
            if isinstance(k, str) and k.lower() in SECRET_FIELD_NAMES:
                result[k] = REDACTED
            else:
                result[k] = redact_obj(v, extra_secrets)
        return result
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v, extra_secrets) for v in obj]
    if isinstance(obj, str):
        return redact_text(obj, extra_secrets)
    return obj


def live_secrets():
    """The actual secret *values* this install currently holds, for the
    bare-occurrence pass of redact_text(). Read defensively: a diagnostic
    report is exactly the thing you generate when config is broken, so a
    missing/corrupt config.json must degrade to "fewer known secrets", never
    to an exception."""
    secrets = set()
    try:
        import config as cfgmod
        cfg = cfgmod.load_config()
        for device in cfg.get("devices", []):
            value = device.get("local_key")
            if value:
                secrets.add(str(value))
    except Exception:
        pass
    try:
        import remote_auth
        state = remote_auth._load()
        for field in ("secret_key", "pin_hash", "salt"):
            value = state.get(field)
            if value:
                secrets.add(str(value))
    except Exception:
        pass
    return secrets


# ------------------------------------------------------ correlation IDs ---
CORRELATION_HEADER = "X-Correlation-ID"
_correlation_id = contextvars.ContextVar("sbd_correlation_id", default=None)

# A caller-supplied correlation id is honored so a request traced from a
# reverse proxy or a CLI keeps one id end to end -- but only if it looks
# like an id. Without this shape check an attacker controls text that gets
# written verbatim into every log line for that request (newline injection
# = forged log entries), which is a real log-injection primitive, not a
# theoretical one.
_CORRELATION_SAFE_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


def new_correlation_id():
    return uuid.uuid4().hex[:16]


def adopt_correlation_id(incoming=None):
    """Use `incoming` if it's a plausible id, otherwise mint a fresh one.
    Returns the id actually adopted."""
    cid = incoming if incoming and _CORRELATION_SAFE_RE.match(incoming) else new_correlation_id()
    _correlation_id.set(cid)
    return cid


def get_correlation_id():
    return _correlation_id.get()


def set_correlation_id(cid):
    _correlation_id.set(cid)


# ------------------------------------------------------------- logging ----
LOGGER_NAME = "sbd"
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
DEFAULT_LOG_LEVEL = "INFO"
LOG_BUFFER_SIZE = 500

_log_buffer = deque(maxlen=LOG_BUFFER_SIZE)
_log_buffer_lock = threading.Lock()
_settings_lock = threading.Lock()
_logging_configured = False


class _RingBufferHandler(logging.Handler):
    """Keeps the most recent records in memory so the dashboard can tail
    them without shelling into the host or parsing a file. Messages are
    pattern-redacted on the way *in* as well as on the way out (see
    recent_logs) -- store-time redaction means a secret-shaped log line
    never sits in the buffer at all, read-time redaction catches bare
    values that only the live config can identify."""

    def emit(self, record):
        try:
            message = redact_text(record.getMessage())
        except Exception:  # a broken __str__ on a log arg must not kill the app
            message = "<unformattable log record>"
        entry = {
            "ts": record.created,
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc)
            .isoformat().replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": message,
            "correlation_id": getattr(record, "correlation_id", None),
        }
        if record.exc_info:
            entry["exception"] = redact_text(self.format(record))
        with _log_buffer_lock:
            _log_buffer.append(entry)

        # Push to any open live-log view. Imported lazily and inside a
        # try/except because this runs on the logging path: a failure here
        # must never turn a log line into an exception, and observability
        # must not gain a hard import of the stream hub just to feed it.
        try:
            import live_stream
            live_stream.publish("log", entry)
        except Exception:
            pass


class _CorrelationFilter(logging.Filter):
    """Stamps whatever correlation id is in scope onto every record, so a
    log line emitted three call frames deep inside bulb_manager still ties
    back to the HTTP request that caused it."""

    def filter(self, record):
        record.correlation_id = get_correlation_id()
        return True


def _default_settings():
    return {"log_level": DEFAULT_LOG_LEVEL}


def _load_settings():
    if not os.path.exists(SETTINGS_PATH):
        return _default_settings()
    try:
        with open(SETTINGS_PATH, "r") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return _default_settings()
    for k, v in _default_settings().items():
        data.setdefault(k, v)
    if data["log_level"] not in LOG_LEVELS:
        data["log_level"] = DEFAULT_LOG_LEVEL
    return data


def _save_settings(data):
    with open(SETTINGS_PATH, "w") as f:
        json.dump(data, f, indent=2)


def get_logger():
    """The one logger the backend logs through. Configured once, lazily --
    importing this module must not have the side effect of reconfiguring
    logging for a host process that embeds it."""
    global _logging_configured
    logger = logging.getLogger(LOGGER_NAME)
    if not _logging_configured:
        with _settings_lock:
            if not _logging_configured:
                logger.handlers.clear()
                logger.addFilter(_CorrelationFilter())
                stream = logging.StreamHandler()
                stream.setFormatter(logging.Formatter(
                    "%(asctime)s %(levelname)s [%(correlation_id)s] %(name)s: %(message)s"))
                logger.addHandler(stream)
                logger.addHandler(_RingBufferHandler())
                # Own the whole chain: propagating to the root logger would
                # double-print under uvicorn (which installs its own root
                # handlers) and, worse, would let a root handler write an
                # un-redacted copy of every line somewhere we don't control.
                logger.propagate = False
                logger.setLevel(getattr(logging, _load_settings()["log_level"]))
                _logging_configured = True
    return logger


def get_log_level():
    return _load_settings()["log_level"]


def set_log_level(level):
    """Change the backend log level at runtime (Settings -> Logging), no
    code edit and no restart. Persisted so it survives a restart too."""
    level = (level or "").upper()
    if level not in LOG_LEVELS:
        raise ValueError(f"unknown log level '{level}', expected one of {list(LOG_LEVELS)}")
    with _settings_lock:
        settings = _load_settings()
        settings["log_level"] = level
        _save_settings(settings)
    get_logger().setLevel(getattr(logging, level))
    return {"log_level": level}


def recent_logs(limit=100, level=None):
    """Most recent buffered log entries, newest first. `level` filters to
    that severity *and above* (the usual meaning), not that severity only."""
    min_level = None
    if level:
        level = level.upper()
        if level not in LOG_LEVELS:
            raise ValueError(f"unknown log level '{level}', expected one of {list(LOG_LEVELS)}")
        min_level = getattr(logging, level)
    with _log_buffer_lock:
        entries = list(_log_buffer)
    if min_level is not None:
        entries = [e for e in entries if getattr(logging, e["level"], 0) >= min_level]
    entries.reverse()
    entries = entries[:max(0, int(limit))]
    secrets = live_secrets()
    return [redact_obj(e, secrets) for e in entries]


def clear_log_buffer():
    """Test/maintenance helper -- the buffer is process-global state."""
    with _log_buffer_lock:
        _log_buffer.clear()


# -------------------------------------------------------------- metrics ---
_metrics_lock = threading.Lock()
_endpoints = {}   # (method, template) -> counters + rolling latency window
_totals = {"requests": 0, "errors": 0, "client_errors": 0}
_status_classes = {}  # "2xx" -> count, process-wide


def _endpoint_record(method, template):
    key = (method, template)
    record = _endpoints.get(key)
    if record is None:
        record = {
            "method": method,
            "endpoint": template,
            "requests": 0,
            "errors": 0,
            "client_errors": 0,
            "latencies": deque(maxlen=LATENCY_WINDOW),
            "latency_sum_s": 0.0,
        }
        _endpoints[key] = record
    return record


def record_request(method, template, status_code, duration_s):
    """One handled HTTP request. `template` must be the *route template*
    (`/api/devices/{device_id}/power`), never the concrete path -- one
    series per real bulb id would make the metric useless for percentiles
    and unbounded in cardinality."""
    status_class = f"{int(status_code) // 100}xx"
    is_server_error = 500 <= int(status_code) < 600
    is_client_error = 400 <= int(status_code) < 500
    with _metrics_lock:
        record = _endpoint_record(method, template)
        record["requests"] += 1
        record["latencies"].append(float(duration_s))
        record["latency_sum_s"] += float(duration_s)
        if is_server_error:
            record["errors"] += 1
            _totals["errors"] += 1
        if is_client_error:
            record["client_errors"] += 1
            _totals["client_errors"] += 1
        _totals["requests"] += 1
        _status_classes[status_class] = _status_classes.get(status_class, 0) + 1


def reset_metrics():
    """Test/maintenance helper -- metric state is process-global."""
    with _metrics_lock:
        _endpoints.clear()
        _status_classes.clear()
        _totals.update({"requests": 0, "errors": 0, "client_errors": 0})


def _percentile(sorted_values, q):
    """Nearest-rank percentile: the smallest value at or above which q of
    the samples fall. Written out longhand rather than reaching for numpy
    on purpose -- this module has to keep working (and reporting) inside a
    process where numpy itself is the thing that's broken."""
    if not sorted_values:
        return None
    index = min(len(sorted_values) - 1, max(0, math.ceil(q * len(sorted_values)) - 1))
    return sorted_values[index]


def _endpoint_summary(record):
    latencies = sorted(record["latencies"])
    requests = record["requests"]
    return {
        "method": record["method"],
        "endpoint": record["endpoint"],
        "requests": requests,
        "errors": record["errors"],
        "client_errors": record["client_errors"],
        "error_rate": round(record["errors"] / requests, 4) if requests else 0.0,
        "latency_samples": len(latencies),
        "p50_ms": round(_percentile(latencies, 0.50) * 1000, 2) if latencies else None,
        "p95_ms": round(_percentile(latencies, 0.95) * 1000, 2) if latencies else None,
        "p99_ms": round(_percentile(latencies, 0.99) * 1000, 2) if latencies else None,
        "max_ms": round(max(latencies) * 1000, 2) if latencies else None,
        "avg_ms": round((record["latency_sum_s"] / requests) * 1000, 2) if requests else None,
    }


def uptime_seconds():
    return round(time.time() - START_TIME, 1)


def metrics_snapshot():
    """JSON view of everything /metrics exposes, for the dashboard's own
    health page (which shouldn't have to parse Prometheus text)."""
    with _metrics_lock:
        endpoints = [_endpoint_summary(r) for r in _endpoints.values()]
        totals = dict(_totals)
        status_classes = dict(_status_classes)
    endpoints.sort(key=lambda e: e["requests"], reverse=True)
    requests = totals["requests"]
    return {
        "data_source": "LIVE DATA",
        "uptime_seconds": uptime_seconds(),
        "started_at": STARTED_AT_ISO,
        "totals": {
            **totals,
            "error_rate": round(totals["errors"] / requests, 4) if requests else 0.0,
            "client_error_rate": round(totals["client_errors"] / requests, 4) if requests else 0.0,
        },
        "status_classes": status_classes,
        "latency_window": LATENCY_WINDOW,
        "endpoints": endpoints,
    }


def _escape_label(value):
    """Prometheus label-value escaping: backslash, double quote, newline.
    Endpoint templates contain `{device_id}` braces but never a quote, so
    this is mostly belt-and-braces -- except for the `unmatched` fallback
    path below, which can carry arbitrary client-supplied text."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def prometheus_text(version=None):
    """The /metrics body, in Prometheus text exposition format 0.0.4.

    Latency is exposed as a `summary` with pre-computed quantiles rather
    than a `histogram`: the quantiles are what a self-hoster actually looks
    at here, and a histogram would need bucket boundaries chosen up front
    for a workload (LAN bulb calls) whose latency spans three orders of
    magnitude. The documented cost is that summary quantiles can't be
    re-aggregated across instances -- fine for a single-process dashboard.
    """
    snapshot = metrics_snapshot()
    lines = []

    def metric(name, help_text, metric_type, samples):
        lines.append(f"# HELP {METRIC_PREFIX}_{name} {help_text}")
        lines.append(f"# TYPE {METRIC_PREFIX}_{name} {metric_type}")
        for labels, value in samples:
            label_str = ("{" + ",".join(f'{k}="{_escape_label(v)}"' for k, v in labels) + "}") if labels else ""
            lines.append(f"{METRIC_PREFIX}_{name}{label_str} {value}")

    if version:
        metric("build_info", "Backend build information (always 1; read the labels).",
               "gauge", [((("version", version),), 1)])
    metric("uptime_seconds", "Backend process uptime in seconds.", "gauge",
           [((), snapshot["uptime_seconds"])])
    metric("start_time_seconds", "Backend process start time as a unix timestamp.", "gauge",
           [((), round(START_TIME, 3))])
    metric("requests_total", "HTTP requests handled, by method and endpoint template.", "counter",
           [((("method", e["method"]), ("endpoint", e["endpoint"])), e["requests"])
            for e in snapshot["endpoints"]])
    metric("request_errors_total", "HTTP requests that returned a 5xx, by method and endpoint.", "counter",
           [((("method", e["method"]), ("endpoint", e["endpoint"])), e["errors"])
            for e in snapshot["endpoints"]])
    metric("request_client_errors_total", "HTTP requests that returned a 4xx, by method and endpoint.", "counter",
           [((("method", e["method"]), ("endpoint", e["endpoint"])), e["client_errors"])
            for e in snapshot["endpoints"]])
    metric("responses_total", "HTTP responses by status class.", "counter",
           [((("status", status_class),), count)
            for status_class, count in sorted(snapshot["status_classes"].items())])

    latency_samples = []
    for e in snapshot["endpoints"]:
        labels = (("method", e["method"]), ("endpoint", e["endpoint"]))
        for quantile, key in (("0.5", "p50_ms"), ("0.95", "p95_ms"), ("0.99", "p99_ms")):
            if e[key] is not None:
                latency_samples.append((labels + (("quantile", quantile),), round(e[key] / 1000, 6)))
    lines.append(f"# HELP {METRIC_PREFIX}_request_latency_seconds "
                 f"Request latency quantiles over the last {LATENCY_WINDOW} requests per endpoint.")
    lines.append(f"# TYPE {METRIC_PREFIX}_request_latency_seconds summary")
    for labels, value in latency_samples:
        label_str = "{" + ",".join(f'{k}="{_escape_label(v)}"' for k, v in labels) + "}"
        lines.append(f"{METRIC_PREFIX}_request_latency_seconds{label_str} {value}")
    for e in snapshot["endpoints"]:
        label_str = f'{{method="{_escape_label(e["method"])}",endpoint="{_escape_label(e["endpoint"])}"}}'
        total_s = round((e["avg_ms"] or 0) / 1000 * e["requests"], 6)
        lines.append(f"{METRIC_PREFIX}_request_latency_seconds_sum{label_str} {total_s}")
        lines.append(f"{METRIC_PREFIX}_request_latency_seconds_count{label_str} {e['requests']}")

    return "\n".join(lines) + "\n"


# --------------------------------------------------- route-template lookup -
_template_cache = {}
_template_cache_lock = threading.Lock()
# Bounded so a scanner hammering random 404 paths can't grow this map
# without limit -- an unbounded per-path cache in front of a metrics
# recorder is a memory-exhaustion primitive, not a nicety.
TEMPLATE_CACHE_MAX = 2048
UNMATCHED_TEMPLATE = "<unmatched>"


def route_template(routes, method, path):
    """Map a concrete request path onto the route *template* that serves it
    (`/api/devices/bulb-1/power` -> `/api/devices/{device_id}/power`).

    Resolved by matching against the app's own compiled route regexes
    rather than reading `scope["route"]` after the fact: Starlette only
    puts `endpoint`/`path_params` into the scope, and which internals a
    middleware can rely on has changed between versions. Matching the
    router's own regexes is explicit and version-independent.
    """
    cache_key = (method, path)
    with _template_cache_lock:
        cached = _template_cache.get(cache_key)
    if cached is not None:
        return cached

    template = UNMATCHED_TEMPLATE
    for route in routes or ():
        regex = getattr(route, "path_regex", None)
        if regex is None or not regex.match(path):
            continue
        # A path that matches but with the wrong verb is still that
        # endpoint's 405 -- attributing it to <unmatched> would hide a real
        # client bug in the noise. Keep it as a candidate and keep looking
        # for a route that accepts the method too.
        candidate = getattr(route, "path_format", None) or getattr(route, "path", UNMATCHED_TEMPLATE)
        methods = getattr(route, "methods", None)
        if not methods or method in methods:
            template = candidate
            break
        if template == UNMATCHED_TEMPLATE:
            template = candidate

    with _template_cache_lock:
        if len(_template_cache) < TEMPLATE_CACHE_MAX:
            _template_cache[cache_key] = template
    return template


def clear_template_cache():
    with _template_cache_lock:
        _template_cache.clear()


# --------------------------------------------------- dependency health ----
class DependencyError(RuntimeError):
    """A required runtime dependency is missing or broken. Raised at
    startup so the process dies with an actionable message instead of
    surfacing as an opaque failure on the first bulb command."""


def _probe_tinytuya():
    import tinytuya
    missing = [name for name in ("BulbDevice", "deviceScan") if not hasattr(tinytuya, name)]
    if missing:
        raise RuntimeError(f"tinytuya is importable but missing {missing} -- wrong package or a broken install")
    return {"version": getattr(tinytuya, "__version__", "unknown")}


def _probe_numpy():
    import numpy as np
    # Import alone isn't proof: a numpy built against the wrong BLAS/ABI
    # imports fine and then explodes on the first real array op, which for
    # this project would surface as a mid-song audio-reactive crash.
    value = float(np.abs(np.fft.rfft(np.zeros(8, dtype="float32"))).sum())
    if value != 0.0:
        raise RuntimeError("numpy rfft returned an implausible result -- suspect a broken build")
    return {"version": np.__version__}


def _probe_sounddevice():
    import sounddevice as sd
    # query_devices() is the call that actually touches PortAudio. On a
    # headless host with no audio backend the import succeeds and this is
    # where it fails -- which is exactly the distinction worth reporting.
    devices = sd.query_devices()
    inputs = [d for d in devices if d.get("max_input_channels", 0) > 0]
    return {"version": getattr(sd, "__version__", "unknown"),
            "input_device_count": len(inputs)}


# `required=True` means the dashboard genuinely cannot do its core job
# without it. sounddevice is deliberately NOT required: audio-reactive
# lighting is one feature of many, and a headless/audio-less host should
# still get bulb control, scenes, schedules and the API -- degraded, and
# said so plainly, rather than refusing to boot at all.
DEPENDENCY_PROBES = (
    ("tinytuya", True, "local bulb control -- nothing works without it", _probe_tinytuya),
    ("numpy", True, "audio FFT analysis; imported at startup by audio_reactive", _probe_numpy),
    ("sounddevice", False, "audio capture for audio-reactive lighting only", _probe_sounddevice),
)

_dependency_cache = None
_dependency_lock = threading.Lock()


def check_dependencies(force=False):
    """Import each dependency and exercise one real operation on it.
    Cached after the first run: this is a startup-time property of the
    install, and re-probing PortAudio on every health-page load would add
    hundreds of milliseconds for an answer that cannot have changed."""
    global _dependency_cache
    with _dependency_lock:
        if _dependency_cache is not None and not force:
            return [dict(r) for r in _dependency_cache]

    results = []
    for name, required, why, probe in DEPENDENCY_PROBES:
        entry = {"name": name, "required": required, "why": why}
        try:
            entry.update(probe() or {})
            entry["ok"] = True
            entry["detail"] = None
        except ImportError as e:
            entry["ok"] = False
            entry["detail"] = f"not installed ({e})"
        except Exception as e:
            entry["ok"] = False
            entry["detail"] = f"installed but not working ({type(e).__name__}: {e})"
        results.append(entry)

    with _dependency_lock:
        _dependency_cache = [dict(r) for r in results]
    return results


def dependency_summary(force=False):
    checks = check_dependencies(force=force)
    broken_required = [c for c in checks if c["required"] and not c["ok"]]
    degraded = [c for c in checks if not c["required"] and not c["ok"]]
    return {
        "data_source": "LIVE DATA",
        "ok": not broken_required,
        "degraded": [c["name"] for c in degraded],
        "checks": checks,
    }


def dependency_failure_message(broken):
    """The message a user actually needs when startup refuses to proceed:
    what broke, why it matters, and the exact command that fixes it."""
    lines = ["Startup aborted -- required dependencies are missing or broken:"]
    for check in broken:
        lines.append(f"  - {check['name']}: {check['detail']} (needed for: {check['why']})")
    lines.append("")
    lines.append("Fix, from the repo root, using THIS project's own venv (never a shared one):")
    lines.append("  backend/venv/Scripts/python.exe -m pip install -r backend/requirements.txt")
    lines.append("  # Linux/macOS: backend/venv/bin/python -m pip install -r backend/requirements.txt")
    lines.append("See SETUP.md, and AGENTS.md's note on never installing into a shared venv.")
    return "\n".join(lines)


def startup_dependency_check(force=True):
    """Run at startup, before any scheduler thread is spawned. Fails fast
    with a readable message rather than letting a missing tinytuya surface
    minutes later as a mysterious 500 on the first bulb command."""
    logger = get_logger()
    checks = check_dependencies(force=force)
    for check in checks:
        if check["ok"]:
            logger.info("dependency ok: %s %s", check["name"], check.get("version", ""))
        elif check["required"]:
            logger.critical("required dependency broken: %s -- %s", check["name"], check["detail"])
        else:
            logger.warning("optional dependency unavailable: %s -- %s (%s will be unavailable)",
                           check["name"], check["detail"], check["why"])
    broken = [c for c in checks if c["required"] and not c["ok"]]
    if broken:
        raise DependencyError(dependency_failure_message(broken))
    return checks


# ------------------------------------------------- self-diagnostic report -
def _config_summary():
    """Shape and size of the config, never its contents. Device entries go
    through config.redact() *and* the generic redactor below, so the
    local_key is masked twice over by two independent mechanisms."""
    try:
        import config as cfgmod
        cfg = cfgmod.load_config()
    except Exception as e:
        return {"available": False, "error": f"{type(e).__name__}: {e}"}
    devices = cfg.get("devices", [])
    return {
        "available": True,
        "device_count": len(devices),
        "group_count": len(cfg.get("groups", [])),
        "zone_count": len(cfg.get("zones", [])),
        "orchestration_preset_count": len(cfg.get("orchestration_presets", [])),
        "audio_custom_preset_count": len(cfg.get("audio_custom_presets", [])),
        "devices": [
            {
                "id": d.get("id"),
                "name": d.get("name"),
                "ip": d.get("ip"),
                "protocol_version": d.get("version"),
                "has_local_key": bool(d.get("local_key")),
                "local_key_length": len(d.get("local_key") or ""),
            }
            for d in devices
        ],
    }


def _remote_auth_summary():
    """Whether the gate is on and how it's tuned -- never the PIN hash,
    salt, signing key, or any session token."""
    try:
        import remote_auth
        state = remote_auth.status()
        return {
            "available": True,
            "enabled": state["enabled"],
            "session_ttl_s": state["session_ttl_s"],
            "login_rate_limit_max": state["login_rate_limit_max"],
            "login_rate_limit_window_s": state["login_rate_limit_window_s"],
            "active_session_count": len(remote_auth.list_sessions()),
        }
    except Exception as e:
        return {"available": False, "error": f"{type(e).__name__}: {e}"}


def _device_history_summary(history_limit):
    try:
        import bulb_manager as bm
        import config as cfgmod
        cfg = cfgmod.load_config()
    except Exception as e:
        return {"available": False, "error": f"{type(e).__name__}: {e}"}
    result = {}
    for device in cfg.get("devices", []):
        controller = bm.get_controller(device["id"])
        if controller is None:
            continue
        result[device["id"]] = controller.history()[:history_limit]
    return {"available": True, "recent_actions": result}


def diagnostic_report(log_limit=200, history_limit=20, version=None):
    """The "here's what my install looks like" bundle to attach when asking
    for help: config shape, dependency state, metrics, network state and
    recent logs/actions -- with every secret stripped.

    Two things this deliberately does NOT do: make any outbound network
    request (a support bundle must not phone anywhere, see SECURITY.md),
    and write itself to disk (nothing generated here should end up sitting
    in the repo where it could be committed). It is returned to the caller
    and that's it.
    """
    try:
        import network_health
        network = {
            "state": network_health.get_state(),
            "connectivity": network_health.connectivity_summary(),
            "bulb_latency": network_health.all_latency_summaries(),
        }
    except Exception as e:
        network = {"available": False, "error": f"{type(e).__name__}: {e}"}

    try:
        import remote_access_status
        remote_access = remote_access_status.status(include_live_lookups=False)
    except Exception as e:
        remote_access = {"available": False, "error": f"{type(e).__name__}: {e}"}

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "version": version,
        "uptime_seconds": uptime_seconds(),
        "started_at": STARTED_AT_ISO,
        "platform": {
            "python": _python_version(),
            "os": os.name,
        },
        "redaction": (
            "Secrets are stripped: any local_key/PIN/hash/salt/session-secret value, "
            "any field whose name looks like a secret, and any bare occurrence of a "
            "live secret value are replaced with " + REDACTED + ". Skim it before "
            "sharing anyway -- IPs and device names are left intact on purpose."
        ),
        "dependencies": dependency_summary(),
        "metrics": metrics_snapshot(),
        "log_level": get_log_level(),
        "config": _config_summary(),
        "remote_auth": _remote_auth_summary(),
        "network": network,
        "remote_access": remote_access,
        "recent_logs": recent_logs(limit=log_limit),
        "history": _device_history_summary(history_limit),
    }
    # Final gate: the whole bundle goes through the redactor once more, so a
    # secret that reached it through a path nobody anticipated (a nested
    # error string, a third-party traceback) still doesn't get out.
    return redact_obj(report, live_secrets())


def _python_version():
    import sys
    return sys.version.split()[0]
