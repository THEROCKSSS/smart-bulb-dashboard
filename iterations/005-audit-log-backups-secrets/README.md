# 005 — Security audit log, backup/restore, secrets management

Week 2 Phase C of the roadmap (`roadmap/week-2-remote-access-and-security.md`
sections 8, 9 and 12 — W2-141–160, W2-161–175, W2-211–225). Tracking issue
THEROCKSSS/smart-bulb-dashboard#71.

## Goal

Three things that share one theme — *knowing what happened to this install,
and being able to get it back*:

1. A security-events log distinct from the per-device action history, with
   severity, retention, alerting, export, a search UI, and tamper-evidence.
2. Backup/restore, with encryption, integrity checking, selective restore,
   and a hard guarantee that a restore can never change remote-access state.
3. Secrets: env-var/`.env` support, a systematic redaction audit with tests
   that would catch a regression, and a CI secret scan.

## Approach

### Not duplicating what already existed

`remote_auth.py` already wrote `data/auth_audit.log` — one JSON line per
auth event, deliberately never containing the PIN. The obvious wrong move
was a second, parallel logger with its own call sites, which would drift:
someone adds a new auth event to one and forgets the other.

Instead `remote_auth.log_audit_event()` now *forwards* every line it writes
into `security_audit.log_event()`, which adds severity, alerting and
chaining on top. The original file is untouched (it's what iteration 004
verified against). One call site, two records, no way to update one and
miss the other.

Two genuine gaps turned up while doing this: **enabling and disabling the
PIN gate weren't audited at all**. Disabling it is the single most
security-relevant config change this app has — it takes a remotely-exposed
dashboard from PIN-protected to wide open — and it left no trace. Both now
log, `disable` at `critical`.

### Tamper-evidence, and being honest about its ceiling

Each line carries `prev` (the previous line's HMAC) and its own HMAC, keyed
from `data/security_audit_key`.

A hash chain alone catches an edited or removed *middle* entry, but **not**
truncation — chop the last five lines and what remains is internally
consistent. So the head (last seq + last HMAC) is recorded in a separate
`security_audit_state.json`, and `verify_chain()` compares the file's tail
against it. That's what catches truncation and outright deletion.

What it does **not** stop: an attacker with both the key file and the state
file can rebuild a consistent forged chain. That's inherent to keeping the
anchor on the same host. Rather than dress it up, the module docstring, the
docs and the UI all say so, and the alert webhook is named as the honest
off-box upgrade path — a line that already left the machine can't be
retroactively edited on it.

### Alert fatigue as a design constraint, not a nice-to-have

W2-156 asks for defaults where legitimate daily use doesn't generate noise.
That's easy to *claim* and easy to regress, so it's a test:
`test_ordinary_daily_use_raises_no_alerts` logs in, changes config, mistypes
a PIN once, takes a backup and logs out, then asserts the alert queue is
empty. Promote any of those to `warning` in future and the test fails, which
forces the tradeoff to be made deliberately.

A single wrong PIN is `notice` — one typo is not an incident. A *run* of
them is caught by a separate rate rule (3 in 5 minutes → one aggregated
alert, not three).

### Backups: making the tradeoff unavoidable rather than documented

A backup contains every bulb's `local_key` in plaintext, and that key is
permanent — there's no revocation short of re-pairing the bulb. Writing "you
should encrypt your backups" in a doc doesn't help: by the time someone
reads the doc, the plaintext archive is in their Downloads folder.

So: `create_backup(password=None)` still works, but the API response
carries an explicit `warning` field whenever it produced a plaintext
archive, and the dashboard requires ticking an acknowledgement before the
button does anything. The backup list marks unencrypted archives with a red
`NO — plaintext keys` tag rather than a neutral "no".

### W2-175 as structure, not a rule

"A restore must never silently flip remote access on or off." A rule
someone has to remember is a rule a refactor breaks. Instead
`remote_auth.json` is on a hard-excluded list — it is never put into an
archive and never written by a restore — so there is no code path that
could flip it. The restore result still reads the flag before and after and
reports both, so the guarantee is observable rather than asserted.

Same reasoning excludes the security log, its state file and its key:
restoring those would rewind the tamper-evident trail, which is exactly the
"compromise quietly erases its own trail" move the chain exists to prevent.

## What happened

Mostly worked as designed. Three things didn't.

## Failures

### 1. A real (unexploited) `local_key` leak path in `bulb_manager.status()`

Writing the redaction-audit tests meant asking "what would a regression
actually look like?" The answer was a `_LeakyDevice` test double whose
exception text contains the key it was constructed with:

```python
raise RuntimeError(f"handshake failed for key={self.local_key} at 10.0.0.11")
```

That test failed against the code as it stood. `status()` did:

```python
except Exception as e:
    self._last_error = str(e)
    self._log("status", ok=False, error=str(e))
    return {"online": False, "error": str(e)}
```

`str(e)` is text this codebase does not control — it comes from tinytuya or
the socket layer under it. If any version of tinytuya ever echoed the key it
was constructed with, that string went **straight into an API response and
into the per-device history log**, with nothing else in the app to catch
it. No current tinytuya version does; that's luck, not a control.

**Fix:** `BulbController._safe_error()` — every exception string that leaves
the class is scrubbed of that device's own key first (`bulb_manager.py`).
The error-path `raw` payload gets the same treatment via `_safe_raw()`, and
`_log()` scrubs as a backstop so any *future* caller is covered too.
Deliberately passes the device's key explicitly and skips the config read,
so it stays cheap enough for per-frame error paths.

### 2. Rotation looked like tampering

`test_rotation_writes_a_marker_and_keeps_the_chain_verifiable` failed on the
first run. Two separate causes:

- Rotating left the new segment **empty**, so the state file said "10
  entries" while the current file had none — indistinguishable from someone
  wiping the log. Fix: rotation writes an `audit_log_rotated` marker as the
  first line of the new segment, keeping the chain continuous *and* keeping
  the state's head pointing into the current file.
- The test itself then failed differently: 20 events at an 800-byte rotation
  threshold produced ~7 rotations against a `rotate_keep` of 5, so the
  oldest segments were legitimately pruned and `complete` came back False.
  That's correct behaviour and a badly-calibrated test — split into three
  tests instead: chain stays verifiable across rotation, *oldest* segment
  gone reads as `ok=True, complete=False` (housekeeping), and a segment
  removed from the *middle* reads as tampering. Conflating pruning with
  tampering would train the operator to ignore the result.

### 3. A path-traversal hole in `verify_backup()`, found by its own test

`test_backup_names_cannot_escape_the_backup_directory` didn't raise on
`/etc/passwd`. Cause: `verify_backup()` accepted "a name **or** an absolute
path" as a convenience, and `os.path.isabs("/etc/passwd")` is True on
Windows too, so it bypassed `_resolve()` entirely and tried to open the
path directly. Not reachable through the API (a FastAPI path segment can't
contain `/`), but it was a file-read primitive one refactor away from a URL.
Fix: the convenience is gone — `verify_backup()` takes a name inside
`BACKUP_DIR`, full stop.

### 4. Two pre-existing problems found in passing

- **Latent deadlock in `config.load_config()`**: it called `save_config()`
  while already holding a non-reentrant `threading.Lock`. Only reachable
  when *both* `config.json` and `config.example.json` are missing — which
  never happens in a checkout, since the example is committed — so it had
  never fired. Split out a `_write()` helper that doesn't re-take the lock.
- **Test-isolation gap**: the suite was writing to the *real*
  `backend/data/auth_audit.log`. `conftest.auth_reset` redirected the auth
  *state* file but not its log. Found by looking at what `backend/data/`
  actually contained after a full run rather than trusting the fixture's
  docstring. Fixed. The audio modules (`audio_safety`,
  `audio_session_presets`, `audio_last_session`) still have the same
  problem — noted in `HANDOFF.md`, out of scope here.

## Fix

New: `backend/security_audit.py`, `backend/backup_restore.py`,
`backend/secrets_env.py`, 19 API routes in `backend/main.py`, two dashboard
tabs (System → Security Log, System → Backup),
`.github/scripts/scan_secrets.py` + its workflow, `.env.example`,
`docs/security-secrets.md`, `docs/backup-restore.md`.

Changed: `config.py` (env overrides, change-tracking, device-added events,
the lock fix), `remote_auth.py` (forwarding + enable/disable auditing),
`bulb_manager.py` (error scrubbing), `.gitignore`, `requirements.txt`
(`cryptography` declared explicitly — it was already there transitively via
tinytuya, and depending on another package's dependency is how a feature
breaks on an unrelated bump).

## Verification

**481/481 backend tests pass** (353 before this iteration, 128 new). CLI
suite unchanged at 22/22.

Beyond the suite, against a real `uvicorn` on `127.0.0.1:8577` with a
throwaway config:

**Backups** — plain and encrypted create → verify → preflight → restore all
returned 200. A wrong password reports `wrong password, or the archive has
been modified`, identical to a tampered archive (distinguishing them would
be an oracle). Restore without `confirm` → 409. Checked the archives on
disk: the plain one's *decompressed* `config.json` really does contain
`LiveTestKey12345` (so the warning isn't theatre), and the encrypted one
isn't a readable zip at all (`BadZipFile`).

**W2-175, live** — enabled the PIN gate, logged in, then restored an archive
taken while the gate was *disabled*:

```
remote auth enabled BEFORE restore: True
remote_access block: {'enabled_before': True, 'enabled_after': True, 'changed': False}
remote auth enabled AFTER restore: True
```

Also confirmed the reverse direction in `test_restore_never_changes_remote_access_state`.
Incidentally confirmed that once the gate is on, `/api/system/remote-auth/disable`
is itself gated (401 without a session) — correct, and now pinned by a test.

**Tamper detection, against the running server**, all three shapes:

```
before tamper: {"ok":true,"complete":true,"entries":10,...}
after edit   : {"ok":false,"first_bad_seq":4,"reason":"hmac mismatch at seq 4 (entry altered)"}
after delete : {"ok":false,"first_bad_seq":5,"reason":"broken link at seq 5 (entry removed)"}
after wipe   : {"ok":false,"reason":"log is empty but state records 10 entries (file deleted or truncated)"}
```

**Leak sweep** over the entire live security log, the alert queue and the
CSV export, for the test `local_key`, the test PIN and the backup password:
all three absent.

**Severity/alerting** — disabling the gate produced a `critical` event and
an alert; `backup_restored` produced `warning`; `login_success` and
`backup_created` stayed `info` and raised nothing.

**Secret scanner** — `python .github/scripts/scan_secrets.py` reports
`secret scan: clean` against this repo, and returns exit 1 with the right
message for a planted `backend/config.json`, a real-looking key pasted into
a markdown file, and a committed `backend/data/`.

## Not built, deliberately

- **No scheduler.** No automatic backup timer, no periodic digest job —
  rather than add another background thread. `POST /api/security/self-test`
  and `POST /api/backups` are cron-shaped and documented as such.
- **No backup upload endpoint** (W2-168 adjacent) — needs
  `python-multipart`, and a new dependency wasn't worth it. Drop the file
  into `backend/backups/` instead.
- **`keyring` (W2-212) and encrypted-at-rest `config.json` (W2-213)** — env
  vars cover the same threat with no new dependency, and the encrypted
  config path would need a passphrase prompt at every start, which breaks
  unattended restart.
- **Discord alerting (W2-148)** is Week 3's; the webhook is the seam it
  plugs into.
- **Correlation view (W2-151)** — lining a security event up against what
  the bulbs were doing needs the per-device history to be persistent, and
  it's currently an in-memory deque that resets on restart. Building the
  view on top of data that vanishes on restart would be a view that lies.
