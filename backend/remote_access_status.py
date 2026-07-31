"""Remote-access surfacing (Week 2 Phase D; roadmap sections 1 and 2):
the currently-detected public IP and last DuckDNS sync, whether Tailscale
is actually running on this host and at what tailnet URL, and the
exposure-vs-PIN-gate warnings the dashboard shows as a banner.

**Nothing in this module runs on a timer.** The public-IP lookup is the
only outbound internet request anywhere in this codebase, and it happens
only when a user explicitly clicks for it in Settings -- never at startup,
never from a background thread, never as a side effect of loading a page.
SECURITY.md promises this project does not phone home; that promise is
only worth something if the code genuinely has no background caller, so
please keep it that way. `status()` defaults to cached values for exactly
this reason.

The Tailscale check shells out to the `tailscale` CLI. Both it and the
public-IP fetch take an injectable runner/fetcher so the tests never
depend on this machine having Tailscale installed or an internet
connection.
"""

import ipaddress
import json
import os
import subprocess
import threading
import time
from datetime import datetime, timezone

import network_health
import observability

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)
REMOTE_ACCESS_PATH = os.path.join(DATA_DIR, "remote_access.json")

# A plain-text "what is my IP" endpoint that returns the address and
# nothing else -- no JSON, no cookies, no account. Overridable via env for
# anyone who would rather point at their own.
PUBLIC_IP_SERVICE = os.environ.get("SBD_PUBLIC_IP_SERVICE", "https://api.ipify.org")
PUBLIC_IP_TIMEOUT_S = 5.0

TAILSCALE_CLI_TIMEOUT_S = 5.0
DEFAULT_DASHBOARD_PORT = int(os.environ.get("SBD_PORT", "8500"))

_lock = threading.Lock()


def _now_iso(ts=None):
    return datetime.fromtimestamp(ts or time.time(), timezone.utc).isoformat().replace("+00:00", "Z")


def _default_state():
    return {
        # Last *explicitly requested* public-IP lookup.
        "public_ip": None,
        "public_ip_checked_at": None,
        "public_ip_error": None,
        # Reported by whatever updates the DuckDNS record (cron job, the
        # provider's container, a manual run) via the duckdns-sync endpoint.
        # This project does not run a DuckDNS updater itself -- see
        # docs/remote-access-security.md.
        "duckdns_domain": None,
        "duckdns_last_sync_at": None,
        "duckdns_last_sync_ip": None,
        "duckdns_last_sync_ok": None,
        "duckdns_last_sync_detail": None,
        # "Public exposure is configured" -- sticky on purpose. Once this is
        # true it stays true until explicitly cleared, so turning the PIN
        # gate off later re-raises the warning instead of quietly leaving an
        # exposed dashboard unauthenticated. That fail-safe is the whole
        # point (roadmap item W2-011).
        "exposure_configured": False,
        "exposure_source": None,
        "exposure_marked_at": None,
        # Evidence rather than configuration: a request actually arrived
        # from a globally-routable address, so the dashboard demonstrably IS
        # reachable from the public internet.
        "public_client_seen_at": None,
        "public_client_ip": None,
    }


def _load():
    if not os.path.exists(REMOTE_ACCESS_PATH):
        return _default_state()
    try:
        with open(REMOTE_ACCESS_PATH, "r") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return _default_state()
    for k, v in _default_state().items():
        data.setdefault(k, v)
    return data


def _save(state):
    try:
        with open(REMOTE_ACCESS_PATH, "w") as f:
            json.dump(state, f, indent=2)
    except OSError:
        pass


def get_state():
    with _lock:
        return _load()


def reset_state():
    """Test/maintenance helper."""
    with _lock:
        _save(_default_state())


# ------------------------------------------------------------ public IP ---
def _default_fetcher(url, timeout_s):
    import urllib.request
    with urllib.request.urlopen(url, timeout=timeout_s) as response:  # noqa: S310 - fixed https URL
        return response.read().decode("utf-8", "replace").strip()


def detect_public_ip(fetcher=None, timeout_s=PUBLIC_IP_TIMEOUT_S):
    """Ask an external service what this network's public IP looks like.

    This is the ONE outbound request this project makes, and only ever on
    an explicit user action. It sends no identifying information beyond the
    bare HTTP request itself. If it fails (offline, service down, blocked),
    that's recorded as an error and surfaced -- it must never take down
    anything else, since remote-access status is a convenience readout, not
    a dependency.
    """
    fetcher = fetcher or _default_fetcher
    logger = observability.get_logger()
    now = time.time()
    ip = None
    error = None
    try:
        raw = (fetcher(PUBLIC_IP_SERVICE, timeout_s) or "").strip()
        ipaddress.ip_address(raw)  # reject an HTML error page pretending to be an answer
        ip = raw
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        logger.warning("public IP lookup failed: %s", error)

    with _lock:
        state = _load()
        state["public_ip"] = ip
        state["public_ip_checked_at"] = _now_iso(now)
        state["public_ip_error"] = error
        _save(state)
    return {"public_ip": ip, "checked_at": _now_iso(now), "error": error, "source": PUBLIC_IP_SERVICE}


# -------------------------------------------------------------- DuckDNS ---
def record_duckdns_sync(domain, ip=None, ok=True, detail=None, at=None):
    """Record that something successfully (or unsuccessfully) refreshed the
    DuckDNS record. This project deliberately does not run its own DuckDNS
    updater -- the provider's cron/container already does that job well --
    so the "last sync" the Settings page shows comes from whatever updater
    the user runs POSTing here. A successful sync is also treated as proof
    that public exposure is configured, which arms the fail-safe warning.
    """
    at = at or time.time()
    with _lock:
        state = _load()
        state["duckdns_domain"] = domain
        state["duckdns_last_sync_at"] = _now_iso(at)
        state["duckdns_last_sync_ip"] = ip
        state["duckdns_last_sync_ok"] = bool(ok)
        state["duckdns_last_sync_detail"] = detail
        if ok and not state["exposure_configured"]:
            state["exposure_configured"] = True
            state["exposure_source"] = f"duckdns:{domain}"
            state["exposure_marked_at"] = _now_iso(at)
        _save(state)
        return dict(state)


def mark_exposure(configured, source=None, at=None):
    """Explicitly declare (or retract) that this dashboard is exposed
    beyond the LAN. Retracting is a deliberate user decision -- the warning
    is designed not to be dismissible by accident, because the failure it
    guards against is an unauthenticated dashboard on the open internet."""
    at = at or time.time()
    with _lock:
        state = _load()
        state["exposure_configured"] = bool(configured)
        state["exposure_source"] = source if configured else None
        state["exposure_marked_at"] = _now_iso(at) if configured else None
        if not configured:
            # Clearing the declared exposure also clears the observed-public
            # -client evidence; otherwise there'd be no way to ever silence a
            # warning caused by one visit from a public address.
            state["public_client_seen_at"] = None
            state["public_client_ip"] = None
        _save(state)
        return dict(state)


def note_client_ip(ip):
    """Record that a request arrived from `ip`. Only globally-routable
    sources are persisted, and only the first one -- the point isn't an
    access log (the audit log already exists), it's a single durable bit
    saying "this dashboard has demonstrably been reached from the public
    internet". Tailnet (100.64/10) and LAN sources are ignored."""
    if network_health.classify_ip(ip) != "public":
        return None
    with _lock:
        state = _load()
        if state["public_client_seen_at"]:
            return dict(state)
        state["public_client_seen_at"] = _now_iso()
        state["public_client_ip"] = ip
        _save(state)
    observability.get_logger().warning(
        "request received from a public (internet-routable) address %s -- "
        "this dashboard is reachable from outside the LAN", ip)
    return get_state()


# ------------------------------------------------------------ Tailscale ---
def _default_runner(args, timeout_s):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout_s)


def tailscale_status(runner=None, timeout_s=TAILSCALE_CLI_TIMEOUT_S, port=None):
    """Is Tailscale actually running on this host, and what URL does that
    make the dashboard reachable at?

    Three distinguishable outcomes, because they need three different
    fixes: the CLI isn't installed at all; it's installed but the daemon is
    stopped/logged out; it's up and we can name the tailnet URL. Anything
    unexpected degrades to "unknown" with the error attached rather than
    raising -- Diagnostics must still render on a host with no Tailscale.
    """
    runner = runner or _default_runner
    port = port or DEFAULT_DASHBOARD_PORT
    result = {
        "data_source": "LIVE DATA",
        "installed": False,
        "running": False,
        "backend_state": None,
        "magic_dns_name": None,
        "tailscale_ips": [],
        "tailnet_url": None,
        "peer_count": None,
        "error": None,
    }
    try:
        completed = runner(["tailscale", "status", "--json"], timeout_s)
    except FileNotFoundError:
        result["error"] = "tailscale CLI not found on PATH -- Tailscale is not installed on this host"
        return result
    except subprocess.TimeoutExpired:
        result["installed"] = True
        result["error"] = f"tailscale status timed out after {timeout_s}s"
        return result
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        return result

    result["installed"] = True
    stdout = (getattr(completed, "stdout", "") or "").strip()
    if getattr(completed, "returncode", 1) != 0 or not stdout:
        stderr = (getattr(completed, "stderr", "") or "").strip()
        result["error"] = stderr or f"tailscale status exited {getattr(completed, 'returncode', '?')}"
        return result

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as e:
        result["error"] = f"could not parse tailscale status output: {e}"
        return result

    backend_state = data.get("BackendState")
    result["backend_state"] = backend_state
    result["running"] = backend_state == "Running"
    self_node = data.get("Self") or {}
    # MagicDNS names come back fully qualified with a trailing dot.
    dns_name = (self_node.get("DNSName") or "").rstrip(".")
    result["magic_dns_name"] = dns_name or None
    result["tailscale_ips"] = list(self_node.get("TailscaleIPs") or [])
    result["peer_count"] = len(data.get("Peer") or {})
    host = dns_name or (result["tailscale_ips"][0] if result["tailscale_ips"] else None)
    if host and result["running"]:
        result["tailnet_url"] = f"http://{host}:{port}"
    if not result["running"] and not result["error"]:
        result["error"] = (f"Tailscale is installed but its backend state is "
                           f"'{backend_state}' -- run `tailscale up` to connect this host")
    return result


# ------------------------------------------------------------- warnings ---
def exposure_warnings(state=None, pin_gate_enabled=None):
    """The banner. Each warning is a stable id plus a severity, so the
    frontend can style and the tests can assert on something that isn't a
    prose string.

    The two distinct conditions here are deliberately not merged:
      - `public_client_observed` is *evidence* -- someone really did reach
        this dashboard from an internet-routable address.
      - `exposure_configured_gate_disabled` is the *fail-safe* -- exposure
        was configured at some point and the PIN gate is off now. It stays
        up across restarts until the gate goes back on or the user
        explicitly retracts the exposure declaration, which is exactly the
        "gate later gets disabled" case (roadmap W2-011) that a
        dismiss-once banner would fail to catch.
    """
    state = state if state is not None else get_state()
    if pin_gate_enabled is None:
        try:
            import remote_auth
            pin_gate_enabled = remote_auth.is_enabled()
        except Exception:
            pin_gate_enabled = False

    warnings = []
    if state.get("public_client_seen_at") and not pin_gate_enabled:
        warnings.append({
            "id": "public_client_observed",
            "severity": "critical",
            "title": "This dashboard has been reached from a public IP with no PIN gate",
            "detail": (
                f"A request arrived from {state.get('public_client_ip')} at "
                f"{state.get('public_client_seen_at')}, which is an internet-routable "
                "address -- so this dashboard is reachable from outside your LAN while "
                "the PIN gate is disabled. Anyone who finds it can control your bulbs "
                "and read your device inventory. Enable the PIN gate in Settings, or "
                "remove the port forward."),
            "action": "Enable the PIN gate (Settings -> Remote Access)",
        })
    if state.get("exposure_configured") and not pin_gate_enabled:
        warnings.append({
            "id": "exposure_configured_gate_disabled",
            "severity": "critical",
            "title": "Public exposure is configured but the PIN gate is off",
            "detail": (
                f"Public exposure was recorded ({state.get('exposure_source') or 'unknown source'}, "
                f"{state.get('exposure_marked_at')}) and the PIN gate is currently disabled. "
                "This warning is deliberately persistent: it will keep showing until the "
                "PIN gate is re-enabled, or you explicitly retract the exposure "
                "declaration after taking the port forward down."),
            "action": "Re-enable the PIN gate, or retract exposure once the port forward is removed",
        })
    if state.get("exposure_configured") and state.get("duckdns_last_sync_ok") is False:
        warnings.append({
            "id": "duckdns_sync_failing",
            "severity": "warning",
            "title": "The last DuckDNS sync reported a failure",
            "detail": (
                f"Last sync at {state.get('duckdns_last_sync_at')} for "
                f"{state.get('duckdns_domain')} failed: {state.get('duckdns_last_sync_detail')}. "
                "The hostname may now point at a stale IP, which usually shows up as "
                "'remote access just stopped working'."),
            "action": "Check the DuckDNS updater on this host",
        })
    return warnings


def status(include_live_lookups=False, runner=None, port=None, pin_gate_enabled=None):
    """Everything the Settings/Diagnostics remote-access panels need.

    `include_live_lookups` gates the Tailscale subprocess call only. The
    public IP is ALWAYS served from cache here -- see this module's
    docstring: the outbound lookup happens on its own explicit endpoint and
    nowhere else.
    """
    state = get_state()
    tailscale = (tailscale_status(runner=runner, port=port) if include_live_lookups
                 else {"data_source": "CACHED", "checked": False,
                       "note": "Tailscale status is checked on demand from Diagnostics."})
    return {
        "data_source": "LIVE DATA",
        "public_ip": {
            "ip": state["public_ip"],
            "checked_at": state["public_ip_checked_at"],
            "error": state["public_ip_error"],
            "source": PUBLIC_IP_SERVICE,
            "note": ("Detected only when you ask for it -- this is the only outbound "
                     "request this project makes. See SECURITY.md."),
        },
        "duckdns": {
            "domain": state["duckdns_domain"],
            "last_sync_at": state["duckdns_last_sync_at"],
            "last_sync_ip": state["duckdns_last_sync_ip"],
            "last_sync_ok": state["duckdns_last_sync_ok"],
            "last_sync_detail": state["duckdns_last_sync_detail"],
            "note": ("Reported by your own DuckDNS updater via "
                     "POST /api/system/remote-access/duckdns-sync -- this project does "
                     "not run an updater itself."),
        },
        "exposure": {
            "configured": state["exposure_configured"],
            "source": state["exposure_source"],
            "marked_at": state["exposure_marked_at"],
            "public_client_seen_at": state["public_client_seen_at"],
            "public_client_ip": state["public_client_ip"],
        },
        "tailscale": tailscale,
        "warnings": exposure_warnings(state, pin_gate_enabled=pin_gate_enabled),
    }
