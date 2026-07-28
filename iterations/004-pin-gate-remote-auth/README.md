# 004 — PIN-Gate Remote-Access Auth

## Goal
Add a real, working security gate for the case where this dashboard is
exposed beyond the LAN (DuckDNS + port forwarding, requested for a later
phase) — a PIN required before any use, with brute-force lockout, so an
internet-facing instance isn't just an open bulb-control API.

## Approach
- `backend/remote_auth.py`: PIN stored as a PBKDF2-SHA256 hash (200k
  iterations) + random salt, never plaintext. Stateless signed session
  tokens (HMAC-SHA256 over an expiry timestamp, server-side secret
  generated once and persisted) instead of a server-side session store —
  simpler, and survives a backend restart without invalidating active
  sessions. Per-IP failed-attempt tracking with a 5-attempt / 5-minute
  lockout, in-memory (resets on restart — an accepted tradeoff for a
  single-user local dashboard, not meant to replace a real auth system for
  a multi-tenant service).
- `main.py`: a FastAPI `@app.middleware("http")` gate that's a complete
  no-op unless explicitly enabled (default LAN-only setups see zero
  behavior change), checking a signed cookie against every route except an
  explicit open list.
- Login/logout/status endpoints (`/api/auth/*`) plus Settings-facing
  enable/disable/status endpoints (`/api/system/remote-auth/*`).
- Frontend: a full-screen PIN overlay (`index.html`/`style.css`) shown when
  `/api/auth/status` reports `enabled && !authenticated`; a Settings card
  to turn the gate on/off.

## What happened
The backend auth logic (hashing, lockout, token expiry) worked correctly
on the first real test pass. The frontend integration caught one real bug
that would have made the feature actively harmful if shipped as-is.

## Failures

**The root page itself was gated, creating a lockout with no way in.**
`OPEN_PATHS` initially listed only the API login/status/health endpoints,
not `/`. With the PIN gate enabled, a browser's `GET /` got the
middleware's raw `401 JSON` response instead of `index.html` — meaning the
page containing the PIN entry form itself never loaded. Found via a real
Playwright test: after enabling the gate and loading the page, the test
checked for `#pin-gate-overlay` in the served HTML and got `false`; the
console showed a `401` resource-load error for the page request itself.
This would have been a real "lock yourself out of your own dashboard" bug
for anyone who actually enabled it. **Fixed** by adding `/` to
`OPEN_PATHS` — the static shell (HTML/CSS/JS) is always reachable;
everything it *calls* (the actual API) stays gated.

## Verification
All of the following were run against the real, running backend (not
mocked), with a fresh server restart before the token-expiry test so
in-memory attempt-tracking state didn't carry over from earlier tests:

1. **Default (disabled) state** — confirmed zero behavior change:
   `/api/devices` and friends return normally with no cookie.
2. **Enabling the gate** blocks a previously-open endpoint
   (`/api/devices` → `401`) while the explicitly-open health check still
   returns `200`.
3. **Wrong PIN** is rejected (`"incorrect PIN"`), doesn't reveal anything
   about the real PIN.
4. **Brute-force lockout**: 5 cumulative wrong attempts from the same
   client locked that IP out for 300s — confirmed the 6th attempt, *even
   with the correct PIN*, was still rejected with a lockout message. This
   is the property that actually matters (a lockout that only blocks wrong
   guesses isn't a real lockout).
5. **Correct PIN** issues a working session cookie; subsequent requests
   with that cookie succeed (`/api/devices` returns real device data,
   local_key still redacted as always).
6. **Session expiry is enforced server-side, not just client-side**:
   started a session with a 10-second TTL, waited 11 seconds, confirmed
   the (still-present) cookie was rejected — proving the signed token's
   embedded expiry is actually checked, not just relying on the browser to
   drop an expired cookie.
7. **Disabling the gate** restores unauthenticated access immediately.
8. **Full real-browser login flow** (Playwright, after the root-path fix):
   PIN overlay appears on load while gate is enabled; wrong PIN shows an
   inline error and keeps the overlay up; correct PIN hides the overlay and
   the dashboard loads normally underneath; zero console/page errors
   throughout.

## What this is NOT

This is a real, tested access gate appropriate for a personal single-user
dashboard fronted by DuckDNS/a router port-forward — it is **not** a
substitute for TLS (the PIN and session cookie both travel in plaintext
over bare HTTP; see `docs/remote-access-security.md` for why Tailscale is
recommended over raw DuckDNS exposure), and the lockout is IP-based and
in-memory, not a hardened rate-limiter suitable for a multi-tenant or
high-value target. A dedicated adversarial pentest (a separate agent
actually trying to break this from outside, port scanning, replay attacks,
timing attacks on the PBKDF2 comparison, etc.) is intentionally scoped as
its own future roadmap phase rather than squeezed in here — see
`roadmap/`.
