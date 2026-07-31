# deploy/ — TLS, reverse proxies, and running this as a service

Reference configs for putting the dashboard behind HTTPS and keeping it
running. Every file here is a real, working config, not an illustration —
see [What's been validated](#whats-been-validated) for exactly how each was
checked and what wasn't.

Read [`../docs/deployment.md`](../docs/deployment.md) for the reasoning;
this is the index.

## Pick one

| You have | Use |
|---|---|
| A phone and a desire to not think about certificates | **Tailscale Serve** — no files here, see [`../docs/remote-access-security.md`](../docs/remote-access-security.md). Genuinely the best option for most people. |
| A DuckDNS domain and ports 80+443 forwarded | [`caddy/Caddyfile`](caddy/Caddyfile) — automatic Let's Encrypt, renewal included, nothing to maintain |
| Docker, and want one command | [`../docker-compose.caddy.yml`](../docker-compose.caddy.yml) |
| nginx already running on the box | [`nginx/`](nginx/) — same result, you own certbot renewal |
| A LAN only, no public domain | [`caddy/Caddyfile.lan-selfsigned`](caddy/Caddyfile.lan-selfsigned) (`tls internal`) or [`nginx/make-selfsigned-cert.sh`](nginx/make-selfsigned-cert.sh) |

## Contents

```
caddy/Caddyfile                    Public HTTPS on a DuckDNS domain, automatic Let's Encrypt
caddy/Caddyfile.lan-selfsigned     LAN-only HTTPS via Caddy's local CA, no public domain
nginx/smart-bulb-dashboard.conf    nginx equivalent (TLS, rate limiting, WebSocket passthrough)
nginx/README.md                    certbot issuance + the renewal automation nginx needs
nginx/make-selfsigned-cert.sh      Self-signed cert with the SANs browsers actually check
systemd/*.service, *.timer         Linux service: restart on crash, start on boot, health-probe restart
windows-service.md                 Windows equivalent via NSSM
smoke-test.py                      Post-deploy verification — run this after every change
```

## Whichever you choose, do these three things

**1. Enable the PIN gate before exposing anything.** TLS stops people
reading your PIN off the wire. It does nothing about people typing one in.

**2. Set `SBD_TRUSTED_PROXIES`.** Behind a proxy the app only ever sees the
proxy's address, so the PIN gate's per-IP brute-force lockout keys every
remote user into one shared bucket — one attacker locks out the whole
household. For a proxy on the same host:

```
SBD_TRUSTED_PROXIES=127.0.0.1,::1
```

It's opt-in, and defaults to trusting nothing, because `X-Forwarded-For` is
just a request header: believed unconditionally, anything that can reach
the port forges a new source IP per guess and the lockout stops existing.

**3. Run the smoke test.** It checks the two above actually took effect,
plus TLS validity, certificate expiry, cookie flags, and asset loading:

```bash
python3 deploy/smoke-test.py --base-url https://yourname.duckdns.org --pin YOUR-PIN
```

## What's been validated

Honest accounting, since a config file that merely looks plausible is worse
than none:

| Artifact | How it was checked |
|---|---|
| `caddy/Caddyfile` | `caddy validate` against Caddy **v2.11.4** — passes, and `caddy fmt` clean |
| `caddy/Caddyfile.lan-selfsigned` | same, passes |
| `nginx/smart-bulb-dashboard.conf` | `nginx -t` against nginx **1.27-alpine**, with real certificates in place — passes |
| `nginx/make-selfsigned-cert.sh` | executed on Linux (alpine 3.20); output inspected for correct SAN list (DNS + IP), 825-day validity, key mode 600 |
| `systemd/*.service`, `*.timer` | `systemd-analyze verify` on Debian 12 — clean. It caught a real bug: `StartLimitIntervalSec` was in `[Service]`, where systemd silently ignores it |
| `../docker-compose.caddy.yml` | `docker compose config` — parses and resolves |
| `smoke-test.py` | run against a live server: PIN gate off and on, and through a real Caddy container proxying to it. It found a real bug in itself (case-sensitive header lookup) |
| X-Forwarded-For handling | end-to-end through that real Caddy container: with trust unset a forged header is ignored; with trust set the real client IP comes through |

**Not validated:** no certificate was ever issued from Let's Encrypt (that
needs a real domain and open ports), no systemd unit was started on a real
Linux host, the NSSM instructions in `windows-service.md` were not run, and
the Raspberry Pi performance guidance in `../docs/deployment.md` is
reasoning from measurements taken on a desktop, not from a Pi.
