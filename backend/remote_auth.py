import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)
AUTH_PATH = os.path.join(DATA_DIR, "remote_auth.json")

SESSION_COOKIE = "sbd_session"
DEFAULT_SESSION_TTL_S = 24 * 3600
MAX_ATTEMPTS = 5
LOCKOUT_SECONDS = 300
PBKDF2_ITERATIONS = 200_000

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


def _default_state():
    return {
        "enabled": False,
        "pin_hash": None,
        "salt": None,
        "secret_key": secrets.token_hex(32),
        "session_ttl_s": DEFAULT_SESSION_TTL_S,
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
    return {"enabled": state["enabled"], "session_ttl_s": state["session_ttl_s"]}


def get_session_ttl():
    return _load()["session_ttl_s"]


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


def create_session_token():
    state = _load()
    exp = time.time() + state["session_ttl_s"]
    payload = json.dumps({"exp": exp}).encode()
    payload_b64 = base64.urlsafe_b64encode(payload).decode()
    sig = _sign(payload_b64, state["secret_key"])
    return f"{payload_b64}.{sig}"


def verify_session_token(token):
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
    return time.time() < payload.get("exp", 0)


def path_requires_auth(path):
    if not is_enabled():
        return False
    if path in OPEN_PATHS:
        return False
    if any(path.startswith(p) for p in OPEN_PREFIXES):
        return False
    return True
