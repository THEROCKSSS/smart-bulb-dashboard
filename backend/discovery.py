import json
import os
import threading
import time
from datetime import datetime, timezone

import config as cfgmod
import network_health
import observability

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)
DISCOVERY_PATH = os.path.join(DATA_DIR, "discovery.json")

_lock = threading.Lock()
_scan_lock = threading.Lock()  # prevents overlapping scans (manual + scheduled)
DEFAULT_INTERVAL_HOURS = 168  # weekly

# Router-reboot resilience (roadmap W2-206). A scan launched while the
# router is still coming back up fails outright -- the UDP broadcast has
# nowhere to go. One immediate failure is not evidence that there are no
# devices, so the scan is retried a couple of times with a widening gap
# before giving up. Kept small: a full scan already takes ~18s, and the
# scheduled scan will come round again anyway.
DEFAULT_SCAN_RETRIES = 2
DEFAULT_SCAN_RETRY_DELAY_S = 5.0


def _now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _default_state():
    return {
        "last_scan": None,
        "interval_hours": DEFAULT_INTERVAL_HOURS,
        "discovered": [],  # devices seen on the LAN not in config.json and not ignored
        "ignored": [],  # device_ids explicitly dismissed by the user
    }


def _load():
    if not os.path.exists(DISCOVERY_PATH):
        return _default_state()
    with open(DISCOVERY_PATH, "r") as f:
        data = json.load(f)
    # backfill any keys added after a state file already existed
    for k, v in _default_state().items():
        data.setdefault(k, v)
    return data


def _save(state):
    with open(DISCOVERY_PATH, "w") as f:
        json.dump(state, f, indent=2)


def get_state():
    with _lock:
        return _load()


def set_interval_hours(hours):
    with _lock:
        state = _load()
        state["interval_hours"] = max(1, int(hours))
        _save(state)
        return state


def ignore_device(device_id):
    with _lock:
        state = _load()
        state["discovered"] = [d for d in state["discovered"] if d["device_id"] != device_id]
        if device_id not in state["ignored"]:
            state["ignored"].append(device_id)
        _save(state)
        return state


def unignore_device(device_id):
    with _lock:
        state = _load()
        state["ignored"] = [d for d in state["ignored"] if d != device_id]
        _save(state)
        return state


def forget_discovered(device_id):
    """Remove a device from the discovered list without ignoring it — it can
    reappear on the next scan (unlike ignore, which is permanent until
    manually undone)."""
    with _lock:
        state = _load()
        state["discovered"] = [d for d in state["discovered"] if d["device_id"] != device_id]
        _save(state)
        return state


def _raw_scan(maxretry=2):
    """Runs the actual UDP broadcast discovery. Isolated into its own
    function so tests/callers can see exactly what tinytuya returned before
    any dedup/classification logic runs."""
    import tinytuya
    return tinytuya.deviceScan(verbose=False, maxretry=maxretry)


def _scan_with_retry(maxretry, retries, retry_delay_s, sleeper):
    """Run the raw scan, retrying a failure with a widening delay. Returns
    (raw, error, attempts). `sleeper` is injected so the retry path is
    testable without actually waiting."""
    logger = observability.get_logger()
    last_error = None
    for attempt in range(1, retries + 2):
        try:
            return _raw_scan(maxretry=maxretry), None, attempt
        except Exception as e:
            last_error = str(e)
            if attempt <= retries:
                delay = retry_delay_s * attempt
                logger.warning("discovery scan attempt %d failed (%s) -- retrying in %.1fs; "
                               "this is the expected shape of a scan launched during a router reboot",
                               attempt, last_error, delay)
                sleeper(delay)
    return None, last_error, retries + 1


def scan_now(maxretry=2, retries=DEFAULT_SCAN_RETRIES,
             retry_delay_s=DEFAULT_SCAN_RETRY_DELAY_S, sleeper=time.sleep):
    """Runs a discovery scan, reconciles results against config.json
    (existing devices), the ignored list, and the discovered list. Returns a
    summary dict. Safe to call concurrently — a scan already in progress
    causes the caller to get {"already_scanning": True} instead of stacking
    two scans on the same network.

    A failed scan is retried (see DEFAULT_SCAN_RETRIES) so that a scan
    which happened to land during a router restart doesn't get recorded as
    "no devices on this network"."""
    if not _scan_lock.acquire(blocking=False):
        return {"already_scanning": True}
    try:
        started = time.time()
        raw, error, attempts = _scan_with_retry(maxretry, retries, retry_delay_s, sleeper)
        if error is not None:
            return {"ok": False, "error": error, "attempts": attempts}

        cfg = cfgmod.load_config()
        known_by_id = {d["device_id"]: d for d in cfg["devices"]}

        new_count = 0
        ip_updates = []
        now = _now_iso()

        with _lock:
            state = _load()
            ignored = set(state["ignored"])
            discovered_by_id = {d["device_id"]: d for d in state["discovered"]}

            for dev_id, info in raw.items():
                ip = info.get("ip")
                version = info.get("version")
                name = info.get("name") or info.get("product_name") or ""

                existing = known_by_id.get(dev_id)
                if existing:
                    # Already configured — only worth acting on if its IP moved
                    # (e.g. a DHCP lease renewal), never treat as "new".
                    old_ip = existing.get("ip")
                    if ip and old_ip != ip:
                        existing["ip"] = ip
                        cfgmod.upsert_device(existing)
                        ip_updates.append({"device_id": dev_id, "old_ip": old_ip, "new_ip": ip})
                    continue

                if dev_id in ignored:
                    continue

                entry = discovered_by_id.get(dev_id)
                if entry:
                    entry["ip"] = ip or entry.get("ip")
                    entry["version"] = version or entry.get("version")
                    entry["last_seen"] = now
                else:
                    discovered_by_id[dev_id] = {
                        "device_id": dev_id,
                        "ip": ip,
                        "version": version,
                        "name": name,
                        "first_seen": now,
                        "last_seen": now,
                    }
                    new_count += 1

            state["discovered"] = list(discovered_by_id.values())
            state["last_scan"] = now
            _save(state)

        return {
            "ok": True,
            "scanned_count": len(raw),
            "new_count": new_count,
            "ip_updates": ip_updates,
            "attempts": attempts,
            "duration_seconds": round(time.time() - started, 2),
            "timestamp": now,
        }
    finally:
        _scan_lock.release()


def scan_due(state, now=None):
    """Whether the configured interval has elapsed since the last scan.
    Split out of the scheduler loop so the "should we scan" decision is
    testable without running the thread."""
    last_scan = state.get("last_scan")
    if not last_scan:
        return True
    try:
        last_dt = datetime.fromisoformat(last_scan.replace("Z", "+00:00"))
    except ValueError:
        return True
    now = now or datetime.now(timezone.utc)
    return (now - last_dt).total_seconds() >= state["interval_hours"] * 3600


def should_scan(state, connectivity, network_poll=None, now=None):
    """The scheduler's full decision: scan, or don't, and why.

    Two additions over "has the interval elapsed":
      - Skip entirely when the host has no LAN address. A UDP broadcast
        scan with no LAN cannot find anything; running it anyway burns ~18s
        and, worse, overwrites `last_scan` so the *next* real opportunity
        is pushed a whole interval away.
      - Scan immediately when the host's own IP just changed, regardless of
        the interval. A new IP usually means a new router or a new subnet,
        so every cached bulb IP is suspect and the whole point of
        discovery is to find them again (roadmap W2-206).
    """
    if not connectivity.get("bulb_control_available"):
        return {"scan": False, "reason": "no_lan", "detail": connectivity.get("message")}
    if network_poll and network_poll.get("ip_changed"):
        return {"scan": True, "reason": "host_ip_changed",
                "detail": f"{network_poll.get('previous_ip')} -> {network_poll.get('ip')}"}
    if scan_due(state, now=now):
        return {"scan": True, "reason": "interval_elapsed", "detail": None}
    return {"scan": False, "reason": "not_due", "detail": None}


def start_scheduler():
    """Background thread that runs scan_now() on the configured interval.
    Checks every 5 minutes whether enough time has passed since last_scan,
    rather than sleeping for the full interval, so a changed interval_hours
    setting (or a manual scan resetting last_scan) takes effect promptly.

    The same tick also polls the host's own network state, so a router
    reboot or a DHCP-driven IP change gets noticed (and triggers a fresh
    scan) without a second timer thread. The scan/skip decision itself
    lives in should_scan() so it can be tested without the thread."""

    def loop():
        while True:
            try:
                network_poll = network_health.poll()
                decision = should_scan(get_state(), network_health.connectivity_summary(),
                                       network_poll=network_poll)
                if decision["scan"]:
                    scan_now()
                elif decision["reason"] == "no_lan":
                    observability.get_logger().info(
                        "skipping scheduled discovery scan: %s", decision["detail"])
            except Exception:
                pass
            time.sleep(300)  # check every 5 minutes; actual scan cadence controlled by interval_hours

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t
