"""Environment-variable / `.env` secrets support (Week 2, W2-211, W2-215,
W2-223) plus the shared redaction helper the rest of the app uses.

The problem this solves: `config.json` is the only place a device's
`local_key` lives, in plaintext, next to the code. That's a reasonable
default for a LAN-only home install (and it's git-ignored), but it means
the secret is readable by anything that can read the file, ends up in any
naive backup, and is easy to leak by accident. This module lets a
security-conscious operator move `local_key` values out of that file into
the process environment (or a `.env` file kept outside version control)
without changing anything else about how the app works.

Design rules that matter:

  - The real environment always wins over the `.env` file. A `.env` is a
    convenience for a desktop/Docker install, not an override of what an
    operator explicitly exported.
  - An env-sourced key is never written back to `config.json`. `config.py`
    tags such devices on load and strips the value again on save -- without
    that, a single "rename this bulb" call through the UI would silently
    persist the secret into the exact file the operator moved it out of.
  - No new dependency. python-dotenv happens to be installed (it comes in
    via `uvicorn[standard]`), but relying on a transitive extra for a
    security feature is how a feature quietly disappears on an unrelated
    dependency bump. The parser below is ~20 lines.

Deliberately NOT supported: an env-provided session-signing secret. That
key must be rotatable at runtime (`remote_auth.revoke_all_sessions()`
rotates it as part of "log everyone out"), and a value pinned in the
environment can't be rotated by the process that depends on it. Sourcing
it from env would silently turn revoke-all into a partial control.
"""

import os
import re

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(BACKEND_DIR)

# Repo root, not backend/ -- it sits next to `.env.example`, and keeping it
# out of the package directory makes it harder to accidentally ship.
DEFAULT_ENV_PATH = os.path.join(REPO_ROOT, ".env")

DEVICE_KEY_PREFIX = "SBD_LOCAL_KEY_"

# Which secrets exist in this system and what an attacker gains from each.
# Referenced by docs/security-secrets.md and returned (values never
# included) by GET /api/security/secrets so the answer lives in one place.
SECRET_SENSITIVITY = {
    "local_key": {
        "what": "Per-bulb Tuya local key",
        "grants": "Full local control of that bulb (colour, power) by anyone on the same LAN",
        "stored_in": "backend/config.json, or the environment (see this module)",
        "rotate_by": "Re-pairing the bulb in the Tuya/Smart Life app and re-reading the key",
        "severity": "high",
    },
    "pin": {
        "what": "Remote-access PIN",
        "grants": "Full dashboard + API access when the PIN gate is enabled",
        "stored_in": "Never stored. Only a PBKDF2-SHA256 hash (200k iterations) + salt "
                     "in backend/data/remote_auth.json",
        "rotate_by": "Settings -> Remote Access -> re-enable with a new PIN",
        "severity": "high",
    },
    "session_secret": {
        "what": "HMAC key that signs session cookies",
        "grants": "Ability to forge a valid session cookie, bypassing the PIN entirely",
        "stored_in": "backend/data/remote_auth.json (256 bits from secrets.token_hex(32))",
        "rotate_by": "POST /api/auth/sessions/revoke-all (rotates the key and logs everyone out)",
        "severity": "critical",
    },
    "security_audit_key": {
        "what": "HMAC key for the security-event log's tamper-evident chain",
        "grants": "Ability to forge audit-log entries that pass verification",
        "stored_in": "backend/data/security_audit_key (0600 where the OS honours it)",
        "rotate_by": "Delete the file and the state file together, then re-verify "
                     "(this starts a NEW chain -- do it only deliberately)",
        "severity": "high",
    },
}


# --------------------------------------------------------------- .env ----
_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


def parse_env_file(path):
    """Minimal `.env` parser: KEY=VALUE, `#` comments, optional `export`
    prefix, optional single/double quotes. Returns a dict; a missing file
    is not an error (running without a `.env` is the normal case)."""
    values = {}
    if not path or not os.path.exists(path):
        return values
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            match = _ENV_LINE.match(line)
            if not match:
                continue
            key, value = match.group(1), match.group(2).strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            else:
                # Only strip trailing comments on unquoted values -- a quoted
                # value is taken literally, `#` included.
                value = value.split(" #", 1)[0].strip()
            values[key] = value
    return values


def load_env_file(path=None, override=False):
    """Load `.env` into os.environ. Existing environment variables win
    unless `override=True`. Returns the list of names that were set (never
    their values -- this return value goes into a startup log line)."""
    path = path or os.environ.get("SBD_ENV_FILE") or DEFAULT_ENV_PATH
    applied = []
    for key, value in parse_env_file(path).items():
        if override or key not in os.environ:
            os.environ[key] = value
            applied.append(key)
    return applied


# ------------------------------------------------------- device secrets --
def env_var_for_device(device_id):
    """`bulb-1` -> `SBD_LOCAL_KEY_BULB_1`. Any character that isn't
    alphanumeric becomes `_`, since env var names can't hold `-` portably."""
    suffix = re.sub(r"[^A-Za-z0-9]", "_", str(device_id)).upper()
    return DEVICE_KEY_PREFIX + suffix


def local_key_from_env(device_id):
    value = os.environ.get(env_var_for_device(device_id))
    return value or None


def apply_env_overrides(cfg):
    """Replace each device's `local_key` with the environment's value where
    one is set, tagging the device so `config.save_config()` knows not to
    persist it. Mutates and returns `cfg` (callers already treat the loaded
    config as theirs to mutate)."""
    for device in cfg.get("devices", []):
        env_value = local_key_from_env(device.get("id"))
        if env_value:
            device["local_key"] = env_value
            device["_local_key_from_env"] = True
        else:
            device.pop("_local_key_from_env", None)
    return cfg


def strip_env_sourced(cfg):
    """Return a deep-enough copy safe to write to config.json: any device
    whose key came from the environment gets an empty `local_key` on disk,
    and the internal tag is removed. This is what stops a routine save from
    leaking the secret back into the file the operator moved it out of."""
    out = dict(cfg)
    devices = []
    for device in cfg.get("devices", []):
        d = dict(device)
        if d.pop("_local_key_from_env", False):
            d["local_key"] = ""
        devices.append(d)
    out["devices"] = devices
    return out


def secret_inventory(cfg):
    """Non-secret report: for each configured device, where its key comes
    from and whether one is present at all. Never includes a key value, a
    length, or a prefix -- 'set/unset' plus the source is everything a UI
    legitimately needs to show."""
    devices = []
    for device in cfg.get("devices", []):
        from_env = bool(device.get("_local_key_from_env"))
        devices.append({
            "device_id": device.get("id"),
            "name": device.get("name", device.get("id")),
            "local_key_present": bool(device.get("local_key")),
            "local_key_source": "environment" if from_env else "config.json",
            "env_var": env_var_for_device(device.get("id")),
        })
    return {
        "devices": devices,
        "env_file": os.environ.get("SBD_ENV_FILE") or DEFAULT_ENV_PATH,
        "env_file_present": os.path.exists(os.environ.get("SBD_ENV_FILE") or DEFAULT_ENV_PATH),
        "sensitivity": SECRET_SENSITIVITY,
    }


# ------------------------------------------------------------ redaction --
MASK = "[redacted]"


def redact_secrets(text, extra=(), include_config=True):
    """Scrub known secret *values* out of an arbitrary string -- the
    backstop for error messages and third-party exception text, where we
    don't control the format and can't rely on structured redaction.

    `extra` is for values the caller already holds. `include_config=False`
    skips reading config.json, which matters on hot paths: a bulb error can
    be logged on every failed frame, and a disk read per log line would be
    a real cost. A caller that already has the device's own key (see
    `BulbController._safe_error`) passes it via `extra` and turns the
    lookup off.

    Values shorter than 4 characters are skipped -- masking a 1-2 character
    "secret" would shred unrelated text for no security gain.
    """
    if not text:
        return text
    text = str(text)
    values = {v for v in extra if v}
    if include_config:
        try:
            import config as cfgmod  # local import: config imports this module
            for device in cfgmod.load_config().get("devices", []):
                if device.get("local_key"):
                    values.add(device["local_key"])
        except Exception:
            # Redaction must never be the thing that raises -- if config
            # can't be read we still scrub whatever `extra` gave us.
            pass
    for value in sorted(values, key=lambda v: len(str(v)), reverse=True):
        if len(str(value)) >= 4:
            text = text.replace(str(value), MASK)
    return text
