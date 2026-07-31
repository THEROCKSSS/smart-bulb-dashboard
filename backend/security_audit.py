"""Dedicated security-events log (Week 2, W2-141..160).

Deliberately *separate* from two things that already exist:

  - `bulb_manager.BulbController._log()` / `.history()` — the general action
    history (every colour change, every effect start). That's an operational
    record, it's in-memory, it's per-device, and it's noisy by design. A
    security review shouldn't have to sift a thousand hue changes to find a
    lockout.
  - `remote_auth.AUDIT_LOG_PATH` (`data/auth_audit.log`) — the auth-only
    audit trail. That file stays exactly as it is; this module does not
    replace it. Instead `remote_auth.log_audit_event()` *forwards* every
    line it writes here too, so the security log is a superset that also
    carries non-auth security events (device added, config changed, backup
    restored) with severity, alerting and tamper-evidence attached.

Everything here is stdlib-only and has no project imports, so any module
(`config`, `remote_auth`, `backup_restore`) can import it without a cycle.

## Tamper-evidence, honestly scoped

Each line carries `prev` (the previous line's `hmac`) and its own `hmac`,
keyed by a random key in `data/security_audit_key` that is never written to
any log line or API response. A separate `data/security_audit_state.json`
records the last sequence number and hmac. Together that detects:

  - editing a line in place        -> hmac mismatch (needs the key to forge)
  - deleting a line from the middle -> prev/seq mismatch at the next line
  - deleting lines from the END     -> state file disagrees with the file
  - deleting the whole file         -> state says N entries, file has none

What it does NOT defend against: an attacker with write access to *both*
the key file and the state file can rebuild a consistent forged chain. That
is the inherent limit of keeping the anchor on the same host. The honest
upgrade path is shipping lines off-box (syslog/webhook) — the alert webhook
below is the hook for that, and is why alerts carry the event payload.
"""

import csv
import io
import json
import hmac as hmac_mod
import hashlib
import os
import secrets
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime, timezone

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)

EVENTS_LOG_PATH = os.path.join(DATA_DIR, "security_events.log")
STATE_PATH = os.path.join(DATA_DIR, "security_audit_state.json")
KEY_PATH = os.path.join(DATA_DIR, "security_audit_key")
CONFIG_PATH = os.path.join(DATA_DIR, "security_audit_config.json")
ALERTS_PATH = os.path.join(DATA_DIR, "security_alerts.json")

# Ordered weakest -> strongest. Rank comparisons everywhere go through
# `_rank()` so adding a level later is a one-line change.
SEVERITIES = ("info", "notice", "warning", "critical")

GENESIS_PREV = "0" * 64

# Default severity per event type. Tuned for W2-156 (alert fatigue): with
# the default `alert_min_severity` of "warning", nothing on this list that
# happens during ordinary daily use raises an alert. A successful login is
# info. A single mistyped PIN is notice -- one typo is not an incident, and
# a *run* of them is caught by the login_failure threshold rule instead.
# Only things that are genuinely worth waking up for sit at warning+.
DEFAULT_SEVERITIES = {
    # -- routine, expected during normal use --
    "login_success": "info",
    "logout": "info",
    "config_changed": "info",
    "backup_created": "info",
    "audit_log_rotated": "info",
    "audit_self_test": "info",
    # -- worth a look in review, not worth an alert on its own --
    "login_failure": "notice",
    "session_revoked": "notice",
    "device_removed": "notice",
    "remote_auth_enabled": "notice",
    "backup_deleted": "notice",
    # -- actionable --
    "login_lockout": "warning",
    "login_rate_limited": "warning",
    "session_revoke_all": "warning",
    "device_added": "warning",
    "backup_restored": "warning",
    "backup_restore_rejected": "warning",
    "login_failure_threshold": "warning",
    # -- drop everything and look --
    "remote_auth_disabled": "critical",
    "audit_tamper_detected": "critical",
}

_DEFAULT_CONFIG = {
    # What gets written to the log at all. "info" = write everything; raise
    # it only if log volume is genuinely a problem, since anything filtered
    # out here is gone for good.
    "min_severity": "info",
    # What raises an alert. See the noise note above -- "warning" is the
    # deliberate default, not an arbitrary one.
    "alert_min_severity": "warning",
    # event -> severity, overriding DEFAULT_SEVERITIES (W2-150).
    "severity_overrides": {},
    # Rate-based rules: N occurrences of an event within window_s raises one
    # aggregated alert instead of N individual ones (W2-142).
    "alert_thresholds": {"login_failure": {"count": 3, "window_s": 300}},
    # Off by default: an outbound webhook is an external integration and
    # this project's default posture is that nothing leaves the LAN unless
    # the operator asks for it (W2-149).
    "webhook_enabled": False,
    "webhook_url": None,
    # Local-only alerting (a queue this host's own dashboard polls and can
    # surface as a browser notification) needs no external service at all.
    "local_alerts_enabled": True,
    "max_log_bytes": 1_000_000,
    "rotate_keep": 5,
    "retention_days": 90,
    "max_local_alerts": 100,
}

_lock = threading.Lock()

# In-memory only, by design -- same accepted tradeoff as remote_auth's
# _attempts: threshold counting resets on restart, which for a single-user
# local dashboard is fine and avoids a second write per event.
_threshold_hits = {}  # event -> deque[float]


# ------------------------------------------------------------- severity --
def _rank(severity):
    try:
        return SEVERITIES.index(severity)
    except ValueError:
        return 0


def severity_for(event, cfg=None):
    cfg = cfg if cfg is not None else get_config()
    override = cfg.get("severity_overrides", {}).get(event)
    if override in SEVERITIES:
        return override
    return DEFAULT_SEVERITIES.get(event, "info")


def is_actionable(severity):
    """W2-159: the UI needs to tell 'informational' from 'actionable'
    entries. One definition, here, so the dashboard and the digest can't
    drift apart on what counts as which."""
    return _rank(severity) >= _rank("warning")


# ------------------------------------------------------------ settings ---
def _read_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def _write_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def get_config():
    cfg = dict(_DEFAULT_CONFIG)
    stored = _read_json(CONFIG_PATH, {})
    if isinstance(stored, dict):
        cfg.update({k: v for k, v in stored.items() if k in _DEFAULT_CONFIG})
    return cfg


def update_config(**changes):
    """Partial update. Validates every value it accepts -- a bad severity
    name silently persisted here would quietly disable alerting, which is
    exactly the sort of failure a security feature must not have."""
    cfg = get_config()
    for key, value in changes.items():
        if value is None and key not in ("webhook_url",):
            continue
        if key not in _DEFAULT_CONFIG:
            raise ValueError(f"unknown setting '{key}'")
        if key in ("min_severity", "alert_min_severity"):
            if value not in SEVERITIES:
                raise ValueError(f"{key} must be one of {list(SEVERITIES)}")
        elif key == "severity_overrides":
            if not isinstance(value, dict):
                raise ValueError("severity_overrides must be an object")
            for ev, sev in value.items():
                if sev not in SEVERITIES:
                    raise ValueError(f"severity for '{ev}' must be one of {list(SEVERITIES)}")
        elif key == "alert_thresholds":
            if not isinstance(value, dict):
                raise ValueError("alert_thresholds must be an object")
            for ev, rule in value.items():
                if not isinstance(rule, dict) or "count" not in rule or "window_s" not in rule:
                    raise ValueError(f"threshold for '{ev}' needs 'count' and 'window_s'")
                if int(rule["count"]) < 1 or float(rule["window_s"]) <= 0:
                    raise ValueError(f"threshold for '{ev}' must have count >= 1 and window_s > 0")
        elif key in ("max_log_bytes", "rotate_keep", "retention_days", "max_local_alerts"):
            if int(value) < 1:
                raise ValueError(f"{key} must be >= 1")
            value = int(value)
        elif key in ("webhook_enabled", "local_alerts_enabled"):
            value = bool(value)
        elif key == "webhook_url":
            if value is not None:
                if not isinstance(value, str) or not value.startswith(("http://", "https://")):
                    raise ValueError("webhook_url must be an http(s) URL")
        cfg[key] = value
    with _lock:
        _write_json(CONFIG_PATH, cfg)
    return cfg


# ------------------------------------------------------------ hmac chain --
def _get_key():
    """Read (or lazily create) the chain key. Created with 0600 where the
    OS honours it; the value is never returned by any API route or written
    to any log line."""
    if os.path.exists(KEY_PATH):
        with open(KEY_PATH, "r", encoding="utf-8") as f:
            key = f.read().strip()
        if key:
            return key
    key = secrets.token_hex(32)
    fd = os.open(KEY_PATH, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(key)
    return key


def _entry_hmac(entry_without_hmac, key):
    payload = json.dumps(entry_without_hmac, sort_keys=True, separators=(",", ":"))
    return hmac_mod.new(bytes.fromhex(key), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def _get_state():
    state = _read_json(STATE_PATH, {"seq": 0, "last_hmac": GENESIS_PREV})
    if not isinstance(state, dict):
        state = {"seq": 0, "last_hmac": GENESIS_PREV}
    state.setdefault("seq", 0)
    state.setdefault("last_hmac", GENESIS_PREV)
    return state


# ------------------------------------------------------------- appending --
def _append_locked(event, outcome, severity, source, detail):
    """Write one chained line. Caller must hold `_lock`. Returns the entry,
    or None if it was filtered out by min_severity."""
    cfg = get_config()
    if _rank(severity) < _rank(cfg["min_severity"]):
        return None

    key = _get_key()
    state = _get_state()
    now = time.time()
    entry = {
        "seq": state["seq"] + 1,
        "ts": now,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "severity": severity,
        "outcome": outcome,
        "source": source,
        "detail": detail,
        "prev": state["last_hmac"],
    }
    entry["hmac"] = _entry_hmac({k: v for k, v in entry.items() if k != "hmac"}, key)
    with open(EVENTS_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")
    _write_json(STATE_PATH, {"seq": entry["seq"], "last_hmac": entry["hmac"]})
    return entry


def log_event(event, outcome="success", severity=None, source=None, **detail):
    """Record one security event. `detail` is free-form context -- callers
    must never pass a secret (PIN, local_key, session token, signing key)
    here; `_scrub_detail` is a backstop, not a licence to be careless.

    Best-effort by the same rule as remote_auth.log_audit_event(): a
    logging failure must never break the operation being logged, so write
    errors are swallowed. Returns the written entry, or None.
    """
    try:
        cfg = get_config()
        severity = severity if severity in SEVERITIES else severity_for(event, cfg)
        with _lock:
            entry = _append_locked(event, outcome, severity, source, _scrub_detail(detail))
            # Checked after the append so the size test sees the line just
            # written; rotation writes its own in-chain marker line, so the
            # new segment is never empty (see _rotate_locked).
            _rotate_if_needed_locked(cfg)
        if entry is None:
            return None
        # Alerting happens outside the lock: a webhook thread spawn or a
        # local-queue write must not extend the window the log file is held.
        _maybe_alert(entry, cfg)
        return entry
    except (OSError, ValueError, TypeError):
        return None


# Substrings that must never appear as a *key* in an event's detail. This is
# a backstop against a careless call site, not the primary control -- the
# primary control is that no call site passes a secret in the first place,
# which `test_secrets.py` asserts directly.
_SECRET_KEY_HINTS = ("pin", "local_key", "localkey", "secret", "token", "password", "passphrase")


def _scrub_detail(detail):
    out = {}
    for k, v in detail.items():
        if any(hint in k.lower() for hint in _SECRET_KEY_HINTS):
            out[k] = "[redacted]"
        else:
            out[k] = v
    return out


# ------------------------------------------------------------- alerting --
def _maybe_alert(entry, cfg):
    alerts = []
    rule = cfg.get("alert_thresholds", {}).get(entry["event"])
    if rule:
        hits = _threshold_hits.setdefault(entry["event"], deque())
        now = entry["ts"]
        window = float(rule["window_s"])
        hits.append(now)
        while hits and now - hits[0] > window:
            hits.popleft()
        if len(hits) >= int(rule["count"]):
            hits.clear()  # one aggregated alert per burst, not one per event
            alerts.append({
                "kind": "threshold",
                "event": entry["event"],
                "severity": severity_for(entry["event"] + "_threshold", cfg),
                "message": (
                    f"{int(rule['count'])} x {entry['event']} within "
                    f"{int(window)}s"
                ),
                "ts": now,
                "timestamp": entry["timestamp"],
                "seq": entry["seq"],
            })

    if _rank(entry["severity"]) >= _rank(cfg["alert_min_severity"]):
        alerts.append({
            "kind": "event",
            "event": entry["event"],
            "severity": entry["severity"],
            "message": f"{entry['event']} ({entry['outcome']})",
            "ts": entry["ts"],
            "timestamp": entry["timestamp"],
            "seq": entry["seq"],
            "detail": entry["detail"],
        })

    for alert in alerts:
        _raise_alert(alert, cfg)


def _raise_alert(alert, cfg):
    """Never calls log_event() -- an alert about an event must not itself
    become an event, or a threshold rule would feed itself."""
    if cfg.get("local_alerts_enabled", True):
        try:
            with _lock:
                queue = _read_json(ALERTS_PATH, [])
                if not isinstance(queue, list):
                    queue = []
                queue.append(dict(alert, acknowledged=False))
                queue = queue[-int(cfg.get("max_local_alerts", 100)):]
                _write_json(ALERTS_PATH, queue)
        except OSError:
            pass
    if cfg.get("webhook_enabled") and cfg.get("webhook_url"):
        _deliver_webhook(cfg["webhook_url"], alert)


def _deliver_webhook(url, alert):
    """Fire-and-forget on a daemon thread: a slow or dead webhook endpoint
    must never add latency to (or fail) the request that triggered it."""
    def run():
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(alert).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=5).close()
        except (urllib.error.URLError, OSError, ValueError):
            pass

    threading.Thread(target=run, daemon=True).start()


def list_alerts(limit=50, unacknowledged_only=False):
    queue = _read_json(ALERTS_PATH, [])
    if not isinstance(queue, list):
        return []
    if unacknowledged_only:
        queue = [a for a in queue if not a.get("acknowledged")]
    return list(reversed(queue[-limit:]))


def acknowledge_alerts():
    """Mark every queued alert as seen. Returns how many were newly acked
    (so the UI can say something truthful rather than always 'done')."""
    with _lock:
        queue = _read_json(ALERTS_PATH, [])
        if not isinstance(queue, list):
            return 0
        count = 0
        for alert in queue:
            if not alert.get("acknowledged"):
                alert["acknowledged"] = True
                count += 1
        _write_json(ALERTS_PATH, queue)
        return count


# ----------------------------------------------------- rotation/retention --
def _rotated_path(index):
    return f"{EVENTS_LOG_PATH}.{index}"


def _rotate_if_needed_locked(cfg):
    try:
        size = os.path.getsize(EVENTS_LOG_PATH)
    except OSError:
        return False
    if size < int(cfg.get("max_log_bytes", 1_000_000)):
        return False
    _rotate_locked(cfg)
    return True


def _rotate_locked(cfg):
    keep = int(cfg.get("rotate_keep", 5))
    oldest = _rotated_path(keep)
    if os.path.exists(oldest):
        os.remove(oldest)
    for i in range(keep - 1, 0, -1):
        src, dst = _rotated_path(i), _rotated_path(i + 1)
        if os.path.exists(src):
            os.replace(src, dst)
    if os.path.exists(EVENTS_LOG_PATH):
        os.replace(EVENTS_LOG_PATH, _rotated_path(1))
    # The new segment is never left empty: a marker line keeps the hmac
    # chain continuous across the rotation boundary AND keeps the state
    # file's "last entry" pointing into the *current* file, so verify()
    # can still tell "rotated" from "someone truncated the log".
    _append_locked("audit_log_rotated", "success",
                   severity_for("audit_log_rotated", cfg), "security_audit", {})


def rotate_now():
    with _lock:
        _rotate_locked(get_config())
    return True


def apply_retention():
    """Drop rotated segments older than `retention_days`, and any beyond
    `rotate_keep`. Never touches the current segment -- retention must not
    be a way to lose today's events. Returns the removed file names."""
    cfg = get_config()
    keep = int(cfg.get("rotate_keep", 5))
    max_age = float(cfg.get("retention_days", 90)) * 86400
    now = time.time()
    removed = []
    with _lock:
        for i in range(1, keep + 20):
            path = _rotated_path(i)
            if not os.path.exists(path):
                continue
            too_many = i > keep
            try:
                too_old = (now - os.path.getmtime(path)) > max_age
            except OSError:
                too_old = False
            if too_many or too_old:
                try:
                    os.remove(path)
                    removed.append(os.path.basename(path))
                except OSError:
                    pass
    return removed


# ------------------------------------------------------------- reading ----
def _read_file_entries(path):
    if not os.path.exists(path):
        return []
    entries = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                # A corrupt line is itself evidence -- surface it rather
                # than silently skipping, so verify() can flag it.
                entries.append({"_corrupt": True, "_raw": line})
    return entries


def _all_entries(include_rotated=False):
    entries = []
    if include_rotated:
        for i in range(20, 0, -1):  # oldest segment first
            entries.extend(_read_file_entries(_rotated_path(i)))
    entries.extend(_read_file_entries(EVENTS_LOG_PATH))
    return entries


def read_events(limit=100, since=None, until=None, event=None, min_severity=None,
                outcome=None, q=None, include_rotated=False):
    """Search/filter backing the dashboard's audit view (W2-155). All
    filters are AND-ed; `q` is a case-insensitive substring match over the
    whole serialized entry, which is what makes 'find everything about
    10.0.0.5' work without a per-field query language."""
    if min_severity is not None and min_severity not in SEVERITIES:
        raise ValueError(f"min_severity must be one of {list(SEVERITIES)}")
    entries = [e for e in _all_entries(include_rotated) if not e.get("_corrupt")]
    out = []
    for e in entries:
        if since is not None and e.get("ts", 0) < float(since):
            continue
        if until is not None and e.get("ts", 0) > float(until):
            continue
        if event and e.get("event") != event:
            continue
        if outcome and e.get("outcome") != outcome:
            continue
        if min_severity and _rank(e.get("severity", "info")) < _rank(min_severity):
            continue
        if q and q.lower() not in json.dumps(e, sort_keys=True).lower():
            continue
        out.append(e)
    out.reverse()  # newest first, matching every other list in this app
    if limit:
        out = out[: int(limit)]
    for e in out:
        e["actionable"] = is_actionable(e.get("severity", "info"))
    return out


EXPORT_COLUMNS = ["seq", "timestamp", "ts", "event", "severity", "outcome", "source", "detail"]


def export_events(fmt="json", **filters):
    """Exportable audit log for external review (W2-144). Returns
    (content_str, media_type, suggested_filename). The `hmac`/`prev` chain
    fields are included in the JSON export deliberately -- an export that
    drops them can't be independently verified, which defeats the point."""
    filters.setdefault("limit", 0)  # export defaults to everything matched
    entries = read_events(**filters)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=EXPORT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for e in entries:
            row = dict(e)
            row["detail"] = json.dumps(e.get("detail", {}), sort_keys=True)
            writer.writerow(row)
        return buf.getvalue(), "text/csv", f"security-events-{stamp}.csv"
    if fmt != "json":
        raise ValueError("format must be 'json' or 'csv'")
    return (json.dumps(entries, indent=2, sort_keys=True), "application/json",
            f"security-events-{stamp}.json")


# ---------------------------------------------------------- verification --
def verify_chain(include_rotated=True):
    """Recompute the hmac chain and compare its head against the state
    file. `complete` is False (with `ok` still True) when retention has
    pruned older segments -- that's expected housekeeping, not tampering,
    and conflating the two would train the operator to ignore the result.
    """
    key = _get_key()
    entries = _all_entries(include_rotated)
    state = _get_state()

    result = {
        "ok": True,
        "complete": True,
        "entries": len(entries),
        "first_bad_seq": None,
        "reason": None,
        "expected_seq": state["seq"],
    }

    if not entries:
        if state["seq"] > 0:
            result.update(ok=False, reason="log is empty but state records "
                                           f"{state['seq']} entries (file deleted or truncated)")
        return result

    prev = None
    for e in entries:
        if e.get("_corrupt"):
            result.update(ok=False, reason="unparseable line in log")
            return result
        stored = e.get("hmac")
        recomputed = _entry_hmac({k: v for k, v in e.items()
                                  if k not in ("hmac", "actionable")}, key)
        if stored != recomputed:
            result.update(ok=False, first_bad_seq=e.get("seq"),
                          reason=f"hmac mismatch at seq {e.get('seq')} (entry altered)")
            return result
        if prev is not None:
            if e.get("prev") != prev["hmac"]:
                result.update(ok=False, first_bad_seq=e.get("seq"),
                              reason=f"broken link at seq {e.get('seq')} (entry removed)")
                return result
            if e.get("seq") != prev.get("seq", 0) + 1:
                result.update(ok=False, first_bad_seq=e.get("seq"),
                              reason=f"sequence gap before seq {e.get('seq')}")
                return result
        prev = e

    first_seq = entries[0].get("seq")
    if include_rotated and first_seq != 1:
        result["complete"] = False
        result["reason"] = (f"verified from seq {first_seq}; earlier segments "
                            "are gone (retention/rotation), so history before "
                            "that cannot be checked")
    elif not include_rotated and first_seq != 1:
        result["complete"] = False
        result["reason"] = f"verified current segment only, from seq {first_seq}"

    last = entries[-1]
    if last.get("hmac") != state["last_hmac"] or last.get("seq") != state["seq"]:
        result.update(ok=False, first_bad_seq=last.get("seq"),
                      reason=(f"log head (seq {last.get('seq')}) disagrees with recorded "
                              f"state (seq {state['seq']}) -- entries removed from the end"))
    return result


def self_test():
    """Canary (W2-160): writes one benign event, then re-verifies the chain
    and reports whether alerting is actually wired to anything. Something
    that says 'alerting is on' while pointing at a dead webhook is worse
    than saying nothing, so the delivery config is reported explicitly."""
    cfg = get_config()
    entry = log_event("audit_self_test", "success", source="security_audit",
                      note="scheduled canary")
    verification = verify_chain()
    return {
        "ok": bool(entry) and verification["ok"],
        "wrote_event": bool(entry),
        "verification": verification,
        "alerting": {
            "local_alerts_enabled": cfg["local_alerts_enabled"],
            "webhook_enabled": cfg["webhook_enabled"],
            "webhook_configured": bool(cfg["webhook_url"]),
            "alert_min_severity": cfg["alert_min_severity"],
        },
    }


def digest(days=7):
    """Periodic summary (W2-153) -- deliberately reports even when nothing
    happened, so a silent digest is proof the pipeline works rather than
    ambiguous between 'quiet' and 'broken'."""
    since = time.time() - float(days) * 86400
    entries = read_events(limit=0, since=since, include_rotated=True)
    by_severity = {s: 0 for s in SEVERITIES}
    by_event = {}
    for e in entries:
        by_severity[e.get("severity", "info")] = by_severity.get(e.get("severity", "info"), 0) + 1
        by_event[e["event"]] = by_event.get(e["event"], 0) + 1
    actionable = [e for e in entries if e.get("actionable")]
    return {
        "days": days,
        "total_events": len(entries),
        "by_severity": by_severity,
        "by_event": dict(sorted(by_event.items(), key=lambda kv: -kv[1])),
        "actionable_count": len(actionable),
        "most_recent_actionable": actionable[0] if actionable else None,
        "verification": verify_chain(),
        "data_source": "LIVE DATA (this host's security event log)",
    }
