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

## Pitfalls

1. **Don't leave a test/default PIN active** (`test1234`, `1234`, etc. were
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
