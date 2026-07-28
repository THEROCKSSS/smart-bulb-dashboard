import json
import os
import threading
import time
from datetime import datetime, timezone

import config as cfgmod

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)
DISCOVERY_PATH = os.path.join(DATA_DIR, "discovery.json")

_lock = threading.Lock()
_scan_lock = threading.Lock()  # prevents overlapping scans (manual + scheduled)
DEFAULT_INTERVAL_HOURS = 168  # weekly


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


def scan_now(maxretry=2):
    """Runs a discovery scan, reconciles results against config.json
    (existing devices), the ignored list, and the discovered list. Returns a
    summary dict. Safe to call concurrently — a scan already in progress
    causes the caller to get {"already_scanning": True} instead of stacking
    two scans on the same network."""
    if not _scan_lock.acquire(blocking=False):
        return {"already_scanning": True}
    try:
        started = time.time()
        try:
            raw = _raw_scan(maxretry=maxretry)
        except Exception as e:
            return {"ok": False, "error": str(e)}

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
            "duration_seconds": round(time.time() - started, 2),
            "timestamp": now,
        }
    finally:
        _scan_lock.release()


def start_scheduler():
    """Background thread that runs scan_now() on the configured interval.
    Checks every 5 minutes whether enough time has passed since last_scan,
    rather than sleeping for the full interval, so a changed interval_hours
    setting (or a manual scan resetting last_scan) takes effect promptly."""

    def loop():
        while True:
            try:
                state = get_state()
                interval_seconds = state["interval_hours"] * 3600
                last_scan = state["last_scan"]
                due = True
                if last_scan:
                    last_dt = datetime.fromisoformat(last_scan.replace("Z", "+00:00"))
                    elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds()
                    due = elapsed >= interval_seconds
                if due:
                    scan_now()
            except Exception:
                pass
            time.sleep(300)  # check every 5 minutes; actual scan cadence controlled by interval_hours

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t
