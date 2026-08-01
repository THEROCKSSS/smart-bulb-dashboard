"""Network resilience (Week 2 Phase D; roadmap section 11, W2-196..210):
host-IP change detection, automatic reconnection after the host loses and
regains connectivity, per-bulb latency history over time, and an honest
LAN-vs-Tailscale reachability picture that the rest of the backend can
make decisions from.

The three real failure modes this exists for, all of which actually happen
on a home network:
  1. The router reboots. Every bulb keeps its IP but every persistent
     tinytuya socket this process holds is now dead, and a LAN scan
     launched mid-reboot just burns ~18s for nothing.
  2. DHCP moves the *host*. Port-forward rules, DuckDNS records and the
     user's bookmark all still point at the old address, and the only
     evidence is that "it stopped working" -- so the IP change gets logged.
  3. The LAN is down but Tailscale is up (or the reverse). The dashboard is
     still reachable, but bulb control cannot possibly work; saying so
     plainly beats every bulb call timing out with its own opaque error.

Every network fact this module reports comes from an injectable probe, so
the tests never depend on the machine they run on actually having (or not
having) a network. Nothing here makes an outbound *internet* request --
`primary_ip()` performs no I/O at all, it only asks the routing table.
"""

import ipaddress
import json
import os
import socket
import threading
import time
from collections import deque
from datetime import datetime, timezone

import observability

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)
NETWORK_STATE_PATH = os.path.join(DATA_DIR, "network_state.json")

# Tailscale hands out addresses from the CGNAT range. Python's own
# `is_private` says False for it (it's officially "shared address space",
# not private), which would make a tailnet peer look like a public client
# -- exactly the wrong answer for the exposure warnings in
# remote_access_status.py. Classified explicitly here so both modules agree.
TAILSCALE_CGNAT = ipaddress.ip_network("100.64.0.0/10")

# How often the background monitor takes a reading. A minute is frequent
# enough that a user watching Diagnostics after a router reboot sees the
# recovery, and infrequent enough to be free.
DEFAULT_MONITOR_INTERVAL_S = 60

MAX_CHANGE_LOG = 50      # host-IP changes kept
MAX_RECONNECT_LOG = 50   # connectivity regain events kept
LATENCY_SAMPLES = 200    # per-bulb rolling latency window

_lock = threading.Lock()
_latency_lock = threading.Lock()
# In-memory, resets on restart -- the same accepted tradeoff remote_auth
# makes for lockout state. Persisting a sample on every status() call would
# mean a disk write per bulb poll, which is a worse trade than losing the
# window on restart.
_latency = {}  # device_id -> deque of {at, latency_ms, ok}

_monitor_thread = None


def _now_iso(ts=None):
    return datetime.fromtimestamp(ts or time.time(), timezone.utc).isoformat().replace("+00:00", "Z")


def _default_state():
    return {
        "current_ip": None,
        "last_checked": None,
        "down_since": None,
        "changes": [],      # [{at, old_ip, new_ip}]
        "reconnects": [],   # [{at, down_seconds, ip}]
    }


def _load():
    if not os.path.exists(NETWORK_STATE_PATH):
        return _default_state()
    try:
        with open(NETWORK_STATE_PATH, "r") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return _default_state()
    for k, v in _default_state().items():
        data.setdefault(k, v)
    return data


def _save(state):
    try:
        with open(NETWORK_STATE_PATH, "w") as f:
            json.dump(state, f, indent=2)
    except OSError:
        # Losing the change log is not a reason to break the monitor thread
        # (or, via the startup path, the whole app).
        pass


def get_state():
    with _lock:
        return _load()


def reset_state():
    """Test/maintenance helper -- wipes the persisted change log."""
    with _lock:
        _save(_default_state())
    with _latency_lock:
        _latency.clear()


# ------------------------------------------------------- host addresses ---
def primary_ip():
    """The local address the OS would source an outbound packet from -- i.e.
    the one that matters for "what IP is this dashboard on".

    connect() on a *UDP* socket sends nothing; it only asks the routing
    table which local interface would be used, so this is a pure local
    lookup with no traffic and no dependency on the far address being real
    or reachable. Returns None when there is no route at all, which is the
    signal poll() reads as "this host is offline".
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(0.5)
        sock.connect(("203.0.113.1", 9))  # TEST-NET-3, reserved for documentation
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def list_host_ips():
    """Every IPv4 address this host answers to. Used to tell "LAN up" from
    "only Tailscale up" -- a machine with just a 100.x address has a
    working tailnet and no usable LAN."""
    addresses = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addresses.add(info[4][0])
    except OSError:
        pass
    current = primary_ip()
    if current:
        addresses.add(current)
    return sorted(addresses)


def classify_ip(ip):
    """One of: loopback, link_local, tailscale, private, public, unknown."""
    try:
        parsed = ipaddress.ip_address(ip)
    except (ValueError, TypeError):
        return "unknown"
    if parsed.is_loopback:
        return "loopback"
    if parsed.is_link_local:
        return "link_local"
    if parsed.version == 4 and parsed in TAILSCALE_CGNAT:
        return "tailscale"
    if parsed.is_private:
        return "private"
    if parsed.is_global:
        return "public"
    return "unknown"


def connectivity_summary(ips=None):
    """What is actually reachable *from* this host, as a mode plus a
    plain-language message. Deliberately derived from local interface state
    only -- no pings, no outbound requests -- so it is instant, free, and
    safe to call on every health-page load.

    Modes:
      full           LAN and tailnet both present
      lan_only       LAN present, no tailnet -- remote access is down
      tailscale_only tailnet present, no LAN -- bulb control cannot work
      offline        neither -- nothing can be controlled
    """
    ips = list_host_ips() if ips is None else list(ips)
    classes = {ip: classify_ip(ip) for ip in ips}
    lan = any(c == "private" for c in classes.values())
    tailnet = any(c == "tailscale" for c in classes.values())

    if lan and tailnet:
        mode, message = "full", "LAN and Tailscale are both up."
    elif lan:
        mode, message = "lan_only", (
            "LAN is up; no Tailscale address on this host. Local control works, "
            "tailnet remote access does not.")
    elif tailnet:
        mode, message = "tailscale_only", (
            "Tailscale is up but this host has no LAN address. The dashboard is "
            "reachable over the tailnet, but it cannot reach any bulb -- bulb "
            "commands will fail until the LAN comes back.")
    else:
        mode, message = "offline", (
            "No LAN and no Tailscale address on this host. Nothing can be "
            "controlled until networking comes back.")

    return {
        "data_source": "LIVE DATA",
        "mode": mode,
        "message": message,
        "lan": lan,
        "tailscale": tailnet,
        # The one thing the rest of the backend actually branches on: is it
        # even worth attempting to talk to a bulb / run a LAN scan?
        "bulb_control_available": lan,
        "host_ips": [{"ip": ip, "class": cls} for ip, cls in sorted(classes.items())],
    }


# ------------------------------------------------------- the monitor tick -
def poll(ip_probe=None, reconnect_hook=None, now=None):
    """One reading of the host's own network state.

    Detects two transitions and records both:
      - the host IP changed (DHCP moved us, or a new router handed out a
        different subnet) -- logged so "remote access stopped working
        yesterday" has an answer;
      - connectivity was lost and has come back -- which triggers the
        reconnect hook, because every persistent bulb socket this process
        is holding died with the old link and will otherwise keep failing
        until something drops them.

    `ip_probe` and `reconnect_hook` are injectable so this is testable
    without a real network and without real bulbs.
    """
    now = now or time.time()
    ip_probe = ip_probe or primary_ip
    try:
        ip = ip_probe()
    except Exception:
        ip = None

    logger = observability.get_logger()
    result = {"ip": ip, "at": _now_iso(now), "ip_changed": False,
              "connectivity": "up" if ip else "down", "regained": False, "reconnected": False}

    with _lock:
        state = _load()
        previous_ip = state.get("current_ip")
        was_down = state.get("down_since") is not None

        if ip is None:
            if not was_down:
                state["down_since"] = now
                logger.warning("network connectivity lost -- no route from this host")
            result["down_seconds"] = round(now - (state.get("down_since") or now), 1)
        else:
            if was_down:
                down_seconds = round(now - state["down_since"], 1)
                state["down_since"] = None
                state["reconnects"] = ([{"at": _now_iso(now), "down_seconds": down_seconds, "ip": ip}]
                                       + state.get("reconnects", []))[:MAX_RECONNECT_LOG]
                result["regained"] = True
                result["down_seconds"] = down_seconds
                logger.warning("network connectivity regained after %.1fs -- dropping stale bulb connections",
                               down_seconds)

            if previous_ip and previous_ip != ip:
                state["changes"] = ([{"at": _now_iso(now), "old_ip": previous_ip, "new_ip": ip}]
                                    + state.get("changes", []))[:MAX_CHANGE_LOG]
                result["ip_changed"] = True
                result["previous_ip"] = previous_ip
                logger.warning("host IP changed: %s -> %s (port forwards / DuckDNS records "
                               "pointing at the old address will be stale)", previous_ip, ip)
            state["current_ip"] = ip

        state["last_checked"] = _now_iso(now)
        _save(state)

    # The hook runs OUTSIDE the state lock: it drops live bulb sockets and
    # has no business being serialized behind a JSON file write.
    if result["regained"] or result["ip_changed"]:
        hook = reconnect_hook if reconnect_hook is not None else _default_reconnect_hook
        try:
            hook()
            result["reconnected"] = True
        except Exception as e:
            logger.error("reconnect hook failed after a network transition: %s", e)
    return result


def _default_reconnect_hook():
    """Drop every cached tinytuya connection. Imported lazily so this
    module stays importable (and testable) without the bulb layer."""
    import bulb_manager as bm
    bm.reset_all_connections()


def start_monitor(interval_s=DEFAULT_MONITOR_INTERVAL_S):
    """Background thread that polls the host's network state. Returns the
    thread so a caller can join it in a test; idempotent, so a double
    startup (uvicorn --reload) doesn't stack two monitors."""
    global _monitor_thread
    if _monitor_thread is not None and _monitor_thread.is_alive():
        return _monitor_thread

    def loop():
        while True:
            try:
                poll()
            except Exception:
                # A monitor that dies on one bad reading is worse than one
                # that misses a reading; the next tick tries again.
                pass
            time.sleep(interval_s)

    _monitor_thread = threading.Thread(target=loop, daemon=True, name="network-monitor")
    _monitor_thread.start()
    return _monitor_thread


# --------------------------------------------------- per-bulb latency -----
def record_latency(device_id, latency_ms, ok=True, at=None):
    """Record one round-trip observation for a bulb. Called from the code
    paths that already pay for the round trip (status(), test_connection())
    so building the history costs no extra network traffic."""
    at = at or time.time()
    with _latency_lock:
        samples = _latency.get(device_id)
        if samples is None:
            samples = deque(maxlen=LATENCY_SAMPLES)
            _latency[device_id] = samples
        samples.append({"at": _now_iso(at), "ts": at, "latency_ms": round(float(latency_ms), 1), "ok": bool(ok)})


def latency_history(device_id, limit=None):
    """Latency over time for one bulb, newest first, plus the summary stats
    that answer the question Diagnostics is actually being asked: is this
    bulb getting slower, and how often is it failing outright?"""
    with _latency_lock:
        samples = list(_latency.get(device_id, ()))
    ok_values = sorted(s["latency_ms"] for s in samples if s["ok"])
    failures = sum(1 for s in samples if not s["ok"])
    newest_first = list(reversed(samples))
    if limit:
        newest_first = newest_first[:int(limit)]
    return {
        "data_source": "LIVE DATA",
        "device_id": device_id,
        "window": LATENCY_SAMPLES,
        "sample_count": len(samples),
        "failure_count": failures,
        "failure_rate": round(failures / len(samples), 4) if samples else 0.0,
        "min_ms": ok_values[0] if ok_values else None,
        "max_ms": ok_values[-1] if ok_values else None,
        "avg_ms": round(sum(ok_values) / len(ok_values), 1) if ok_values else None,
        "p50_ms": observability._percentile(ok_values, 0.50),
        "p95_ms": observability._percentile(ok_values, 0.95),
        "samples": newest_first,
        "note": "In-memory rolling window; resets when the backend restarts.",
    }


def all_latency_summaries():
    """Every tracked bulb's latency stats without the raw sample lists --
    the shape the system-health page wants."""
    with _latency_lock:
        device_ids = list(_latency)
    return [
        {k: v for k, v in latency_history(device_id).items() if k != "samples"}
        for device_id in device_ids
    ]


# ------------------------------------------------------ firewall guidance -
# Surfaced in the UI next to the network status, and mirrored in
# docs/remote-access-security.md. Kept as data (not prose baked into the
# frontend) so both places can't drift apart.
LAN_ONLY_PORTS = [
    {"port": 8500, "protocol": "TCP", "direction": "inbound",
     "needed": "Only from your own LAN -- this is the dashboard itself.",
     "safe_to_close_externally": True},
    {"port": 6668, "protocol": "TCP", "direction": "outbound",
     "needed": "Backend -> bulb. Tuya's local control port.",
     "safe_to_close_externally": True},
    {"port": 6666, "protocol": "UDP", "direction": "inbound (LAN broadcast)",
     "needed": "Tuya device discovery broadcasts (protocol 3.1). Only needed for network auto-discovery.",
     "safe_to_close_externally": True},
    {"port": 6667, "protocol": "UDP", "direction": "inbound (LAN broadcast)",
     "needed": "Tuya device discovery broadcasts (protocol 3.3). Only needed for network auto-discovery.",
     "safe_to_close_externally": True},
]
