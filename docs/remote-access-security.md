# Remote Access & Security

This dashboard defaults to LAN-only, no auth, because that's the correct
posture for a trusted home network with a REST API that has zero
authentication otherwise. This doc covers what to do if you want to reach
it from outside your house, and what you must set up alongside that so
you're not putting an open bulb-control API (and, more importantly, a
foothold into your home network) on the public internet.

## Recommended: Tailscale (do this first)

[Tailscale](https://tailscale.com) creates a private mesh VPN between your
devices — your phone/laptop can reach `http://<machine>:8500` as if it were
on your home LAN, from anywhere, **without opening any port on your
router or exposing anything to the public internet at all**. This is the
safe default and should be your first choice.

1. Install Tailscale on the machine running this dashboard, and on your
   phone/laptop. Sign in with the same account on both.
2. Note the Tailscale IP or MagicDNS name assigned to the dashboard
   machine (`tailscale ip -4`, or check the admin console).
3. From your phone (on Tailscale, not your home Wi-Fi), open
   `http://<tailscale-ip>:8500`. It should work identically to being on
   your home network.

No PIN gate is strictly required here — Tailscale's own device
authentication is already the access control. You can still enable the PIN
gate for defense-in-depth (e.g. if someone else's device is also on your
tailnet), but it's not the primary defense in this setup.

**v0.3.0 update — exercised, not just recommended:** this setup was actually
run this way, with the PIN gate *also* enabled. Rather than hitting the bare
Tailscale IP over plain HTTP, `tailscale serve` gives you a real HTTPS
endpoint (no self-signed-cert warnings) that's still tailnet-only — not
`funnel`, which would expose it to the public internet:

```bash
# One-time, per machine — exposes a local port over the tailnet only
tailscale serve --bg --https=8502 http://127.0.0.1:8502
tailscale serve status   # confirm it's listed as "(tailnet only)", not "Funnel on"
# To turn it off later:
tailscale serve --https=8502 off
```

Verified end-to-end: root path stays reachable, every protected route
correctly returns 401 without a session, and a real PIN login through the
Tailscale HTTPS route works identically to hitting it over plain LAN HTTP.

## If you want actual public access: DuckDNS + port forward

This is a materially different risk profile — your router forwards a port
straight to this dashboard, and DuckDNS just gives that public IP a
memorable hostname. **Anyone on the internet who finds that hostname:port
can reach the API.** Do not do this without the PIN gate enabled (see
below), and understand its limits (also below) before relying on it.

### Setup

1. Create a free account at [duckdns.org](https://www.duckdns.org), claim
   a subdomain (e.g. `yourname.duckdns.org`).
2. Run DuckDNS's update script (cron job or their Docker container) so the
   subdomain always points at your current public IP — home IPs change
   periodically, this keeps it current.
3. On your router, forward an external port (**do not use 8500 externally
   — pick something non-obvious**, e.g. `48213 → <machine-LAN-IP>:8500`).
4. Access via `http://yourname.duckdns.org:48213`.

### You MUST also do this

- **Enable the PIN gate** (Settings → Remote Access, or
  `POST /api/system/remote-auth/enable`) before forwarding the port. See
  `iterations/004-pin-gate-remote-auth/` for exactly what this does and
  what was tested. Use a real PIN, not `1234` or `test1234` (the value used
  during this project's own testing — rotate it before exposing anything
  publicly).
- **This is plain HTTP, not HTTPS.** The PIN and session cookie both
  travel unencrypted between your phone and your house. On a trusted home
  Wi-Fi this doesn't matter much; the moment you're forwarding a port to
  the public internet, anyone positioned to intercept that specific
  traffic path (a malicious/compromised Wi-Fi you're connecting from,
  your ISP, etc.) can see the PIN in plaintext. If this matters to you,
  put a reverse proxy (e.g. Caddy, which gets you free automatic TLS certs
  for a DuckDNS domain with almost no config) in front of this dashboard
  instead of forwarding straight to it. This is flagged as a roadmap item
  rather than built now — see `roadmap/`.
- **Use a non-default external port.** Port 8500 (or any well-known
  default) gets found by internet-wide scanners fast. A random high port
  isn't real security on its own, but it cuts down drive-by scanning noise
  a lot.
- **Set a DHCP reservation** for the dashboard machine's LAN IP so your
  port-forward rule doesn't silently break when its address changes.

### What the PIN gate does and doesn't protect against

Does:
- Blocks casual/automated discovery from immediately controlling your
  bulbs or reading your device inventory.
- Locks out an IP for 5 minutes after 5 wrong PIN attempts — a real,
  tested brute-force throttle (see iteration 004).
- Session expiry is enforced server-side, not just by cookie expiration.
- **v0.3.0:** rate-limits login attempts per-IP *independent of and ahead
  of* the lockout counter (`POST /api/system/remote-auth/rate-limit` to
  tune it) — this specifically closes the "distributed attempts across
  many source IPs" gap noted below in the pre-v0.3.0 version of this doc.
- **v0.3.0:** every auth event is appended to a real audit log
  (`backend/data/auth_audit.log`, one JSON line per event: login
  success/failure, lockout, session revocation) — never containing the PIN
  or a raw session token. Verified by asserting the raw PIN string is
  absent from the log file after both a correct and incorrect login.
- **v0.3.0:** sessions can be listed (`GET /api/auth/sessions`) and revoked
  individually or all at once — a forgotten or compromised session doesn't
  have to just expire on its own.
- **Week 2:** repeat lockouts for the same source double in length (300s,
  600s, 1200s … capped at 24h by default), so sustained guessing gets more
  expensive over time instead of costing a fixed price per batch. A quiet
  source's escalation decays after 24h so one bad night doesn't brand an
  address forever. Threshold and durations are configurable
  (`POST /api/system/remote-auth/lockout-policy`, or Settings).
- **Week 2:** weak PINs are refused, not warned about — under 6 characters,
  well-known PINs, this project's own development/test PINs, repeated
  characters, straight sequences, and short repeated patterns. There is
  deliberately no override flag; the only thing an override ever gets used
  for is shipping the dev PIN.
- **Week 2:** IPv6 sources are tracked by /64 prefix and normalized across
  spellings (compressed/expanded, bracketed, scope id, IPv4-mapped). Without
  this, an IPv6 attacker walks out of every lockout for free by picking a
  new address inside their own allocation — which SLAAC privacy extensions
  do automatically, without any attacker effort.
- **Week 2:** changing the PIN revokes every existing session and rotates
  the signing key, so a cookie issued under the old PIN dies with it.
- **Week 2:** guest PINs (up to 5) open the same gate and can be revoked
  individually; revoking one signs out only the sessions it opened.
- **Week 2:** a general per-IP API rate limit sits in front of everything —
  see "Rate limiting" below.

Doesn't:
- Encrypt traffic on its own (see the HTTPS point above) — though Tailscale
  Serve, above, sidesteps this for the tailnet path specifically.
- Defend against an attacker who can see your network traffic and doesn't
  need to guess the PIN because they can just read it off the wire.
- Replace a real login/user system. Guest PINs are a second *credential*,
  not a second *account*: a guest has exactly the same powers as the
  household, just a separately-revocable way in. The audit log records
  which PIN opened a session, so you can tell "someone using the guest PIN"
  from "someone using the household PIN" — but not which person that was.
  Real per-user identity is Week 3's multi-user work.
- Survive a restart with its lockout/rate-limit state intact. All per-IP
  tracking is in-memory by design (the same accepted tradeoff since the gate
  shipped), so restarting the backend clears every active lockout and
  rate-limit counter. That's fine for a single-household dashboard and a
  documented limitation, not an oversight — a persisted store is a roadmap
  item if in-memory ever proves insufficient in practice.

### Rate limiting

Two separate things, easy to confuse:

- **Login lockout / login rate limit** — auth-specific. Counts wrong PINs
  and raw request volume to `/api/auth/login`.
- **General API rate limit** — everything else. A per-IP cap on request
  volume across the whole public HTTP surface, with different allowances by
  endpoint sensitivity.

**Loopback and LAN clients are exempt by default.** The point of the general
limiter is unattended public exposure; throttling your own phone on your own
Wi-Fi would be a bug, not a security win. This is why the Diagnostics
rate-limit counters usually read zero on a local-only setup.

Recommended defaults:

| Setup | Suggestion |
|---|---|
| LAN-only (no port forward, no Tailscale) | Leave everything at defaults. The exemption means the limiter is effectively inert. |
| Tailscale-only | Leave at defaults. Tailnet addresses (100.64.0.0/10) are private, so they're exempt too. |
| DuckDNS + port forward | Keep the exemption on (your LAN still shouldn't be throttled) and consider lowering `write` to ~60/min. Watch Diagnostics → Rate limiting for a week before tightening further. |
| Shared/untrusted LAN | Turn the exemption off (`SBD_RATE_LIMIT_EXEMPT_LOCAL=0`) so local clients are limited too. |

Durable changes go in env vars (`SBD_RATE_LIMIT_READ`, `_WRITE`, `_POLL`,
`_EXPENSIVE`, `SBD_RATE_LIMIT_EXEMPT_LOCAL`); `POST /api/system/rate-limit`
is the runtime override and does not survive a restart.

**Not the same as bulb pacing.** `min_dwell_ms` limits how often a *bulb* is
written to, to protect the hardware and make colour changes visible. API
rate limiting limits how often a *client* may call the HTTP API. They never
interact: the audio engine's per-bulb sender dispatches from an in-process
queue and never enters the HTTP stack, so a lightshow running at dozens of
updates a second consumes exactly zero of the API budget.

## Still planned: a dedicated adversarial security-test phase

v0.3.0 added real hardening (rate limiting, audit logging, session
revocation) and a live 21-check verification pass against the actual
running server with the PIN gate enabled — but that's a same-machine
verification pass, not an adversarial one; every check was written by the
same session that built the feature. Before treating a DuckDNS+PIN (or even
a Tailscale+PIN) setup as fully "done," this project's roadmap (see
`roadmap/`) still includes a dedicated phase where a separate agent
actively attempts to break the exposed setup from the outside — brute-force
timing analysis, session token forgery attempts, replay attacks, port-scan
discovery timing, and confirming the lockout/rate-limiter can't be
trivially bypassed (e.g. via `X-Forwarded-For` spoofing if a proxy is added
later). That's intentionally scoped as its own phase rather than folded
into this build, since a real adversarial test deserves to run against an
actually-deployed instance (real DuckDNS domain, real port forward) rather
than a same-machine simulation.

## Not doing (for now)

- A reverse proxy / automatic TLS setup (Caddy or similar) — flagged above
  as the real fix for the plaintext-PIN concern, scoped as a roadmap item.
- Multi-user accounts — this is a single shared PIN by design; see
  `ROADMAP.md`'s existing note on multi-user auth as a separate, larger
  feature.
