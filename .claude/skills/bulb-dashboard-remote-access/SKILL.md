---
name: bulb-dashboard-remote-access
description: "Set up safe remote access to the Smart Bulb Dashboard beyond the LAN: Tailscale (recommended) or DuckDNS+PIN gate, and how to test the PIN gate."
---

# Smart Bulb Dashboard — Remote Access & PIN Gate

## When to use
User wants to reach the dashboard from outside their home network, or
wants to enable/test/disable the PIN gate. Read
`docs/remote-access-security.md` for the full threat-model reasoning —
this skill is the condensed action version.

## The procedure

### Recommended path: Tailscale
No dashboard config needed. Install Tailscale on the host machine and the
remote device, sign into the same account on both, then reach
`http://<tailscale-ip>:8500` directly — Tailscale's own device auth is the
access control here.

### If exposing publicly via DuckDNS + port forward
**Enable the PIN gate first, always:**
```bash
curl -X POST http://localhost:8500/api/system/remote-auth/enable \
  -H "Content-Type: application/json" -d '{"pin": "<a real pin, not a default>"}'
```
Then set up DuckDNS (dynamic DNS → your public IP) and forward a
**non-default** external port on the router to this machine's LAN
IP:8500. Full steps in `docs/remote-access-security.md`.

### Using the PIN gate
```bash
curl -c cookies.txt -X POST http://localhost:8500/api/auth/login \
  -H "Content-Type: application/json" -d '{"pin": "<the pin>"}'
# subsequent requests need the cookie:
curl -b cookies.txt http://localhost:8500/api/devices

curl -X POST http://localhost:8500/api/auth/logout
curl http://localhost:8500/api/auth/status   # {"enabled": bool, "authenticated": bool}
curl -X POST http://localhost:8500/api/system/remote-auth/disable
```

## Pitfalls

1. **Don't leave a test/default PIN active** (`test1234`, `1234`, etc. were
   used during this project's own testing — see `iterations/004`). Rotate
   to a real PIN before actually exposing anything.
2. **5 wrong attempts from one client locks that IP out for 5 minutes —
   including the correct PIN afterward.** This is intentional (a lockout
   that only blocks wrong guesses isn't a real lockout) — don't "fix" this
   by allowing correct PINs through during lockout, that defeats the point.
3. **This is plaintext HTTP, not HTTPS**, once forwarded publicly. The PIN
   and session cookie are both visible to anything positioned on that
   traffic path. Don't treat the PIN gate as sufficient on its own for a
   truly adversarial threat model — see the doc's TLS/reverse-proxy note.
4. **The root page `/` must stay in `remote_auth.OPEN_PATHS`** — gating it
   was a real bug found during development (the PIN entry page itself
   couldn't load, a total lockout). If modifying `remote_auth.py`, don't
   remove `/` from the open list without understanding why it's there.
5. **In-memory lockout state resets on backend restart.** Don't rely on it
   surviving a restart as a permanent record of who's locked out.
6. **Once the gate is enabled, `/api/system/remote-auth/disable` is itself
   gated** — you need a valid session to turn the gate off. That's correct,
   but it surprises people mid-script: log in first.

## After enabling: the security log

Enabling/disabling the gate, every login, every lockout and every rate-limit
trip is recorded in `backend/data/security_events.log` (dashboard: System →
Security Log), on top of the existing `data/auth_audit.log`. Disabling the
gate is logged `critical` and raises an alert, since it takes a
remotely-exposed dashboard from PIN-protected to wide open.

```bash
curl "http://localhost:8500/api/security/events?min_severity=warning"
curl http://localhost:8500/api/security/verify   # tamper check on the log itself
curl http://localhost:8500/api/security/alerts
```

If the tamper check ever fails, **don't clear the log** — the broken chain
is the evidence. `docs/security-secrets.md` has the incident-response
checklist and the secret-by-secret rotation table.

**A restore never changes remote-access state** — not "shouldn't": nothing
in `backup_restore.py` writes `remote_auth.json`, so a restored config
cannot silently turn the gate on or off. The corollary is that a
migrated/restored install has **no PIN set** and needs one set explicitly.
See `docs/backup-restore.md`.

## Verification
After enabling, confirm the gate is actually enforcing, not just reporting
`enabled: true`:
```bash
curl -o /dev/null -w "%{http_code}\n" http://localhost:8500/api/devices
# should be 401 with no cookie, once enabled
```
Then confirm the full login round-trip (login → cookie → authenticated
request succeeds → logout → request fails again). See
`iterations/004-pin-gate-remote-auth/` for the exact sequence this was
verified with, including the lockout and session-expiry tests.
