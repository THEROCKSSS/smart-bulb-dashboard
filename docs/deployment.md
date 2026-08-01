# Deployment, TLS & Operations

Covers running this as a real service rather than a terminal you left
open: TLS, reverse proxies, service supervision, updates, rollback, and
what it actually costs to run.

If all you want is HTTPS in front of the dashboard, skip to
[`../deploy/README.md`](../deploy/README.md) — it picks a path for you in
about five lines. This document is the reference behind it.

For the *security* posture (should you expose this at all, Tailscale vs.
DuckDNS, what the PIN gate does and doesn't protect against), read
[`remote-access-security.md`](remote-access-security.md) first. TLS
protects the PIN in transit. It does not decide whether putting bulb
control on the public internet is a good idea.

---

## 1. Supported platforms

**Python 3.10 or newer is required.** Not a preference — `backend/main.py`
uses PEP 604 union syntax in its Pydantic models (`int | None`, evaluated
at import), which is a `TypeError` on 3.9. The failure is at startup, so
you find out immediately.

| | Version | Notes |
|---|---|---|
| Python | **3.10+ required**, 3.11 recommended | `Dockerfile` pins `python:3.11-slim`; the project's own venv and test runs are on 3.11 |
| Linux | Debian 12 / Ubuntu 22.04 or newer | anything with Python 3.10+ and ALSA works; `deploy/systemd/` assumes systemd 229+ for `StartLimitIntervalSec` in `[Unit]` |
| Raspberry Pi | Pi OS Bookworm (64-bit) on a Pi 4 or newer | works, but see the CPU note in §2 — audio analysis is the load, not the API |
| Windows | 10 / 11 | supported; audio input works, service wrapping needs NSSM (see [`../deploy/windows-service.md`](../deploy/windows-service.md)) |
| macOS | 12+ | should work; **not verified by this project** — no macOS machine has run it |
| Docker | any current engine | `network_mode: host` is Linux-only in practice; Docker Desktop on Windows/macOS does not implement it usefully, so on those, use bridge networking and lose Tuya auto-discovery |

`sounddevice` needs PortAudio. It ships wheels with PortAudio bundled on
Windows and macOS; on Debian/Ubuntu install `libportaudio2`. Without it the
import fails at startup and nothing runs, including the non-audio routes.

## 2. Resource footprint (W2-181)

Measured on Windows 11, CPython 3.11, on a desktop CPU. Treat the CPU
percentages as a lower bound for weaker hardware.

| State | Memory (RSS) | CPU |
|---|---|---|
| Server idle, no session | **~80 MiB** | ~0% |
| One active audio-reactive session, one bulb | **~82 MiB** (+2 MiB) | **~4.4% of one core** |

The audio numbers are from a real capture-and-FFT session against a real
input device (270 bulb updates issued over 25 s ≈ 10.8 Hz), with bulb I/O
stubbed so the figure reflects analysis cost rather than network waiting.

What this means in practice:

- **Memory is flat.** Almost all of the ~80 MiB is numpy and the Python
  interpreter, paid once at import whether or not you ever use audio. A
  session adds ~2 MiB of ring buffers. Ten bulbs will not be ten times
  anything.
- **CPU scales with sessions, not bulbs.** A group session analyses the
  audio stream once and fans the result out to each bulb, so adding bulbs
  to a group costs one sender thread each, not another FFT.
- **A Raspberry Pi is the case to think about**, and this was **not
  measured on one**. The FFT runs per audio block regardless of hardware,
  so expect materially more than 4.4% of a core on a Pi. If audio-reactive
  stutters there, raise `min_dwell_ms` before anything else — it directly
  reduces how often the pipeline computes and sends.
- Idle memory is dominated by imports, so a 512 MiB box is fine and a
  256 MiB one is tight but plausible.

## 3. TLS and reverse proxies

Full configs in [`../deploy/`](../deploy/). The short version:

| Situation | Use |
|---|---|
| Public domain, want HTTPS, want it to keep working | [`../deploy/caddy/Caddyfile`](../deploy/caddy/Caddyfile) — automatic Let's Encrypt, renewal included |
| Already run nginx | [`../deploy/nginx/`](../deploy/nginx/) — same result, you own certbot renewal |
| Docker, want one command | [`../docker-compose.caddy.yml`](../docker-compose.caddy.yml) |
| LAN only, no public domain | [`../deploy/caddy/Caddyfile.lan-selfsigned`](../deploy/caddy/Caddyfile.lan-selfsigned) or `../deploy/nginx/make-selfsigned-cert.sh` |
| Just want it on your phone, safely | Tailscale Serve — see [`remote-access-security.md`](remote-access-security.md). No certs, no ports, no proxy. |

### 3.1 The setting you must not skip

```
SBD_TRUSTED_PROXIES=127.0.0.1,::1
```

Behind a reverse proxy the app's socket only ever sees the proxy. The PIN
gate's brute-force lockout and login rate limiter are **per client IP** —
so without this, every remote user shares one bucket. One attacker burning
five wrong PINs locks out the entire household, and the rate limiter
throttles legitimate users on the attacker's behalf. Nothing about this is
visible until it happens.

It is **opt-in, defaulting to trusting nothing**, because the alternative
is worse. `X-Forwarded-For` is just a request header: if the app believed
it unconditionally, anything that could reach the port directly would
forge a fresh source IP per guess and the lockout would stop existing.
Trusting the header is only safe once you have named the specific peer
whose word you accept.

Set it to the address **the app sees the proxy as**, not the proxy's public
address:

| Deployment | Value |
|---|---|
| Proxy on the same host, app on loopback | `127.0.0.1,::1` |
| Docker compose, both on a pinned bridge subnet | that subnet, e.g. `172.28.0.0/16` |
| Proxy on another LAN machine | that machine's LAN IP, e.g. `192.168.1.5` |

`*` trusts every peer. It exists for the case where the app is genuinely
unreachable except through the proxy, and it is a footgun anywhere else —
on `network_mode: host`, or any bind that isn't loopback-only, it lets
anything that can open a socket to the port forge its own source IP.

**Verify it, don't assume it.** Through the proxy:

```bash
curl -s https://yourname.duckdns.org/api/system/proxy-status | jq
```

`client_ip` must be your real address and `peer_is_trusted_proxy` must be
`true`. Or run the smoke test, which checks this and explains what's wrong:

```bash
python3 deploy/smoke-test.py --base-url https://yourname.duckdns.org --pin YOUR-PIN
```

### 3.2 Run uvicorn with `--no-proxy-headers`

`deploy/systemd/` and the `Dockerfile` already do. If you wrote your own
start command, add it.

uvicorn enables its own `ProxyHeadersMiddleware` by default, trusting
`127.0.0.1`, and it rewrites the request's client address from
`X-Forwarded-For` **before the app sees it**. With the app bound to
loopback that matches every request, so any local process can hand itself
an arbitrary source IP and a fresh lockout bucket — and `SBD_TRUSTED_PROXIES`
never gets a say, because by the time this app looks, the substitution has
already happened.

```bash
python -m uvicorn main:app --host 127.0.0.1 --port 8500 --no-proxy-headers
```

`/api/system/proxy-status` reports `peer_rewritten_by_server: true` when
it detects the server did this. It must be `false`.

### 3.3 HSTS and redirect-to-HTTPS

Both are app-level, both **default to off**, both are opt-in:

| Variable | Default | Effect |
|---|---|---|
| `SBD_HSTS` | `off` | send `Strict-Transport-Security`, and only ever on a request that really is HTTPS |
| `SBD_HSTS_MAX_AGE` | `31536000` | seconds |
| `SBD_HSTS_INCLUDE_SUBDOMAINS` | `off` | adds `includeSubDomains` |
| `SBD_HSTS_PRELOAD` | `off` | adds `preload` |
| `SBD_HTTPS_REDIRECT` | `off` | 307 plain HTTP to the same URL over HTTPS |

Off by default is deliberate, not an oversight:

- **The redirect would break every LAN-only HTTP user instantly**, which is
  this project's default and most common deployment.
- **HSTS is sticky.** Once a browser has seen it for a hostname it refuses
  plain HTTP there until `max-age` elapses — you cannot retract it by
  turning the setting off, because the browser stopped asking. On a home
  hostname you may later want to serve over HTTP, that's a year of pain
  for a header a reverse proxy is already better placed to send.
- **Both proxy configs in `deploy/` set HSTS themselves.** In the
  recommended setup the app's copy would be a duplicate header with a
  second source of truth.

Turn `SBD_HSTS=on` if you terminate TLS somewhere that doesn't set the
header for you. To *undo* an HSTS pin a browser already stored, serve
`max-age=0` for a while — `SBD_HSTS=on` with `SBD_HSTS_MAX_AGE=0`. Plain
`SBD_HSTS=off` only stops sending it.

The redirect never applies to `/healthz` or `/api/system/health`, so
container and proxy health probes over plain HTTP keep working.

### 3.4 Health endpoints

Two, deliberately:

- **`/healthz`** — for infrastructure. Returns `{"status": "ok"}` and
  nothing else, stays reachable when the PIN gate is on, is never
  redirected. Use it for proxy upstream checks, Docker `HEALTHCHECK`, and
  uptime monitors. It's the endpoint most likely to end up publicly
  reachable, so it deliberately leaks no version or uptime.
- **`/api/system/health`** — the app's own status (uptime), free to grow
  richer dependency checks that would be wrong to expose publicly.

### 3.5 Mixed content

The frontend loads everything relative (`/static/...`) and calls the API
with a relative base, so the same files work identically over HTTP and
HTTPS with no per-scheme configuration. This is enforced by a test
(`backend/tests/test_reverse_proxy.py::test_frontend_assets_have_no_mixed_content_references`)
rather than by convention, because one hardcoded `http://192.168.1.20:8500`
committed during debugging is enough to break the dashboard over HTTPS.

## 4. Running it as a service

- **Linux:** [`../deploy/systemd/`](../deploy/systemd/) — restart on crash,
  start on boot, plus an optional health-check timer that restarts the
  service if it stops answering `/healthz` while still technically running.
- **Windows:** [`../deploy/windows-service.md`](../deploy/windows-service.md) — NSSM.
- **Docker:** `restart: unless-stopped` plus the `healthcheck` in
  `docker-compose.yml`. Note that a healthcheck marks a container
  unhealthy; it does not restart it. Add an autoheal sidecar if you want
  the restart, or use the systemd timer instead.

## 5. Updating (W2-183)

`backend/config.json` (device credentials) and `backend/data/` (favourites,
schedules, sessions, audit log) are both git-ignored, so a normal update
does not touch them. That is the design, not luck — but verify rather than
trust it:

```bash
cd /opt/smart-bulb-dashboard
cp backend/config.json ~/sbd-config-backup-$(date +%F).json   # 5 seconds, always worth it
git fetch --tags
git log --oneline HEAD..origin/main        # read what you're about to take
git rev-parse HEAD > ~/sbd-previous-commit # your rollback target
git pull
backend/venv/bin/pip install -r backend/requirements.txt
sudo systemctl restart smart-bulb-dashboard
python3 deploy/smoke-test.py --base-url http://127.0.0.1:8500
```

The last line is the point of the procedure. An update that leaves the
service running but the dashboard broken is the normal failure mode, and
it is not obvious from `systemctl status`.

Docker:

```bash
docker compose pull            # if using a registry image
docker compose build --no-cache
docker compose up -d
python3 deploy/smoke-test.py --base-url http://127.0.0.1:8500
```

## 6. Rolling back (W2-190)

```bash
cd /opt/smart-bulb-dashboard
git checkout "$(cat ~/sbd-previous-commit)"
backend/venv/bin/pip install -r backend/requirements.txt   # dependencies roll back too
sudo systemctl restart smart-bulb-dashboard
python3 deploy/smoke-test.py --base-url http://127.0.0.1:8500
```

Re-running `pip install` is the step people skip and then spend an hour
on. `git checkout` moves the code back; it does not touch the venv, so a
dependency the newer version pulled in stays installed at its newer
version.

Two things that do **not** roll back with the code:

- **`backend/data/`.** If a release migrated a data file, checking out the
  old code leaves the new file shape in place. Restore your backup of
  `backend/data/` alongside the code if a release note mentions a data
  change.
- **A rotated PIN or revoked sessions.** These live in
  `backend/data/remote_auth.json`, not in git.

Docker rollback is `git checkout <commit> && docker compose up -d --build`,
or keep the previous image tagged before you rebuild.

## 7. Version pinning (W2-184)

`backend/requirements.txt` pins every direct dependency with `==`. Keep it
that way. `pip install -r backend/requirements.txt` on a machine you set up
six months ago must install what it installed then, or "it works on the
other Pi" becomes unanswerable.

What that does *not* pin is the rest of the transitive tree — `anyio`,
`h11`, `click` and friends still resolve to whatever is current.
(`starlette` is the exception: it is pinned directly, because the CVEs in
#74 were all its, and leaving it to FastAPI's floor would let a fresh
install drift back onto a vulnerable build.) For a genuinely reproducible
install, freeze the full tree once it works:

```bash
backend/venv/bin/pip freeze > backend/requirements.lock.txt
backend/venv/bin/pip install -r backend/requirements.lock.txt   # on the next machine
```

Keep `requirements.txt` as the human-readable list of what this project
actually depends on, and the lock file as the byte-exact reproduction.
Upgrading a pin is a deliberate act: change it, run
`backend/venv/Scripts/python -m pytest backend/tests/ -q`, then commit both
files together.

## 8. Getting help (W2-195)

File an issue at
<https://github.com/THEROCKSSS/smart-bulb-dashboard/issues>. Include:

1. Output of `python3 deploy/smoke-test.py --base-url <your-url> --json`.
   It answers most of the follow-up questions in one paste.
2. Python version (`python3 --version`), OS, and how you run it (systemd /
   Docker / a terminal).
3. Relevant log lines — `journalctl -u smart-bulb-dashboard -n 100`, or
   `docker compose logs --tail 100`.
4. Whether the PIN gate is on, and whether a reverse proxy is in front.
5. For a bulb problem: whether `ping <bulb-ip>` works. These bulbs drop off
   Wi-Fi on their own, and that presents identically to a software bug.

**Never paste `backend/config.json`, a `local_key`, or your PIN.** The
smoke test's output is safe to share; those are not. `GET /api/devices`
redacts `local_key`, but the file on disk does not.
