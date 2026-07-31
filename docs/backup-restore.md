# Backup & Restore

Settings → System → **Backup**, or the API below.

## Read this first

**A backup contains every bulb's `local_key` in plaintext.** That key is
permanent local control of the bulb for anyone on your network, and it
cannot be revoked — the only way to change it is to re-pair the bulb in the
Tuya/Smart Life app. An unencrypted archive is therefore a portable,
permanent credential file.

The dashboard asks you to acknowledge this before it will make an
unencrypted one, and the API returns an explicit `warning` field rather
than staying silent about it. Unencrypted is a legitimate choice if the
file stays on an encrypted disk you control. It is a bad choice for a
synced folder, a NAS share, or an email attachment.

## What's in an archive

| Included | Not included, ever |
|---|---|
| `backend/config.json` (devices, groups, zones, orchestration presets, audio calibrations) | `data/remote_auth.json` — PIN hash, salt, session signing key, live sessions |
| `data/favorites.json`, `data/schedules.json` | `data/security_events.log` and its rotated segments |
| `data/lightshows/`, `data/audio_*.json`, `data/discovery.json` | `data/security_audit_key`, `data/security_audit_state.json` |
| `manifest.json` — per-file SHA-256, timestamps, app version | `data/security_alerts.json`, `backend/backups/` itself |
| `remote_auth_settings.json` — non-secret settings only, **informational, never applied** | |

Two reasons for the exclusions, both load-bearing:

- **Auth state.** An offline attacker with the PIN hash can grind a short
  numeric PIN regardless of PBKDF2, so shipping it in an archive that might
  be unencrypted is a real downgrade of the PIN gate. And keeping it out is
  what makes the guarantee below structural rather than a rule someone has
  to remember.
- **The audit chain.** Restoring the security log would rewind the
  tamper-evident trail — precisely the "compromise quietly erases its own
  trail" move the chain exists to prevent. Migrating it is a deliberate
  manual copy, never a side effect of a restore.

## A restore never changes remote access

**A restore cannot turn the PIN gate on, or off.** Not "shouldn't" —
there is no code path in `backup_restore.py` that writes
`remote_auth.json`. The restore result reports `enabled_before` and
`enabled_after` so you can see it rather than take it on faith, the
dashboard prints it, and
`test_backup_restore.py::test_restore_never_changes_remote_access_state`
pins both directions.

Consequence for a migration: the new host will have **no PIN set**. Set one
there explicitly (Settings → Remote Access) before exposing it.

## Restoring

The flow is deliberately two-step; nothing is written until:

1. the archive passes its **integrity check** (zip CRCs plus every file's
   SHA-256 against the manifest — a CRC alone is trivially recomputable by
   anyone editing the archive),
2. you've seen the **diff** of what would change (device/group additions,
   removals and changes; key *values* are never shown, only whether they
   differ),
3. you tick the **overwrite confirmation**.

A **pre-restore safety backup** of the current state is taken automatically
first, so a bad restore is undoable — note its filename, which the result
panel prints.

### Selective restore

Bring back part of a backup without touching device credentials:

| Section | Restores |
|---|---|
| `favorites` | `data/favorites.json` |
| `schedules` | `data/schedules.json` |
| `audio` | audio session presets, last session, safety settings, input calibrations |
| `lightshows` | recorded lightshows |
| `discovery` | network discovery state |
| `groups_zones` | groups, zones, orchestration presets — merged into the current config |
| `devices` | **the only section that overwrites `local_key`s** |

`groups_zones` merges rather than replacing the file, so a device added
since the backup was taken survives.

## Versioning and destination

The newest N backups are kept (default 10, configurable); older ones are
deleted, overwritten with random bytes first. Pre-restore safety backups
count toward N — they're ordinary backups, and exempting them would make
the retention number mean something other than what it says.

Destination defaults to `backend/backups/` and moves with `SBD_BACKUP_DIR`
(see `.env.example`). Pointing it at an encrypted external volume is the
better posture.

There is **no built-in scheduler** — deliberately, rather than adding
another background thread. Use cron / Task Scheduler:

```bash
curl -s -X POST http://localhost:8500/api/backups \
  -H 'Content-Type: application/json' \
  -d '{"password":"'"$SBD_BACKUP_PASSWORD"$'","note":"nightly"}'
```

(Read the password from the environment as above rather than inlining it,
or it lands in your shell history and the process list.)

## Encryption format

`SBDBACKUP1\n` + a JSON header line + AES-256-GCM ciphertext of the same
zip bytes. The key is PBKDF2-HMAC-SHA256, 200,000 iterations (matching the
PIN hashing, so there's one number to reason about), 128-bit salt, 96-bit
nonce. The header is passed as AEAD associated data, so its parameters
can't be swapped for weaker ones without failing authentication.

The header is readable without the password, which is why `verify` can say
"this is a valid encrypted archive, it needs a password" instead of
"corrupt". A wrong password and a modified archive are reported
identically — they fail the same check, and distinguishing them would be an
oracle.

**There is no password recovery.** Lose it and the archive is gone.

## Migrating to a new host

1. Take an **encrypted** backup on the old host and copy it across.
2. Install the dashboard on the new host (see [SETUP.md](../SETUP.md)) and
   let it start once so `backend/data/` exists.
3. Put the archive in `backend/backups/` on the new host.
4. Settings → System → Backup → **Restore…**, enter the password, review
   the diff, confirm.
5. **Set a PIN on the new host** if you want the gate — it did not come
   across, by design.
6. Optionally copy `data/security_events.log`, `security_audit_state.json`
   and `security_audit_key` across by hand if you want the audit history to
   continue. All three, or none — the chain won't verify otherwise.
7. Delete the archive from the new host, or move it somewhere encrypted.

## Test your restore

A backup nobody has ever restored from isn't trustworthy. Once a quarter:

```bash
# 1. take one
curl -s -X POST localhost:8500/api/backups -H 'Content-Type: application/json' \
     -d '{"password":"test-only"}'

# 2. check it (password in the body, never a query string --
#    query strings land in access logs and browser history)
curl -s -X POST localhost:8500/api/backups/<name>/verify \
     -H 'Content-Type: application/json' -d '{"password":"test-only"}'

# 3. see what it would change
curl -s -X POST localhost:8500/api/backups/<name>/preflight \
     -H 'Content-Type: application/json' -d '{"password":"test-only"}'

# 4. restore it, then confirm remote_access.changed == false
curl -s -X POST localhost:8500/api/backups/<name>/restore \
     -H 'Content-Type: application/json' \
     -d '{"password":"test-only","confirm":true}'
```

Then delete the test archive — it holds your real keys.

## Secure deletion

Deleting through the dashboard or `DELETE /api/backups/{name}` overwrites
the file with random bytes before unlinking. On an SSD with wear levelling
or a copy-on-write filesystem this does **not** guarantee the old blocks
are gone; full-disk encryption is the only real answer. See
[security-secrets.md](security-secrets.md).
