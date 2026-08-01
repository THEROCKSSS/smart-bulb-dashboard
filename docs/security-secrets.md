# Secrets, the Security Log, and What To Do When Something Looks Wrong

Companion to [remote-access-security.md](remote-access-security.md), which
covers exposing the dashboard beyond your LAN. This one covers the secrets
this app holds, where they live, and the audit trail that tells you whether
anything has happened to them.

## The four secrets, and what each one is actually worth

The same table is served live at `GET /api/security/secrets` (values never
included), so it can't drift away from the code.

| Secret | Where it lives | What an attacker gets with it | Rotate by |
|---|---|---|---|
| **Device `local_key`** | `backend/config.json`, or the environment | Full local control of that bulb from your LAN. **Permanent** — there is no revocation short of re-pairing the bulb. | Re-pair the bulb in the Tuya/Smart Life app, read the new key, update config (see [SETUP.md](../SETUP.md)) |
| **Remote-access PIN** | Nowhere. Only a PBKDF2-SHA256 hash (200,000 iterations) + a 128-bit salt in `backend/data/remote_auth.json` | Full dashboard and API access, when the PIN gate is on | Settings → Remote Access → re-enable with a new PIN |
| **Session signing key** | `backend/data/remote_auth.json`, 256 bits from `secrets.token_hex(32)` | Forge a valid session cookie and skip the PIN entirely | `POST /api/auth/sessions/revoke-all` — rotates the key *and* logs everyone out |
| **Audit chain key** | `backend/data/security_audit_key`, 0600 where the OS honours it | Forge security-log entries that pass verification | Delete it together with `security_audit_state.json`, then re-verify. This starts a **new** chain — history before that point is no longer verifiable, so only do it deliberately |

Ranking, if you only have time to protect one thing: the **session signing
key** is the highest-leverage (it bypasses auth entirely), then the
**PIN**, then each **`local_key`** (high impact, but bounded to one bulb
and to attackers already on your LAN).

## Keeping `local_key`s out of `config.json`

`config.json` is git-ignored, but it's still a plaintext credential file
sitting next to the code. If you'd rather it wasn't:

1. Copy `.env.example` to `.env` at the repo root.
2. For each bulb, add `SBD_LOCAL_KEY_<ID>` — the device's `id` from
   `config.json`, uppercased, with every non-alphanumeric character
   replaced by `_`. `bulb-1` → `SBD_LOCAL_KEY_BULB_1`.
3. Set that device's `"local_key"` to `""` in `config.json`.
4. Restart the backend.

The value is applied at load time and is **never written back**, including
when you rename the bulb or change any other setting through the dashboard.
That round-trip is the trap this design exists to avoid, and
`test_secrets.py::test_env_sourced_key_is_never_written_back_into_config_json`
pins it.

A real exported environment variable always beats the `.env` file, so
Docker/systemd users can skip the file entirely and pass the same names via
`environment:` / `Environment=`.

Settings → Security shows which source each device's key came from.

### What is deliberately not env-configurable

The **PIN** (it's only ever stored hashed) and the **session signing key**.
The signing key has to be rotatable at runtime — revoke-all-sessions
rotates it — and a value pinned in the environment couldn't be rotated by
the process that depends on it. Sourcing it from env would quietly turn
"log everyone out" into a partial control.

## Redaction: what is guaranteed

`local_key` values never appear in:

- any API response (`GET /api/devices` masks them; every other endpoint is
  swept by a test that walks the real route table, so a *new* endpoint that
  leaks one fails the suite without anyone remembering to add it)
- the per-device history log
- either audit log
- error messages, including exception text produced by tinytuya or the
  socket layer — `BulbController._safe_error()` scrubs the device's own key
  out of anything on its way to a client
- the backup diff view, which reports `local_key_changed: true` and nothing
  more

The PIN and the session signing key never appear in any log at any level.
There is no debug logging mode that changes this — the audit writers take
explicit fields, not a dump of local variables.

## The security log

`backend/data/security_events.log`, one JSON object per line. Distinct from:

- **History** (per-device actions — what the bulbs did)
- `backend/data/auth_audit.log` (the original auth-only trail, unchanged;
  every line it writes is forwarded here too, with severity and chaining
  added)

### Severity

`info` → `notice` → `warning` → `critical`. The defaults are tuned so
ordinary daily use produces nothing above `notice`:

| Severity | Examples |
|---|---|
| `info` | login success, logout, config file written, backup created |
| `notice` | a single wrong PIN, a session revoked, a device removed, PIN gate enabled |
| `warning` | lockout triggered, rate limit hit, **a device added**, a backup restored, 3 failed logins inside 5 minutes |
| `critical` | **the PIN gate being disabled**, audit-log tampering detected |

Alerts fire at `warning` and above. That line is the whole point: an alert
you see every day is an alert you stop reading. Both thresholds are
configurable in Settings → Security Log.

### Alerting

- **Local only (default)** — alerts go into a queue the dashboard reads,
  and can raise a browser notification. Nothing leaves the machine.
- **Webhook (off by default)** — `POST`s the alert as JSON. This is also
  the hook for shipping events off-box, which is the honest fix for the
  tamper-evidence limitation below.

`POST /api/security/self-test` writes a canary event, re-verifies the
chain, and reports what alerting is actually wired to — run it from cron
if you want a periodic "still working" heartbeat, and
`GET /api/security/digest?days=7` for a summary that reports even a quiet
week (silence that's ambiguous between "nothing happened" and "it broke"
is worthless).

### Tamper-evidence, and its limits

Each line carries `prev` (the previous line's HMAC) and its own HMAC. A
separate state file records the head. Together that detects an entry
edited in place, an entry removed from the middle, entries removed from the
end, and the whole file deleted. Verified against a running server for all
four shapes.

**It does not defend against an attacker who has both the key file and the
state file** — they can rebuild a consistent forged chain. That's inherent
to keeping the anchor on the same host. If you need more, point the alert
webhook at something off-box; a line that already left the machine can't be
retroactively edited on it.

Rotation and retention never break the chain: rotating writes an in-chain
marker into the new segment, and retention pruning an *old* segment is
reported as `complete: false` with `ok: true`, distinct from tampering.

## Incident-response checklist

If Settings → Security Log shows something you didn't do:

1. **Don't clear the log.** If the chain is broken, the broken chain is the
   evidence. Export it first (`Export JSON` keeps the `prev`/`hmac` fields,
   which is what makes an export independently verifiable).
2. **Cut access.** `POST /api/auth/sessions/revoke-all` — logs everyone out
   *and* rotates the session signing key, so a forged cookie dies too.
3. **Work out the blast radius** from the event type:
   - `remote_auth_disabled` you didn't do → someone had an authenticated
     session. Assume the PIN is known. Re-enable with a **new** PIN.
   - `device_added` you didn't do → someone had API access. Check
     `GET /api/devices` for a device you don't recognise.
   - `login_lockout` / `login_failure_threshold` → someone is guessing.
     If you're exposed via DuckDNS rather than Tailscale, this is the
     signal to stop being exposed.
   - `backup_created` you didn't do → assume every `local_key` is now
     known. Those keys can only be changed by re-pairing each bulb.
4. **Rotate what's implicated**, using the table at the top.
5. **Check what got in.** If the dashboard is reachable from the internet,
   re-read [remote-access-security.md](remote-access-security.md) — the
   Tailscale path exists precisely so this doesn't need to happen.

## Secure deletion of old backups

Deleting a backup through the dashboard overwrites the file with random
bytes before unlinking. Be clear about what that does and doesn't buy: on
an SSD with wear levelling, or any copy-on-write filesystem, the old blocks
may survive regardless. **Full-disk encryption is the only real answer**;
the overwrite removes the plaintext from the obvious place, which is worth
doing but is not a guarantee.

## Preventing a committed secret

`.gitignore` covers `backend/config.json`, `backend/data/`,
`backend/backups/` and `.env` — but `.gitignore` is a default, not a
control. `git add -f` bypasses it silently, and a file tracked before the
rule existed stays tracked forever.

`.github/workflows/secret-scan.yml` runs `.github/scripts/scan_secrets.py`
on every push and PR. It fails the build if any of those paths is actually
tracked, or if a real-looking `local_key` / 64-hex signing key is pasted
into a normal file. Run it locally any time:

```bash
python .github/scripts/scan_secrets.py            # tracked files
python .github/scripts/scan_secrets.py --staged   # pre-commit hook use
```

False positive? Put the word `nosecret` in a comment on that line. A waiver
is visible in review, which is the point — a scanner nobody can silence
gets deleted instead.

**If a real secret was committed:** removing it in a later commit is not
enough, it stays in history. Rotate the credential first (for a
`local_key`, that means re-pairing the bulb), then rewrite the history.

## Recurring review (quarterly)

Worth a diary entry, not automation:

- Re-read this table — has a new secret been added without a row?
- Rotate the session signing key (`revoke-all-sessions` does both).
- `POST /api/security/self-test`, confirm alerting still reports what you
  expect.
- Restore a backup onto a scratch copy. A backup nobody has ever restored
  from is not a backup — see [backup-restore.md](backup-restore.md).
- Check `GET /api/devices` for anything you don't recognise.
