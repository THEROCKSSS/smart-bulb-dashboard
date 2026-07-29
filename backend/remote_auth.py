import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from datetime import datetime, timezone

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
MAX_ATTEMPTS = 5
LOCKOUT_SECONDS = 300
PBKDF2_ITERATIONS = 200_000

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
# an acceptable tradeoff for a single-user local dashboard. Keyed by client
# IP, so this throttles one attacking source, not global request volume.
_attempts = {}  # ip -> {"count": int, "locked_until": float}

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


def _default_state():
    return {
        "enabled": False,
        "pin_hash": None,
        "salt": None,
        "secret_key": secrets.token_hex(32),
        "session_ttl_s": DEFAULT_SESSION_TTL_S,
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


def _load():
    if not os.path.exists(AUTH_PATH):
        return _default_state()
    with open(AUTH_PATH, "r") as f:
        data = json.load(f)
    for k, v in _default_state().items():
        data.setdefault(k, v)
    return data


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
    }


def get_session_ttl():
    return _load()["session_ttl_s"]


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


def enable(pin, session_ttl_s=None):
    if not pin or len(pin) < 4:
        raise ValueError("PIN must be at least 4 characters")
    with _lock:
        state = _load()
        salt = secrets.token_hex(16)
        state["salt"] = salt
        state["pin_hash"] = _hash_pin(pin, salt)
        state["enabled"] = True
        if session_ttl_s:
            state["session_ttl_s"] = session_ttl_s
        _save(state)


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
    now = time.time()
    with _rate_limit_lock:
        bucket = _rate_limit_buckets.get(ip)
        if not bucket or now - bucket["window_start"] >= window_s:
            bucket = {"window_start": now, "count": 0}
            _rate_limit_buckets[ip] = bucket
        bucket["count"] += 1
        if bucket["count"] > max_attempts:
            retry_after = max(0.0, window_s - (now - bucket["window_start"]))
            return False, retry_after
        return True, 0.0


def _is_locked_out(ip):
    entry = _attempts.get(ip)
    if not entry:
        return False, 0
    if entry["locked_until"] and time.time() < entry["locked_until"]:
        return True, round(entry["locked_until"] - time.time())
    return False, 0


def _record_failure(ip):
    entry = _attempts.setdefault(ip, {"count": 0, "locked_until": 0})
    entry["count"] += 1
    if entry["count"] >= MAX_ATTEMPTS:
        entry["locked_until"] = time.time() + LOCKOUT_SECONDS
        entry["count"] = 0


def _record_success(ip):
    _attempts.pop(ip, None)


def verify_pin(pin, ip):
    """Returns (ok, detail). `detail` is a lockout message when applicable —
    callers should surface it as-is (it deliberately doesn't leak whether a
    PIN was close/wrong, only whether the account is locked)."""
    locked, remaining = _is_locked_out(ip)
    if locked:
        return False, f"locked out for {remaining}s after too many failed attempts"
    state = _load()
    if not state["enabled"] or not state["pin_hash"]:
        return False, "PIN auth is not enabled"
    candidate = _hash_pin(pin, state["salt"])
    if hmac.compare_digest(candidate, state["pin_hash"]):
        _record_success(ip)
        return True, None
    _record_failure(ip)
    still_locked, remaining2 = _is_locked_out(ip)
    if still_locked:
        return False, f"too many failed attempts — locked out for {remaining2}s"
    return False, "incorrect PIN"


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


def create_session_token(ip=None):
    """Issue a new session: a stateless HMAC-signed token (payload + sig,
    same as before) PLUS a server-side allowlist entry keyed by a random
    jti embedded in the payload. Both must check out for the session to be
    considered valid -- see verify_session_token()."""
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
    if not hmac.compare_digest(sig, expected):
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
    if not hmac.compare_digest(sig, expected):
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


def revoke_all_sessions():
    """Force every currently-logged-in client to re-authenticate (e.g.
    after a suspected compromise). Marks every tracked session revoked AND
    rotates the signing secret key, so even an edge case where a valid
    signature exists without a matching allowlist entry can't survive
    this call. Returns the number of sessions that were actively revoked."""
    with _lock:
        state = _load()
        _prune_sessions(state)
        count = 0
        for rec in state.get("sessions", {}).values():
            if not rec.get("revoked"):
                rec["revoked"] = True
                rec["revoked_at"] = time.time()
                count += 1
        state["secret_key"] = secrets.token_hex(32)
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
