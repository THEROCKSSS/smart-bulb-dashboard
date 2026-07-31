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

### Guest PINs, PIN change, policy
```bash
# Grade a PIN before committing to it (Settings shows this live as you type)
curl -X POST http://localhost:8500/api/system/remote-auth/pin-strength \
  -H "Content-Type: application/json" -d '{"pin": "<candidate>"}'

# Guest PIN: same access, separately revocable (max 5 active)
curl -b cookies.txt -X POST http://localhost:8500/api/system/remote-auth/pins \
  -H "Content-Type: application/json" \
  -d '{"pin": "<guest pin>", "label": "Dog sitter", "expires_in_s": 604800}'
curl -b cookies.txt -X DELETE http://localhost:8500/api/system/remote-auth/pins/<pin_id>

# Change the household PIN (signs every other device out, reissues your cookie)
curl -b cookies.txt -c cookies.txt -X POST http://localhost:8500/api/system/remote-auth/pin \
  -H "Content-Type: application/json" -d '{"pin": "<new pin>"}'

# Session length + lockout policy
curl -b cookies.txt -X POST http://localhost:8500/api/system/remote-auth/session-ttl \
  -H "Content-Type: application/json" -d '{"session_ttl_s": 28800}'
curl -b cookies.txt -X POST http://localhost:8500/api/system/remote-auth/lockout-policy \
  -H "Content-Type: application/json" -d '{"max_attempts": 5, "base_seconds": 300}'
```

### Rate limiting
```bash
curl http://localhost:8500/api/system/rate-limit
curl http://localhost:8500/api/system/diagnostics/rate-limit   # also in the Diagnostics panel
```
Loopback/LAN is exempt by default, so a local test will show zeros. To see
the limiter actually work, turn the exemption off first:
```bash
curl -X POST http://localhost:8500/api/system/rate-limit \
  -H "Content-Type: application/json" -d '{"exempt_local": false, "limits": {"read": 2}}'
# third GET in the same minute -> 429 with a Retry-After header
```

## Pitfalls

1. **Don't leave a test/default PIN active** (`test1234`, `1234`, etc. were
   used during this project's own testing — see `iterations/004`). The gate
   now refuses these outright, along with anything under 6 characters,
   sequences, and repeated patterns. There is no override flag; if you're
   tempted to add one, that's the exact failure mode it exists to prevent.
2. **5 wrong attempts from one client locks that IP out — including the
   correct PIN afterward — and repeat lockouts double the wait.** This is
   intentional (a lockout that only blocks wrong guesses isn't a real
   lockout) — don't "fix" this by allowing correct PINs through during
   lockout, that defeats the point. Threshold and durations are configurable
   via `/api/system/remote-auth/lockout-policy` if 5/5min is wrong for you.
3. **This is plaintext HTTP, not HTTPS**, once forwarded publicly. The PIN
   and session cookie are both visible to anything positioned on that
   traffic path. Don't treat the PIN gate as sufficient on its own for a
   truly adversarial threat model — see the doc's TLS/reverse-proxy note.
4. **The root page `/` must stay in `remote_auth.OPEN_PATHS`** — gating it
   was a real bug found during development (the PIN entry page itself
   couldn't load, a total lockout). If modifying `remote_auth.py`, don't
   remove `/` from the open list without understanding why it's there.
5. **In-memory lockout state resets on backend restart.** Don't rely on it
   surviving a restart as a permanent record of who's locked out. The same
   is true of the general API rate limiter's counters *and* its runtime
   config — env vars (`SBD_RATE_LIMIT_*`) are the durable way to change a
   default.
6. **Never call `api_rate_limit.check()` from anywhere but the HTTP
   middleware.** The moment it's reachable from service-layer code, the
   audio engine's internal per-bulb dispatch starts spending a budget meant
   for external clients and a long lightshow 429s the user's own browser.
   There's a test (`test_check_is_only_ever_called_from_the_http_middleware`)
   that fails if a second call site appears.

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
