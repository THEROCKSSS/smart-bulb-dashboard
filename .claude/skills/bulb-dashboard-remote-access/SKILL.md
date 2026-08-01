---
name: bulb-dashboard-remote-access
description: "Set up safe remote access to the Smart Bulb Dashboard beyond the LAN: Tailscale (recommended), DuckDNS+PIN gate, or a TLS reverse proxy (Caddy/nginx) — and how to test the PIN gate and its per-IP lockout behind that proxy."
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

### Adding TLS (a reverse proxy)
Ready-made configs live in `deploy/` — pick one from `deploy/README.md`.
`deploy/caddy/Caddyfile` is the path of least resistance (automatic Let's
Encrypt on a DuckDNS domain, renewal included);
`docker-compose.caddy.yml` bundles it with the app for one command.

**Then set the trusted-proxy env var, or the lockout silently breaks:**
```bash
SBD_TRUSTED_PROXIES=127.0.0.1,::1   # for a proxy on the same host
```
and start uvicorn with `--no-proxy-headers` (see pitfall 7). Verify:
```bash
curl -s https://<host>/api/system/proxy-status | jq
# client_ip must be YOUR address; peer_is_trusted_proxy true;
# peer_rewritten_by_server false
python3 deploy/smoke-test.py --base-url https://<host> --pin <the pin>
```

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
   used during this project's own testing — see `iterations/004`). Rotate
   to a real PIN before actually exposing anything.
2. **5 wrong attempts from one client locks that IP out for 5 minutes —
   including the correct PIN afterward.** This is intentional (a lockout
   that only blocks wrong guesses isn't a real lockout) — don't "fix" this
   by allowing correct PINs through during lockout, that defeats the point.
3. **Plain HTTP means the PIN is readable in transit**, once forwarded
   publicly. This is now fixable rather than just a caveat — use `deploy/`.
   Even with TLS, don't treat the PIN gate as sufficient on its own for a
   truly adversarial threat model.
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
   surviving a restart as a permanent record of who's locked out.
6. **Never widen `SBD_TRUSTED_PROXIES` to make something work.** It is the
   list of peers whose `X-Forwarded-For` is believed; every entry is a peer
   that can then claim any source IP it likes and farm fresh lockout
   buckets. `*` is only correct when the app is genuinely unreachable
   except through the proxy — never on `network_mode: host` or a
   non-loopback bind. Setting it while the app is *directly* reachable is
   strictly worse than leaving it unset.
7. **uvicorn's own `--proxy-headers` is on by default and trusts
   `127.0.0.1`.** It rewrites the client address from `X-Forwarded-For`
   before this app runs, which takes the decision out of
   `SBD_TRUSTED_PROXIES`'s hands entirely — and with the app bound to
   loopback, that's every request. Always run with `--no-proxy-headers`
   (`deploy/systemd/` and the `Dockerfile` already do). Confirm via
   `peer_rewritten_by_server` in `/api/system/proxy-status`.
8. **Don't set the session cookie's `Secure` flag unconditionally.**
   Browsers discard a `Secure` cookie delivered over plain HTTP, so a
   LAN-only HTTP user would log in and appear to have done nothing.
   `backend/main.py` sets it from `reverse_proxy.request_is_https()` for
   exactly this reason.
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

## Checking what you're actually exposing

Added in Week 2 Phase D. Full picture in `docs/remote-access-security.md`
and the formal model in `docs/pin-gate-threat-model.md`.

```bash
# Public IP (cached), DuckDNS last sync, exposure state, and the warnings
# the dashboard is currently showing
curl http://localhost:8500/api/system/remote-access/status

# Is Tailscale actually running on this host? Returns the tailnet URL.
curl http://localhost:8500/api/system/remote-access/tailscale

# The only outbound internet request this project makes -- on demand only
curl -X POST http://localhost:8500/api/system/remote-access/detect-public-ip
```

Two warning conditions to know about, both surfaced as a persistent,
non-dismissable banner in the UI:

- `public_client_observed` — a request genuinely arrived from a
  globally-routable IP while the PIN gate was off. This is evidence, not a
  heuristic.
- `exposure_configured_gate_disabled` — the fail-safe. Once exposure is
  recorded (a successful DuckDNS sync reports in, or the user declares a
  port forward in Settings), turning the PIN gate off *later* re-raises
  this and it survives restarts. Clearing it requires re-enabling the gate
  or explicitly retracting exposure — don't retract it as a way to silence
  the banner while the port forward is still live.

Tailnet addresses (100.64.0.0/10) never count as public, so normal
Tailscale use doesn't trip either warning.

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
