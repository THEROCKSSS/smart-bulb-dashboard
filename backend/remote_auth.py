import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone

from net_utils import normalize_ip

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)
AUTH_PATH = os.path.join(DATA_DIR, "remote_auth.json")

# Auth-relevant events (login success/failure, lockouts, session
# revocations, rate-limit trips) get appended here as one JSON line each --
# deliberately separate from server.log/general app logging so security
# events can be reviewed/exported on their own. Never written with a PIN or
# raw session token value; see log_audit_event().
AUDIT_LOG_PATH = os.path.join(DATA_DIR, "auth_audit.log")

SESSION_COOKIE = "sbd_session"
DEFAULT_SESSION_TTL_S = 24 * 3600
PBKDF2_ITERATIONS = 200_000

MIN_SESSION_TTL_S = 60
MAX_SESSION_TTL_S = 90 * 24 * 3600

# Lockout policy defaults. These were hardcoded 5-attempts/300s; they're now
# the out-of-the-box values for a per-install setting (env var for a durable
# default, set_lockout_policy() for runtime changes). Kept under the original
# names because they're the documented "what the gate does by default".
MAX_ATTEMPTS = int(os.environ.get("SBD_LOCKOUT_MAX_ATTEMPTS", "5"))
LOCKOUT_SECONDS = int(os.environ.get("SBD_LOCKOUT_BASE_S", "300"))
# Repeat lockouts for the same IP double the wait (300s, 600s, 1200s, ...)
# up to this ceiling. A flat window lets a patient attacker keep spending
# MAX_ATTEMPTS guesses every LOCKOUT_SECONDS forever at a fixed cost;
# doubling makes sustained guessing cost grow without bound, while a single
# fat-fingered household member only ever pays the base window.
LOCKOUT_MAX_SECONDS = int(os.environ.get("SBD_LOCKOUT_MAX_S", str(24 * 3600)))
# How long a quiet IP keeps its escalation level. Without decay an IP that
# tripped one lockout months ago would still start at a doubled penalty.
LOCKOUT_ESCALATION_DECAY_S = int(os.environ.get("SBD_LOCKOUT_DECAY_S", str(24 * 3600)))

# Per-IP login-attempt rate limit -- independent of, and checked before,
# the MAX_ATTEMPTS/LOCKOUT_SECONDS lockout above. The lockout counts wrong
# PINs against one account's worth of guesses; this limits raw request
# volume to the login endpoint itself, which is what actually blunts a
# slow/distributed brute force spreading guesses across many PINs from one
# IP to stay under the lockout's radar. Tunable via env var so operators
# aren't stuck with a hardcoded value; also stored in persisted state so it
# can be adjusted at runtime (see set_login_rate_limit()).
DEFAULT_LOGIN_RATE_LIMIT_MAX = int(os.environ.get("SBD_LOGIN_RATE_LIMIT_MAX", "20"))
DEFAULT_LOGIN_RATE_LIMIT_WINDOW_S = int(os.environ.get("SBD_LOGIN_RATE_LIMIT_WINDOW_S", "60"))

# Paths reachable with no session, even when the PIN gate is enabled --
# the root page and its static assets plus the login/health endpoints, so
# the PIN prompt itself can actually load. Found via testing: forgetting
# "/" here means the browser gets a raw 401 JSON response instead of the
# HTML page that contains the PIN form -- a real chicken-and-egg lockout,
# not a hypothetical one. Every other route is gated once enabled.
OPEN_PATHS = {"/", "/api/auth/login", "/api/auth/status", "/api/system/health"}
OPEN_PREFIXES = ("/static/",)

_lock = threading.Lock()
# In-memory only, by design — attempt tracking resets on restart, which is
# an acceptable tradeoff for a single-user local dashboard. Keyed by the
# NORMALIZED client IP (see net_utils.normalize_ip), so this throttles one
# attacking source, not global request volume.
# ip -> {"count", "locked_until", "lockouts", "last_failure"}
_attempts = {}
# _attempts was previously mutated without a lock. Its read-modify-write
# ("count" increment, then a threshold check) is not atomic, so two
# concurrent wrong-PIN requests could each read the same count and let the
# IP overshoot the threshold. Narrow lock, never held across a file write.
_attempts_lock = threading.Lock()

# Separate lock from _lock (state-file read/modify/write) so a rate-limit
# check never blocks on, or nests inside, a state-file operation. In-memory
# only, same accepted tradeoff as _attempts above.
_rate_limit_lock = threading.Lock()
_rate_limit_buckets = {}  # ip -> {"window_start": float, "count": int}

# Its own lock, separate from _lock, so appending an audit line never
# nests inside a state-file lock/unlock (avoids any lock-ordering deadlock
# risk since audit calls happen from within code that may already hold
# _lock, e.g. session revocation).
_audit_lock = threading.Lock()

# Running counts for the diagnostics API. In-memory/restart-resets, matching
# the tradeoff already accepted for _attempts above -- these are an
# "is something happening right now" signal, not an audit trail (the audit
# log on disk is the durable record).
_auth_counters = {
    "login_success": 0,
    "login_failure": 0,
    "lockouts_triggered": 0,
    "login_rate_limit_blocks": 0,
}
_counters_lock = threading.Lock()


def _bump(counter):
    with _counters_lock:
        _auth_counters[counter] = _auth_counters.get(counter, 0) + 1


def _default_state():
    return {
        "enabled": False,
        # Legacy single-PIN fields. Still written by nothing new -- kept so a
        # state file from before multi-PIN support loads and migrates (see
        # _migrate_pins) instead of silently locking the household out.
        "pin_hash": None,
        "salt": None,
        # id -> {label, kind, salt, pin_hash, created_at, revoked,
        #        revoked_at, expires_at, last_used_at}
        # Exactly one record has kind="household" (the PIN the gate was
        # enabled with); any others are kind="guest" and independently
        # revocable without disturbing the household PIN or its sessions.
        "pins": {},
        "secret_key": secrets.token_hex(32),
        "session_ttl_s": DEFAULT_SESSION_TTL_S,
        "lockout_max_attempts": MAX_ATTEMPTS,
        "lockout_base_s": LOCKOUT_SECONDS,
        "lockout_max_s": LOCKOUT_MAX_SECONDS,
        "pin_changed_at": None,
        # jti -> {created_at, expires_at, revoked, revoked_at, ip, last_seen}
        # Server-side allowlist of issued session tokens, persisted the same
        # way as everything else in this file. A session is valid only if
        # BOTH its HMAC signature checks out AND its jti is present here and
        # not revoked -- this is additive on top of the existing stateless
        # signature check, not a replacement for it.
        "sessions": {},
        "login_rate_limit_max": DEFAULT_LOGIN_RATE_LIMIT_MAX,
        "login_rate_limit_window_s": DEFAULT_LOGIN_RATE_LIMIT_WINDOW_S,
    }


def _migrate_pins(state):
    """Fold a pre-multi-PIN state file's single pin_hash/salt into the
    `pins` map as the household PIN. Without this, upgrading in place would
    load a gate that is still `enabled` but has no PIN that can open it."""
    if state.get("pins"):
        return state
    if state.get("pin_hash") and state.get("salt"):
        state["pins"] = {
            secrets.token_hex(8): {
                "label": "Household",
                "kind": "household",
                "salt": state["salt"],
                "pin_hash": state["pin_hash"],
                "created_at": None,  # unknown; predates this field
                "revoked": False,
                "revoked_at": None,
                "expires_at": None,
                "last_used_at": None,
            }
        }
    return state


def _load():
    if not os.path.exists(AUTH_PATH):
        return _default_state()
    with open(AUTH_PATH, "r") as f:
        data = json.load(f)
    for k, v in _default_state().items():
        data.setdefault(k, v)
    return _migrate_pins(data)


def _save(state):
    with open(AUTH_PATH, "w") as f:
        json.dump(state, f, indent=2)


def is_enabled():
    return _load().get("enabled", False)


def status():
    state = _load()
    return {
        "enabled": state["enabled"],
        "session_ttl_s": state["session_ttl_s"],
        "login_rate_limit_max": state["login_rate_limit_max"],
        "login_rate_limit_window_s": state["login_rate_limit_window_s"],
        "lockout_max_attempts": state["lockout_max_attempts"],
        "lockout_base_s": state["lockout_base_s"],
        "lockout_max_s": state["lockout_max_s"],
        "pin_changed_at": state["pin_changed_at"],
        "guest_pin_count": sum(
            1 for p in state.get("pins", {}).values()
            if p.get("kind") == "guest" and not p.get("revoked")
        ),
    }


def get_session_ttl():
    return _load()["session_ttl_s"]


def set_session_ttl(session_ttl_s):
    """Change how long a newly-issued session stays valid. Existing sessions
    keep the TTL they were issued with -- their expiry is baked into the
    signed token, so shortening the TTL can't retroactively cut them short.
    Pair with revoke_all_sessions() if that's what you actually wanted."""
    ttl = int(session_ttl_s)
    if ttl < MIN_SESSION_TTL_S or ttl > MAX_SESSION_TTL_S:
        raise ValueError(
            f"session_ttl_s must be between {MIN_SESSION_TTL_S} and {MAX_SESSION_TTL_S} seconds"
        )
    with _lock:
        state = _load()
        state["session_ttl_s"] = ttl
        _save(state)
    return ttl


def set_lockout_policy(max_attempts=None, base_seconds=None, max_seconds=None):
    """Tune the wrong-PIN lockout. `base_seconds` is the FIRST lockout's
    length; repeat lockouts for the same IP double it up to `max_seconds`
    (see _lockout_duration)."""
    with _lock:
        state = _load()
        if max_attempts is not None:
            if max_attempts < 1:
                raise ValueError("max_attempts must be >= 1")
            state["lockout_max_attempts"] = int(max_attempts)
        if base_seconds is not None:
            if base_seconds < 1:
                raise ValueError("base_seconds must be >= 1")
            state["lockout_base_s"] = int(base_seconds)
        if max_seconds is not None:
            if max_seconds < 1:
                raise ValueError("max_seconds must be >= 1")
            state["lockout_max_s"] = int(max_seconds)
        if state["lockout_max_s"] < state["lockout_base_s"]:
            raise ValueError("max_seconds must be >= base_seconds")
        _save(state)
    return status()


def log_audit_event(event, outcome, **fields):
    """Append one JSON line to AUDIT_LOG_PATH for an auth-relevant event
    (login success/failure, lockout, session revocation, rate-limit trip).
    `fields` is free-form context (ip, session_id/jti, counts, ...) -- callers
    must never pass a raw PIN or session token value here. Best-effort: a
    logging failure (disk full, permissions) must never break the actual
    auth flow, so write errors are swallowed."""
    entry = {
        "ts": time.time(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "outcome": outcome,
    }
    entry.update(fields)
    line = json.dumps(entry, sort_keys=True)
    try:
        with _audit_lock:
            with open(AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except OSError:
        pass


def _hash_pin(pin, salt):
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), PBKDF2_ITERATIONS).hex()


# ------------------------------------------------------- PIN complexity ---
MIN_PIN_LENGTH = 6

# Every login hashes the candidate against every active PIN at 200k PBKDF2
# iterations each (see verify_pin's no-early-exit comment), so the count of
# configured PINs is directly the CPU cost an unauthenticated request can
# force. Capped rather than unbounded for exactly that reason.
MAX_GUEST_PINS = 5

# PINs an attacker's very first guesses will cover, plus every placeholder
# this project's own development and docs have used. AGENTS.md is explicit
# that `test1234` and friends must never survive into a real deployment, so
# they're rejected outright rather than merely warned about -- a warning
# that can be clicked past is exactly how a dev PIN ships.
BANNED_PINS = {
    "0000", "00000", "000000", "1111", "111111", "1234", "12345", "123456",
    "1234567", "12345678", "123456789", "1234567890", "4321", "54321",
    "654321", "1212", "121212", "2580", "0852", "6969", "7777", "8888",
    "9999", "1004", "2000", "1122", "1313", "2001", "1010",
    "admin", "letmein", "password", "passw0rd", "qwerty", "iloveyou",
    "changeme", "secret", "default", "guest", "test", "test1234", "test123",
    "testpin", "demo", "bulb", "smartbulb", "dashboard",
}

_SEQUENCES = "0123456789abcdefghijklmnopqrstuvwxyz"


def _is_sequential(pin):
    """True for a straight run in either direction ("3456", "dcba"). These
    are as guessable as a repeated digit but wouldn't be caught by a
    banned-list of any practical size."""
    low = pin.lower()
    if len(low) < 3:
        return False
    idx = _SEQUENCES.find(low)
    if idx != -1:
        return True
    return _SEQUENCES[::-1].find(low) != -1


def assess_pin(pin):
    """Grade a candidate PIN. Returns
    {"ok": bool, "strength": "weak"|"fair"|"strong", "issues": [...],
     "hints": [...]}.

    `ok` False means enable()/change_pin()/add_guest_pin() will refuse it --
    there is deliberately no override flag, because the only thing an
    override gets used for is shipping the dev PIN. `hints` are advisory
    only (the UI shows them) and never block."""
    issues = []
    hints = []
    pin = pin or ""

    if len(pin) < MIN_PIN_LENGTH:
        issues.append(f"must be at least {MIN_PIN_LENGTH} characters")
    if pin.lower() in BANNED_PINS:
        issues.append("is a well-known or development/test PIN")
    if pin and len(set(pin)) == 1:
        issues.append("is a single repeated character")
    if _is_sequential(pin):
        issues.append("is a simple ascending/descending sequence")
    # "123123", "abab" -- a short block repeated to pad out the length.
    if pin and re.fullmatch(r"(.{1,3})\1+", pin):
        issues.append("is a short pattern repeated to look longer")

    distinct = len(set(pin))
    if not issues:
        if len(pin) >= 10 and distinct >= 6:
            strength = "strong"
        elif len(pin) >= 8 and distinct >= 4:
            strength = "fair"
        else:
            strength = "fair" if distinct >= 4 else "weak"
    else:
        strength = "weak"

    if len(pin) < 8:
        hints.append("8+ characters resists guessing far better than 6")
    if pin.isdigit():
        hints.append("mixing in letters multiplies the search space")
    if distinct and distinct < 4:
        hints.append("use more distinct characters")

    return {
        "ok": not issues,
        "strength": strength,
        "issues": issues,
        "hints": hints,
    }


def _require_acceptable_pin(pin):
    result = assess_pin(pin)
    if not result["ok"]:
        raise ValueError("PIN rejected: it " + "; it ".join(result["issues"]))


def _new_pin_record(pin, kind, label, expires_at=None):
    salt = secrets.token_hex(16)
    return {
        "label": label,
        "kind": kind,
        "salt": salt,
        "pin_hash": _hash_pin(pin, salt),
        "created_at": time.time(),
        "revoked": False,
        "revoked_at": None,
        "expires_at": expires_at,
        "last_used_at": None,
    }


def _household_entry(state):
    for pin_id, rec in state.get("pins", {}).items():
        if rec.get("kind") == "household":
            return pin_id, rec
    return None, None


def household_pin_id():
    """Id of the household PIN record, or None if the gate was never armed.
    Used to tie a re-issued session (after a PIN change) back to the PIN it
    was authorized by."""
    return _household_entry(_load())[0]


def enable(pin, session_ttl_s=None):
    """(Re-)arm the gate with `pin` as the household PIN. Any guest PINs
    from a previous arming are dropped: enabling is a fresh setup, and
    silently keeping someone else's still-valid guest PIN across it is the
    kind of surprise that gets a dashboard opened to the internet."""
    _require_acceptable_pin(pin)
    ttl = int(session_ttl_s) if session_ttl_s else None
    if ttl is not None and not (MIN_SESSION_TTL_S <= ttl <= MAX_SESSION_TTL_S):
        raise ValueError(
            f"session_ttl_s must be between {MIN_SESSION_TTL_S} and {MAX_SESSION_TTL_S} seconds"
        )
    with _lock:
        state = _load()
        record = _new_pin_record(pin, "household", "Household")
        state["pins"] = {secrets.token_hex(8): record}
        # Legacy mirrors, so downgrading to an older build (or any code path
        # still reading them) sees the current PIN rather than a stale one.
        state["salt"] = record["salt"]
        state["pin_hash"] = record["pin_hash"]
        state["enabled"] = True
        state["pin_changed_at"] = time.time()
        if ttl is not None:
            state["session_ttl_s"] = ttl
        _save(state)


def change_pin(new_pin):
    """Replace the household PIN without disabling the gate. Every existing
    session is revoked and the signing key rotated, so a token issued under
    the old PIN can't outlive it -- the caller is expected to immediately
    issue itself a fresh session (main.py re-sets the cookie) rather than
    being logged out by its own PIN change. Returns the revoked count."""
    _require_acceptable_pin(new_pin)
    with _lock:
        state = _load()
        pin_id, existing = _household_entry(state)
        if not existing:
            raise ValueError("PIN auth is not enabled")
        record = _new_pin_record(new_pin, "household", existing.get("label") or "Household")
        state["pins"][pin_id] = record
        state["salt"] = record["salt"]
        state["pin_hash"] = record["pin_hash"]
        state["pin_changed_at"] = time.time()
        revoked = _revoke_all_locked(state)
        _save(state)
    return revoked


def add_guest_pin(pin, label=None, expires_in_s=None):
    """Issue an additional PIN that opens the same gate but can be revoked
    on its own (revoking it also kills the sessions it created -- see
    revoke_pin). Foundation for Week 3's real multi-user work; deliberately
    NOT a separate permission level yet, so don't describe it as one."""
    _require_acceptable_pin(pin)
    with _lock:
        state = _load()
        if not _household_entry(state)[1]:
            raise ValueError("PIN auth is not enabled")
        live_guests = sum(
            1 for r in state["pins"].values()
            if r.get("kind") == "guest" and not r.get("revoked")
        )
        if live_guests >= MAX_GUEST_PINS:
            raise ValueError(f"at most {MAX_GUEST_PINS} guest PINs can be active at once")
        for rec in state["pins"].values():
            if rec.get("revoked"):
                continue
            # Two identical PINs would make revoking one look like it did
            # nothing, since the other still opens the gate.
            if _compare(_hash_pin(pin, rec["salt"]), rec["pin_hash"]):
                raise ValueError("that PIN is already in use")
        expires_at = time.time() + int(expires_in_s) if expires_in_s else None
        pin_id = secrets.token_hex(8)
        state["pins"][pin_id] = _new_pin_record(pin, "guest", label or "Guest", expires_at)
        _save(state)
    return _public_pin(pin_id, state["pins"][pin_id])


def _public_pin(pin_id, rec):
    """A PIN record with every secret stripped -- no salt, no hash. What the
    API and UI are allowed to see."""
    return {
        "id": pin_id,
        "label": rec.get("label"),
        "kind": rec.get("kind"),
        "created_at": rec.get("created_at"),
        "expires_at": rec.get("expires_at"),
        "last_used_at": rec.get("last_used_at"),
        "revoked": bool(rec.get("revoked")),
    }


def list_pins(include_revoked=False):
    state = _load()
    pins = [
        _public_pin(pin_id, rec)
        for pin_id, rec in state.get("pins", {}).items()
        if include_revoked or not rec.get("revoked")
    ]
    pins.sort(key=lambda p: (p["kind"] != "household", p["created_at"] or 0))
    return pins


def revoke_pin(pin_id):
    """Revoke one guest PIN and every session that was opened with it.
    Refuses to touch the household PIN: revoking it would leave the gate
    enabled with nothing able to open it -- a self-inflicted lockout that
    would need editing the state file on the host to undo."""
    with _lock:
        state = _load()
        rec = state.get("pins", {}).get(pin_id)
        if not rec:
            return False, 0
        if rec.get("kind") == "household":
            raise ValueError("the household PIN can't be revoked -- change it or disable the gate")
        if rec.get("revoked"):
            return False, 0
        rec["revoked"] = True
        rec["revoked_at"] = time.time()
        revoked_sessions = 0
        for sess in state.get("sessions", {}).values():
            if sess.get("pin_id") == pin_id and not sess.get("revoked"):
                sess["revoked"] = True
                sess["revoked_at"] = time.time()
                revoked_sessions += 1
        _save(state)
        return True, revoked_sessions


def disable():
    with _lock:
        state = _load()
        state["enabled"] = False
        _save(state)


def set_login_rate_limit(max_attempts, window_s):
    """Tune the per-IP login rate limit at runtime (persisted like every
    other setting here). Callers/UIs should treat DEFAULT_LOGIN_RATE_LIMIT_*
    (env-overridable) as the out-of-the-box sane default, this as the
    explicit override path."""
    if max_attempts < 1 or window_s < 1:
        raise ValueError("max_attempts and window_s must each be >= 1")
    with _lock:
        state = _load()
        state["login_rate_limit_max"] = max_attempts
        state["login_rate_limit_window_s"] = window_s
        _save(state)


def check_login_rate_limit(ip):
    """Fixed-window per-IP request-volume limiter for the login endpoint
    itself. Independent of, and checked BEFORE, the MAX_ATTEMPTS/
    LOCKOUT_SECONDS wrong-PIN lockout below -- this throttles raw request
    volume regardless of which PIN was guessed, so it catches a
    slow/distributed brute force that spreads guesses across many PINs from
    one IP specifically to stay under the lockout's per-account radar.
    Returns (allowed: bool, retry_after_seconds: float)."""
    state = _load()
    max_attempts = state.get("login_rate_limit_max", DEFAULT_LOGIN_RATE_LIMIT_MAX)
    window_s = state.get("login_rate_limit_window_s", DEFAULT_LOGIN_RATE_LIMIT_WINDOW_S)
    key = normalize_ip(ip)
    now = time.time()
    with _rate_limit_lock:
        bucket = _rate_limit_buckets.get(key)
        if not bucket or now - bucket["window_start"] >= window_s:
            bucket = {"window_start": now, "count": 0}
            _rate_limit_buckets[key] = bucket
        bucket["count"] += 1
        if bucket["count"] > max_attempts:
            retry_after = max(0.0, window_s - (now - bucket["window_start"]))
            blocked = True
        else:
            blocked = False
    if blocked:
        _bump("login_rate_limit_blocks")
        return False, retry_after
    return True, 0.0


def _lockout_policy():
    state = _load()
    return (
        state.get("lockout_max_attempts", MAX_ATTEMPTS),
        state.get("lockout_base_s", LOCKOUT_SECONDS),
        state.get("lockout_max_s", LOCKOUT_MAX_SECONDS),
    )


def _lockout_duration(lockouts, base_s, max_s):
    """Nth lockout for one IP lasts base * 2**(N-1), capped at max_s. The
    cap matters: without it a long-running attacker's window overflows into
    "effectively permanent", which also permanently locks out the household
    member sharing that public IP."""
    return min(max_s, base_s * (2 ** max(0, lockouts - 1)))


def _is_locked_out(ip):
    key = normalize_ip(ip)
    with _attempts_lock:
        entry = _attempts.get(key)
        if not entry:
            return False, 0
        locked_until = entry.get("locked_until") or 0
        if locked_until and time.time() < locked_until:
            return True, round(locked_until - time.time())
        return False, 0


def _record_failure(ip):
    """Count one wrong PIN against `ip` and lock it out once the configured
    threshold is reached. Returns True if this failure is what triggered a
    lockout, so the caller can log it distinctly from an ordinary failure."""
    key = normalize_ip(ip)
    max_attempts, base_s, max_s = _lockout_policy()
    now = time.time()
    with _attempts_lock:
        entry = _attempts.setdefault(
            key, {"count": 0, "locked_until": 0, "lockouts": 0, "last_failure": 0}
        )
        # An IP that behaved for a full decay period starts over at the base
        # penalty; escalation is meant to punish sustained guessing, not to
        # brand an address forever.
        if entry["last_failure"] and now - entry["last_failure"] > LOCKOUT_ESCALATION_DECAY_S:
            entry["lockouts"] = 0
        entry["last_failure"] = now
        entry["count"] += 1
        if entry["count"] >= max_attempts:
            entry["lockouts"] += 1
            entry["locked_until"] = now + _lockout_duration(entry["lockouts"], base_s, max_s)
            entry["count"] = 0
            triggered = True
        else:
            triggered = False
    if triggered:
        _bump("lockouts_triggered")
    return triggered


def _record_success(ip):
    with _attempts_lock:
        _attempts.pop(normalize_ip(ip), None)


def _compare(a, b):
    """Constant-time equality for the hex digests and signatures compared
    here. Wraps hmac.compare_digest because its str form raises TypeError on
    any non-ASCII character -- and one side is attacker-controlled (a cookie
    value), so a single non-ASCII byte in a forged token turned a rejected
    signature into an unhandled 500. Comparing as UTF-8 bytes has no such
    restriction and keeps the timing property."""
    if not isinstance(a, (str, bytes)) or not isinstance(b, (str, bytes)):
        return False
    a_bytes = a.encode("utf-8") if isinstance(a, str) else a
    b_bytes = b.encode("utf-8") if isinstance(b, str) else b
    return hmac.compare_digest(a_bytes, b_bytes)


def verify_pin(pin, ip):
    """Returns (ok, detail, pin_id). `detail` is a lockout message when
    applicable — callers should surface it as-is (it deliberately doesn't
    leak whether a PIN was close/wrong, only whether the account is locked).
    `pin_id` identifies WHICH configured PIN matched, so the session can be
    tied to it and revoked with it."""
    locked, remaining = _is_locked_out(ip)
    if locked:
        return False, f"locked out for {remaining}s after too many failed attempts", None
    state = _load()
    active = {
        pin_id: rec
        for pin_id, rec in state.get("pins", {}).items()
        if not rec.get("revoked")
        and (not rec.get("expires_at") or rec["expires_at"] > time.time())
    }
    if not state["enabled"] or not active:
        return False, "PIN auth is not enabled", None

    # Every configured PIN is hashed and compared, with no early exit on a
    # match: bailing on the first hit would make a household-PIN login
    # measurably faster than a guest-PIN one, leaking which slot matched.
    # Cost is bounded by how many PINs the household chose to configure.
    matched_id = None
    for pin_id, rec in active.items():
        if _compare(_hash_pin(pin, rec["salt"]), rec["pin_hash"]):
            matched_id = pin_id

    if matched_id:
        _record_success(ip)
        with _lock:
            fresh = _load()
            if matched_id in fresh.get("pins", {}):
                fresh["pins"][matched_id]["last_used_at"] = time.time()
                _save(fresh)
        _bump("login_success")
        return True, None, matched_id

    triggered = _record_failure(ip)
    _bump("login_failure")
    if triggered:
        _, remaining2 = _is_locked_out(ip)
        return False, f"too many failed attempts — locked out for {remaining2}s", None
    return False, "incorrect PIN", None


def auth_metrics():
    """Auth-side counters for the diagnostics API: how much login traffic is
    failing, how often lockouts and the login rate limiter are firing, and
    how many IPs are locked out right now. In-memory/restart-resets."""
    now = time.time()
    with _attempts_lock:
        locked_now = sum(
            1 for e in _attempts.values() if (e.get("locked_until") or 0) > now
        )
        tracked = len(_attempts)
    with _counters_lock:
        counters = dict(_auth_counters)
    max_attempts, base_s, max_s = _lockout_policy()
    return {
        **counters,
        "locked_out_now": locked_now,
        "tracked_ips": tracked,
        "lockout_max_attempts": max_attempts,
        "lockout_base_s": base_s,
        "lockout_max_s": max_s,
    }


def _sign(payload_b64, secret_key):
    return hmac.new(bytes.fromhex(secret_key), payload_b64.encode(), hashlib.sha256).hexdigest()


def _prune_sessions(state):
    """Drop session records whose expiry has already passed so the
    allowlist doesn't grow without bound. Mutates and returns `state`;
    caller is responsible for _save()-ing it."""
    now = time.time()
    sessions = state.get("sessions", {})
    state["sessions"] = {jti: rec for jti, rec in sessions.items() if rec.get("expires_at", 0) > now}
    return state


def create_session_token(ip=None, pin_id=None):
    """Issue a new session: a stateless HMAC-signed token (payload + sig,
    same as before) PLUS a server-side allowlist entry keyed by a random
    jti embedded in the payload. Both must check out for the session to be
    considered valid -- see verify_session_token(). `pin_id` records which
    configured PIN opened it, so revoking a guest PIN can take its sessions
    down with it."""
    with _lock:
        state = _load()
        _prune_sessions(state)
        now = time.time()
        exp = now + state["session_ttl_s"]
        jti = secrets.token_hex(16)
        state["sessions"][jti] = {
            "created_at": now,
            "expires_at": exp,
            "revoked": False,
            "revoked_at": None,
            "ip": ip,
            "pin_id": pin_id,
            "last_seen": now,
        }
        _save(state)
        secret_key = state["secret_key"]
    payload = json.dumps({"exp": exp, "jti": jti}).encode()
    payload_b64 = base64.urlsafe_b64encode(payload).decode()
    sig = _sign(payload_b64, secret_key)
    return f"{payload_b64}.{sig}"


def verify_session_token(token, touch=True):
    """A session is valid only if its HMAC signature checks out AND its
    embedded jti is present in the server-side allowlist, not revoked, and
    not expired. This is additive on top of the original stateless-signing
    design (the signature is still checked first, unconditionally) -- it
    does not replace it with a purely server-side session store."""
    if not token or "." not in token:
        return False
    payload_b64, _, sig = token.partition(".")
    state = _load()
    expected = _sign(payload_b64, state["secret_key"])
    if not _compare(sig, expected):
        return False
    try:
        payload = json.loads(base64.urlsafe_b64decode(payload_b64.encode()))
    except Exception:
        return False
    if time.time() >= payload.get("exp", 0):
        return False
    jti = payload.get("jti")
    if not jti:
        # Pre-revocation-feature token format (no jti) -- signature is
        # technically valid but there's no allowlist entry to check
        # revocation against. Reject rather than silently grandfathering
        # it in, since that would let an old-format token bypass
        # revocation entirely; this just forces one fresh login.
        return False
    session = state.get("sessions", {}).get(jti)
    if not session or session.get("revoked"):
        return False
    if touch:
        # Best-effort last-seen update; re-reads/re-checks under the lock
        # so a revoke() that lands concurrently still wins.
        with _lock:
            fresh = _load()
            fresh_sess = fresh.get("sessions", {}).get(jti)
            if not fresh_sess or fresh_sess.get("revoked"):
                return False
            fresh_sess["last_seen"] = time.time()
            _save(fresh)
    return True


def get_token_jti(token):
    """Return the jti embedded in `token` if its signature is valid,
    else None. Used so a client can only revoke/identify a session it
    actually holds a correctly-signed cookie for, not an arbitrary
    guessed session id."""
    if not token or "." not in token:
        return None
    payload_b64, _, sig = token.partition(".")
    state = _load()
    expected = _sign(payload_b64, state["secret_key"])
    if not _compare(sig, expected):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(payload_b64.encode()))
    except Exception:
        return None
    return payload.get("jti")


def list_sessions():
    """Currently-active (not revoked, not expired) sessions. Never includes
    the raw bearer token or the PIN -- only the jti (a random id that
    cannot be used to reconstruct a valid token without the server's
    secret key), timestamps, and the IP the session was created from."""
    with _lock:
        state = _load()
        _prune_sessions(state)
        _save(state)
        sessions = dict(state.get("sessions", {}))
    result = [
        {
            "id": jti,
            "created_at": rec.get("created_at"),
            "expires_at": rec.get("expires_at"),
            "last_seen": rec.get("last_seen"),
            "ip": rec.get("ip"),
            "pin_id": rec.get("pin_id"),
        }
        for jti, rec in sessions.items()
        if not rec.get("revoked")
    ]
    result.sort(key=lambda r: r["created_at"] or 0, reverse=True)
    return result


def revoke_session(jti):
    """Revoke one session by its jti. Returns True if a matching,
    not-already-revoked session was found and revoked."""
    with _lock:
        state = _load()
        _prune_sessions(state)
        session = state.get("sessions", {}).get(jti)
        found = session is not None and not session.get("revoked")
        if session is not None:
            session["revoked"] = True
            session["revoked_at"] = time.time()
        _save(state)
        return found


def revoke_current_session(token):
    """Revoke whichever session `token` (a correctly-signed cookie value)
    identifies. Used for logout: a real server-side invalidation, not just
    deleting the client-side cookie."""
    jti = get_token_jti(token)
    if not jti:
        return False
    return revoke_session(jti)


def _revoke_all_locked(state):
    """Revoke-everything body, for callers that already hold _lock and are
    mid-transaction on `state` (change_pin). Rotates the signing key too, so
    even a validly-signed token with no allowlist entry can't survive.
    Returns the number of sessions actively revoked; caller _save()s."""
    _prune_sessions(state)
    count = 0
    for rec in state.get("sessions", {}).values():
        if not rec.get("revoked"):
            rec["revoked"] = True
            rec["revoked_at"] = time.time()
            count += 1
    state["secret_key"] = secrets.token_hex(32)
    return count


def revoke_all_sessions():
    """Force every currently-logged-in client to re-authenticate (e.g.
    after a suspected compromise). Marks every tracked session revoked AND
    rotates the signing secret key, so even an edge case where a valid
    signature exists without a matching allowlist entry can't survive
    this call. Returns the number of sessions that were actively revoked."""
    with _lock:
        state = _load()
        count = _revoke_all_locked(state)
        _save(state)
        return count


def path_requires_auth(path):
    if not is_enabled():
        return False
    if path in OPEN_PATHS:
        return False
    if any(path.startswith(p) for p in OPEN_PREFIXES):
        return False
    return True
