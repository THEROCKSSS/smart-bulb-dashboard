"""Backup / restore for config + runtime data (Week 2, W2-161..175).

## The thing to understand before touching this file

**A backup of this app contains every bulb's `local_key` in plaintext.**
That key is full local control of the bulb for anyone on the same LAN, and
it does not expire. So an unencrypted backup is a portable, permanent
credential file — which is why `create_backup()` takes a password, why the
API response says so in words when you don't supply one, and why the
dashboard makes you tick a box acknowledging it. The docs saying "you
should encrypt it" would not have been enough; by the time someone reads
the docs the plaintext archive is already in their Downloads folder.

## What is deliberately NOT in a backup

  - `data/remote_auth.json` — the PIN hash + salt and the session-signing
    key. Two reasons, both load-bearing:
      1. An offline attacker with the PIN hash can brute-force a short
         numeric PIN regardless of PBKDF2, so putting it in an archive
         that might be unencrypted is a real downgrade of the PIN gate.
      2. **W2-175**: a restore must never silently flip remote access on
         or off. Not writing the file at all makes that structural rather
         than a rule someone has to remember — there is no code path here
         that touches remote-access state. `restore_backup()` still reads
         the flag before and after and reports both, so the guarantee is
         observable, not just asserted. `test_backup_restore.py` pins it.
  - the security-event log, its chain state and its HMAC key — restoring
    those would rewind the tamper-evident trail, which is precisely the
    "compromise quietly erases its own trail" move the chain exists to
    prevent. Migrating them is a deliberate manual copy, not a side effect
    of a restore. See docs/backup-restore.md.

## Format

Unencrypted: a plain `.zip` (openable with any zip tool — an archive you
can't inspect without this app is a bad backup) containing `manifest.json`,
`config.json`, `data/...`, and `remote_auth_settings.json` (non-secret
settings only, informational, never applied on restore).

Encrypted: `SBDBACKUP1\n` + a JSON header line + AES-256-GCM ciphertext of
those same zip bytes. The header (salt, nonce, KDF params) is readable
without the password so `verify_backup()` can say "this is a valid
encrypted archive, it needs a password" instead of "corrupt". The header is
passed as AEAD associated data, so its parameters can't be swapped without
failing authentication.
"""

import hashlib
import io
import json
import os
import re
import secrets
import threading
import time
import zipfile
from datetime import datetime, timezone

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import security_audit

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BACKEND_DIR, "data")
CONFIG_PATH = os.path.join(BACKEND_DIR, "config.json")
REMOTE_AUTH_PATH = os.path.join(DATA_DIR, "remote_auth.json")

# Configurable destination (W2-163): point this at an external drive or a
# synced folder and every backup lands there instead.
BACKUP_DIR = os.environ.get("SBD_BACKUP_DIR") or os.path.join(BACKEND_DIR, "backups")
SETTINGS_PATH = os.path.join(DATA_DIR, "backup_settings.json")

FORMAT_VERSION = 1
MAGIC = b"SBDBACKUP1\n"
# Matches remote_auth.PBKDF2_ITERATIONS deliberately -- one number to reason
# about for "how hard is a password in this project to grind", not two.
KDF_ITERATIONS = 200_000

_DEFAULT_SETTINGS = {"keep": 10}

# Never backed up, never restored. See the module docstring for why each is
# on this list -- these are not user-tunable exclusions.
HARD_EXCLUDED_DATA = {
    "remote_auth.json",
    "security_audit_key",
    "security_audit_state.json",
    "security_alerts.json",
    "backups",
}
HARD_EXCLUDED_PREFIXES = ("security_events.log",)

# Restorable sections (W2-167). `devices` is called out on its own because
# it is the only one carrying credentials -- everything else can be restored
# without touching a single local_key.
SECTIONS = {
    "devices": {
        "label": "Devices (includes local_keys)",
        "config_keys": ["devices"],
        "data_files": [],
        "touches_credentials": True,
    },
    "groups_zones": {
        "label": "Groups, zones & orchestration presets",
        "config_keys": ["groups", "zones", "orchestration_presets"],
        "data_files": [],
        "touches_credentials": False,
    },
    "favorites": {
        "label": "Favorite colours",
        "config_keys": [],
        "data_files": ["favorites.json"],
        "touches_credentials": False,
    },
    "schedules": {
        "label": "Schedules",
        "config_keys": [],
        "data_files": ["schedules.json"],
        "touches_credentials": False,
    },
    "audio": {
        "label": "Audio presets, calibration & safety settings",
        "config_keys": ["audio_input_calibrations"],
        "data_files": ["audio_session_presets.json", "audio_last_session.json",
                       "audio_safety.json"],
        "touches_credentials": False,
    },
    "lightshows": {
        "label": "Recorded lightshows",
        "config_keys": [],
        "data_files": ["lightshows"],
        "touches_credentials": False,
    },
    "discovery": {
        "label": "Network discovery state",
        "config_keys": [],
        "data_files": ["discovery.json"],
        "touches_credentials": False,
    },
}

_lock = threading.Lock()

_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


# ------------------------------------------------------------ settings ---
def get_settings():
    settings = dict(_DEFAULT_SETTINGS)
    if os.path.exists(SETTINGS_PATH):
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                stored = json.load(f)
            if isinstance(stored, dict):
                settings.update({k: v for k, v in stored.items() if k in _DEFAULT_SETTINGS})
        except (json.JSONDecodeError, OSError):
            pass
    return settings


def update_settings(keep=None):
    settings = get_settings()
    if keep is not None:
        if int(keep) < 1:
            raise ValueError("keep must be >= 1")
        settings["keep"] = int(keep)
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)
    return settings


# ------------------------------------------------------------- helpers ---
def _app_version():
    # Lazy: main.py imports this module, so a top-level import would cycle.
    try:
        import main
        return getattr(main, "APP_VERSION", None)
    except Exception:
        return None


def _sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _is_hard_excluded(name):
    return name in HARD_EXCLUDED_DATA or name.startswith(HARD_EXCLUDED_PREFIXES)


def _collect_data_files(exclude):
    """Every file under data/ that is eligible for backup, as
    (arcname, absolute_path) pairs. Directories are walked so recorded
    lightshows come along."""
    out = []
    if not os.path.isdir(DATA_DIR):
        return out
    for name in sorted(os.listdir(DATA_DIR)):
        if _is_hard_excluded(name) or name in exclude:
            continue
        path = os.path.join(DATA_DIR, name)
        if os.path.isdir(path):
            for root, _dirs, files in os.walk(path):
                for fname in sorted(files):
                    abs_path = os.path.join(root, fname)
                    rel = os.path.relpath(abs_path, DATA_DIR).replace(os.sep, "/")
                    out.append((f"data/{rel}", abs_path))
        elif os.path.isfile(path):
            out.append((f"data/{name}", path))
    return out


def _remote_auth_settings_snapshot():
    """Non-secret remote-access settings, recorded for reference only.
    Explicitly excludes pin_hash, salt, secret_key and the live session
    allowlist -- and nothing in this module ever writes this back."""
    if not os.path.exists(REMOTE_AUTH_PATH):
        return {"present": False}
    try:
        with open(REMOTE_AUTH_PATH, "r", encoding="utf-8") as f:
            state = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"present": False}
    return {
        "present": True,
        "enabled": state.get("enabled", False),
        "session_ttl_s": state.get("session_ttl_s"),
        "login_rate_limit_max": state.get("login_rate_limit_max"),
        "login_rate_limit_window_s": state.get("login_rate_limit_window_s"),
        "note": "Informational only. Restoring a backup never changes remote-access "
                "state, and the PIN hash / session signing key are never included.",
    }


def optional_exclusions():
    """What a user may legitimately leave out (W2-170) -- everything under
    data/ that isn't hard-excluded, plus config.json itself. Returned with
    sizes so the UI can show what leaving it out actually saves."""
    out = []
    if os.path.isfile(CONFIG_PATH):
        out.append({"name": "config.json", "bytes": os.path.getsize(CONFIG_PATH)})
    if os.path.isdir(DATA_DIR):
        for name in sorted(os.listdir(DATA_DIR)):
            if _is_hard_excluded(name):
                continue
            path = os.path.join(DATA_DIR, name)
            if os.path.isdir(path):
                size = sum(os.path.getsize(os.path.join(r, f))
                           for r, _d, fs in os.walk(path) for f in fs)
            else:
                size = os.path.getsize(path)
            out.append({"name": name, "bytes": size})
    return out


# ---------------------------------------------------------- encryption ---
def _derive_key(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, KDF_ITERATIONS, dklen=32)


def _encrypt(plaintext, password):
    salt = secrets.token_bytes(16)
    nonce = secrets.token_bytes(12)
    header = json.dumps({
        "cipher": "aes-256-gcm",
        "kdf": "pbkdf2-sha256",
        "iterations": KDF_ITERATIONS,
        "salt": salt.hex(),
        "nonce": nonce.hex(),
    }, sort_keys=True).encode("utf-8")
    key = _derive_key(password, salt)
    # Header as AAD: an attacker can't swap in a weaker iteration count or a
    # different salt without the tag failing.
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, header)
    return MAGIC + header + b"\n" + ciphertext


def _split_encrypted(blob):
    if not blob.startswith(MAGIC):
        return None, None
    rest = blob[len(MAGIC):]
    newline = rest.find(b"\n")
    if newline < 0:
        return None, None
    return rest[:newline], rest[newline + 1:]


def _decrypt(blob, password):
    header_bytes, ciphertext = _split_encrypted(blob)
    if header_bytes is None:
        raise ValueError("not an encrypted Smart Bulb Dashboard backup")
    header = json.loads(header_bytes.decode("utf-8"))
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                              bytes.fromhex(header["salt"]),
                              int(header["iterations"]), dklen=32)
    try:
        return AESGCM(key).decrypt(bytes.fromhex(header["nonce"]), ciphertext, header_bytes)
    except InvalidTag:
        # Indistinguishable by design: a wrong password and a tampered file
        # both fail authentication, and we can't tell which without leaking
        # an oracle. Say both.
        raise ValueError("wrong password, or the archive has been modified")


def is_encrypted_file(path):
    try:
        with open(path, "rb") as f:
            return f.read(len(MAGIC)) == MAGIC
    except OSError:
        return False


# -------------------------------------------------------------- create ---
def create_backup(password=None, exclude=None, note=None, prune=True):
    """Write one archive and return its metadata.

    `password=None` produces a plain zip containing every device local_key
    in the clear. That is a supported choice (a local backup to an encrypted
    disk is a perfectly reasonable posture) but the returned dict always
    carries an explicit `warning` when it happens, so no caller can present
    an unencrypted backup as if it were neutral.
    """
    exclude = set(exclude or [])
    unknown = exclude - {e["name"] for e in optional_exclusions()}
    if unknown:
        raise ValueError(f"cannot exclude unknown item(s): {sorted(unknown)}")

    os.makedirs(BACKUP_DIR, exist_ok=True)
    now = time.time()
    stamp = datetime.fromtimestamp(now, timezone.utc).strftime("%Y%m%d-%H%M%S")

    files = []
    if "config.json" not in exclude and os.path.isfile(CONFIG_PATH):
        files.append(("config.json", CONFIG_PATH))
    files.extend(_collect_data_files(exclude))

    manifest = {
        "format_version": FORMAT_VERSION,
        "app_version": _app_version(),
        "created_at": now,
        "created_at_iso": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "note": note,
        "encrypted": bool(password),
        "excluded": sorted(exclude),
        "hard_excluded": sorted(HARD_EXCLUDED_DATA | set(HARD_EXCLUDED_PREFIXES)),
        "contains_device_credentials": False,
        "files": {},
    }

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for arcname, path in files:
            with open(path, "rb") as f:
                data = f.read()
            manifest["files"][arcname] = {"sha256": _sha256_bytes(data), "bytes": len(data)}
            zf.writestr(arcname, data)
        if "config.json" in manifest["files"]:
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                manifest["contains_device_credentials"] = any(
                    d.get("local_key") for d in cfg.get("devices", []))
            except (json.JSONDecodeError, OSError):
                manifest["contains_device_credentials"] = True  # assume the worst
        auth_snapshot = json.dumps(_remote_auth_settings_snapshot(), indent=2).encode("utf-8")
        manifest["files"]["remote_auth_settings.json"] = {
            "sha256": _sha256_bytes(auth_snapshot), "bytes": len(auth_snapshot)}
        zf.writestr("remote_auth_settings.json", auth_snapshot)
        zf.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True))

    payload = buf.getvalue()
    if password:
        filename = f"backup-{stamp}.sbdb"
        payload = _encrypt(payload, password)
    else:
        filename = f"backup-{stamp}.zip"

    path = os.path.join(BACKUP_DIR, filename)
    with _lock:
        # Collision guard: two backups in the same second would otherwise
        # silently overwrite each other.
        suffix = 1
        base, ext = os.path.splitext(path)
        while os.path.exists(path):
            path = f"{base}-{suffix}{ext}"
            suffix += 1
        with open(path, "wb") as f:
            f.write(payload)

    pruned = prune_backups() if prune else []

    result = {
        "ok": True,
        "name": os.path.basename(path),
        "path": path,
        "bytes": len(payload),
        "encrypted": bool(password),
        "manifest": manifest,
        "pruned": pruned,
    }
    if not password and manifest["contains_device_credentials"]:
        result["warning"] = (
            "This archive is NOT encrypted and contains your bulbs' local_key values in "
            "plaintext. Anyone who obtains this file can control those bulbs from your "
            "LAN, and the keys do not expire. Store it on an encrypted disk, or "
            "re-create the backup with a password."
        )
    security_audit.log_event("backup_created", "success", source="backup_restore",
                             name=result["name"], encrypted=bool(password),
                             excluded=sorted(exclude))
    return result


# --------------------------------------------------------------- listing --
def _resolve(name):
    """Turn a user-supplied backup name into an absolute path inside
    BACKUP_DIR, or raise. Rejects anything that isn't a plain file name --
    the name arrives from a URL path, so `../../config.json` must not be
    reachable."""
    if not name or not _SAFE_NAME.match(name):
        raise ValueError("invalid backup name")
    path = os.path.join(BACKUP_DIR, name)
    if os.path.dirname(os.path.abspath(path)) != os.path.abspath(BACKUP_DIR):
        raise ValueError("invalid backup name")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"backup '{name}' not found")
    return path


def list_backups():
    """Newest first. An encrypted archive's manifest can't be read without
    the password, so `manifest` is None for those -- the created-at from the
    filename plus size is what's honestly available."""
    if not os.path.isdir(BACKUP_DIR):
        return []
    out = []
    for name in os.listdir(BACKUP_DIR):
        path = os.path.join(BACKUP_DIR, name)
        if not os.path.isfile(path) or not name.endswith((".zip", ".sbdb")):
            continue
        encrypted = is_encrypted_file(path)
        entry = {
            "name": name,
            "bytes": os.path.getsize(path),
            "modified_at": os.path.getmtime(path),
            "encrypted": encrypted,
            "manifest": None,
        }
        if not encrypted:
            try:
                with zipfile.ZipFile(path) as zf:
                    entry["manifest"] = json.loads(zf.read("manifest.json"))
            except (zipfile.BadZipFile, KeyError, json.JSONDecodeError, OSError):
                entry["manifest"] = None
        out.append(entry)
    out.sort(key=lambda e: e["modified_at"], reverse=True)
    return out


def prune_backups(keep=None):
    """Versioning (W2-165): keep the newest N, delete the rest. Returns the
    removed names. Pre-restore safety backups are pruned too -- they're
    ordinary backups, and exempting them would make the retention count
    mean something different from what the setting says."""
    keep = int(keep if keep is not None else get_settings()["keep"])
    backups = list_backups()
    removed = []
    for entry in backups[keep:]:
        try:
            _secure_delete(os.path.join(BACKUP_DIR, entry["name"]))
            removed.append(entry["name"])
        except OSError:
            pass
    return removed


def _secure_delete(path):
    """Overwrite before unlinking (W2-219). Honest limitation: on an SSD
    with wear levelling, or any copy-on-write filesystem, this does NOT
    guarantee the old blocks are gone -- full-disk encryption is the only
    real answer there. It still removes the plaintext from the obvious
    place, which is worth doing."""
    try:
        size = os.path.getsize(path)
        with open(path, "r+b") as f:
            f.write(secrets.token_bytes(size))
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        pass
    os.remove(path)


def delete_backup(name):
    path = _resolve(name)
    _secure_delete(path)
    security_audit.log_event("backup_deleted", "success", source="backup_restore", name=name)
    return {"ok": True, "name": name}


# ---------------------------------------------------------- inspect/verify --
def _open_archive(path, password=None):
    """Returns a ZipFile for `path`, decrypting first if needed."""
    with open(path, "rb") as f:
        blob = f.read()
    if blob.startswith(MAGIC):
        if not password:
            raise PermissionError("this backup is encrypted -- a password is required")
        blob = _decrypt(blob, password)
    return zipfile.ZipFile(io.BytesIO(blob))


def verify_backup(name, password=None):
    """Integrity check before offering a restore (W2-168). Checks, in
    order: readable, decryptable, valid zip, CRCs intact, manifest present,
    and every file's SHA-256 matching what the manifest recorded. The
    per-file hash matters on top of the zip CRC because a CRC is trivially
    recomputable by anyone editing the archive; the manifest hash at least
    has to be edited too, and in an encrypted archive can't be.

    Takes a name inside BACKUP_DIR, never an arbitrary path -- an
    "or an absolute path" convenience here would be a file-read primitive
    reachable from a URL segment."""
    path = _resolve(name)
    result = {"ok": False, "name": os.path.basename(path), "encrypted": is_encrypted_file(path),
              "needs_password": False, "reason": None, "manifest": None, "files_checked": 0}
    try:
        zf = _open_archive(path, password)
    except PermissionError:
        result["needs_password"] = True
        result["reason"] = "encrypted -- password required to verify"
        return result
    except ValueError as e:
        result["reason"] = str(e)
        return result
    except (zipfile.BadZipFile, OSError) as e:
        result["reason"] = f"unreadable archive: {e}"
        return result

    with zf:
        bad = zf.testzip()
        if bad is not None:
            result["reason"] = f"CRC failure in '{bad}'"
            return result
        try:
            manifest = json.loads(zf.read("manifest.json"))
        except (KeyError, json.JSONDecodeError):
            result["reason"] = "manifest.json missing or unreadable"
            return result
        result["manifest"] = manifest
        names = set(zf.namelist())
        for arcname, meta in manifest.get("files", {}).items():
            if arcname not in names:
                result["reason"] = f"file listed in manifest is missing: {arcname}"
                return result
            if _sha256_bytes(zf.read(arcname)) != meta.get("sha256"):
                result["reason"] = f"checksum mismatch: {arcname}"
                return result
            result["files_checked"] += 1

    result["ok"] = True
    return result


def diff_backup(name, password=None):
    """What a full restore would actually change (W2-171). Credential
    values are never returned -- a device whose local_key differs is
    reported as `local_key_changed: true` and nothing more, so the diff view
    can't become the leak the redaction elsewhere prevents."""
    path = _resolve(name)
    with _open_archive(path, password) as zf:
        if "config.json" not in zf.namelist():
            return {"config_in_backup": False}
        backup_cfg = json.loads(zf.read("config.json"))

    current_cfg = {}
    if os.path.isfile(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                current_cfg = json.load(f)
        except (json.JSONDecodeError, OSError):
            current_cfg = {}

    cur_devices = {d["id"]: d for d in current_cfg.get("devices", []) if "id" in d}
    bak_devices = {d["id"]: d for d in backup_cfg.get("devices", []) if "id" in d}
    changed = []
    for dev_id in sorted(set(cur_devices) & set(bak_devices)):
        cur, bak = cur_devices[dev_id], bak_devices[dev_id]
        fields = sorted({k for k in set(cur) | set(bak)
                         if k != "local_key" and cur.get(k) != bak.get(k)})
        key_changed = cur.get("local_key") != bak.get("local_key")
        if fields or key_changed:
            changed.append({"id": dev_id, "fields_changed": fields,
                            "local_key_changed": key_changed})

    def _collection_diff(key):
        cur_ids = {g.get("id") for g in current_cfg.get(key, [])}
        bak_ids = {g.get("id") for g in backup_cfg.get(key, [])}
        return {"added": sorted(bak_ids - cur_ids), "removed": sorted(cur_ids - bak_ids)}

    return {
        "config_in_backup": True,
        "devices": {
            "added": sorted(set(bak_devices) - set(cur_devices)),
            "removed": sorted(set(cur_devices) - set(bak_devices)),
            "changed": changed,
        },
        "groups": _collection_diff("groups"),
        "zones": _collection_diff("zones"),
        "orchestration_presets": _collection_diff("orchestration_presets"),
        "remote_access": "unchanged by any restore -- see docs/backup-restore.md",
    }


# -------------------------------------------------------------- restore ---
def _remote_access_enabled():
    """Read the flag straight off disk rather than via remote_auth, so this
    check stays honest even if the module's in-process state were somehow
    out of step with the file a restore might have touched."""
    if not os.path.isfile(REMOTE_AUTH_PATH):
        return None
    try:
        with open(REMOTE_AUTH_PATH, "r", encoding="utf-8") as f:
            return json.load(f).get("enabled", False)
    except (json.JSONDecodeError, OSError):
        return None


def _write_data_file(arcname, data):
    rel = arcname[len("data/"):]
    dest = os.path.normpath(os.path.join(DATA_DIR, rel))
    # Zip-slip guard: an archive is untrusted input even when the user
    # supplied it, and `data/../../evil` would otherwise escape DATA_DIR.
    if os.path.commonpath([os.path.abspath(dest), os.path.abspath(DATA_DIR)]) != os.path.abspath(DATA_DIR):
        raise ValueError(f"refusing to write outside data/: {arcname}")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "wb") as f:
        f.write(data)
    return os.path.relpath(dest, BACKEND_DIR).replace(os.sep, "/")


def restore_backup(name, password=None, confirm=False, sections=None, safety_backup=True):
    """Restore from `name`.

    `confirm` must be True: this overwrites live config and data, and a
    default-safe API where the destructive call needs an explicit flag is
    the difference between "are you sure?" being a real gate and being a
    dialog someone clicked past. `sections=None` means a full restore;
    otherwise only the named SECTIONS are applied, which is how you get
    favorites/schedules back without touching device credentials.

    Always integrity-checks first, and always takes a pre-restore safety
    backup (W2-172) before writing anything -- a restore is itself a
    destructive operation and deserves the same undo it provides.
    """
    if sections is not None:
        unknown = set(sections) - set(SECTIONS)
        if unknown:
            raise ValueError(f"unknown section(s): {sorted(unknown)}")
        if not sections:
            raise ValueError("no sections selected")

    verification = verify_backup(name, password)
    if not verification["ok"]:
        security_audit.log_event("backup_restore_rejected", "failure",
                                 source="backup_restore", name=name,
                                 reason=verification["reason"])
        if verification["needs_password"]:
            raise PermissionError(verification["reason"])
        raise ValueError(f"backup failed its integrity check: {verification['reason']}")

    if not confirm:
        raise PermissionError(
            "restore overwrites current configuration and data -- call again with "
            "confirm=true once the caller has actually confirmed"
        )

    enabled_before = _remote_access_enabled()

    safety = None
    if safety_backup:
        # Unencrypted deliberately: the point is to be restorable in the
        # minute after a bad restore, and a password nobody wrote down
        # makes that impossible. It lands in the same directory, which the
        # docs tell you to keep on an encrypted disk.
        safety = create_backup(note=f"pre-restore safety backup (before {name})", prune=False)

    path = _resolve(name)
    restored_files = []
    restored_config_keys = []
    with _open_archive(path, password) as zf:
        names = set(zf.namelist())

        if sections is None:
            if "config.json" in names:
                with open(CONFIG_PATH, "wb") as f:
                    f.write(zf.read("config.json"))
                restored_files.append("config.json")
                restored_config_keys = ["*"]
            for arcname in sorted(n for n in names if n.startswith("data/")):
                restored_files.append(_write_data_file(arcname, zf.read(arcname)))
        else:
            wanted_config_keys = []
            wanted_data = []
            for section in sections:
                wanted_config_keys.extend(SECTIONS[section]["config_keys"])
                wanted_data.extend(SECTIONS[section]["data_files"])

            if wanted_config_keys and "config.json" in names:
                backup_cfg = json.loads(zf.read("config.json"))
                current_cfg = {}
                if os.path.isfile(CONFIG_PATH):
                    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                        current_cfg = json.load(f)
                for key in wanted_config_keys:
                    if key in backup_cfg:
                        current_cfg[key] = backup_cfg[key]
                        restored_config_keys.append(key)
                with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                    json.dump(current_cfg, f, indent=2)
                if restored_config_keys:
                    restored_files.append("config.json")

            for item in wanted_data:
                exact = f"data/{item}"
                matches = [n for n in names
                           if n == exact or n.startswith(exact + "/")]
                for arcname in sorted(matches):
                    restored_files.append(_write_data_file(arcname, zf.read(arcname)))

    enabled_after = _remote_access_enabled()
    touched_credentials = sections is None or any(
        SECTIONS[s]["touches_credentials"] for s in (sections or []))

    security_audit.log_event(
        "backup_restored", "success", source="backup_restore", name=name,
        sections=sorted(sections) if sections else "all",
        files=len(restored_files), touched_credentials=touched_credentials,
    )

    return {
        "ok": True,
        "name": name,
        "sections": sorted(sections) if sections else "all",
        "restored_files": restored_files,
        "restored_config_keys": restored_config_keys,
        "touched_device_credentials": touched_credentials,
        "safety_backup": safety["name"] if safety else None,
        # W2-175, made observable: a restore never writes remote_auth.json,
        # so these two must always match. The UI prints it.
        "remote_access": {
            "enabled_before": enabled_before,
            "enabled_after": enabled_after,
            "changed": enabled_before != enabled_after,
        },
    }


def restore_preflight(name, password=None):
    """Everything a UI needs to show before asking "are you sure?":
    integrity result, what would change, and which sections are available."""
    verification = verify_backup(name, password)
    out = {"verification": verification, "sections": [
        dict(SECTIONS[key], id=key) for key in SECTIONS]}
    if verification["ok"]:
        try:
            out["diff"] = diff_backup(name, password)
        except (ValueError, PermissionError, KeyError, json.JSONDecodeError) as e:
            out["diff"] = {"error": str(e)}
    return out


def export_bytes(name):
    """Raw archive bytes for a download response. Deliberately does not
    decrypt: 'download' means the file as stored."""
    path = _resolve(name)
    with open(path, "rb") as f:
        return f.read(), os.path.basename(path)
