# API Reference

Base URL: `http://localhost:8500` (or your host/port). All bodies are JSON.
Replace `bulb-1` with your device's `id` from `config.json`.

## System

```bash
curl http://localhost:8500/healthz            # {"status": "ok"} -- nothing else
curl http://localhost:8500/api/system/health  # {"ok": true, "uptime_seconds": ...}
curl http://localhost:8500/api/system/info
curl http://localhost:8500/api/system/proxy-status
```

`/healthz` is the **infrastructure** liveness probe: for a reverse proxy's
upstream check, a Docker `HEALTHCHECK`, or an uptime monitor. It stays
reachable when the PIN gate is enabled, is never caught by the
redirect-to-HTTPS setting, and deliberately returns nothing that
fingerprints the install — it's the endpoint most likely to end up publicly
reachable. `/api/system/health` remains the app's own richer status route.

`/api/system/proxy-status` (gated like the rest of `/api/system/`) reports
what the backend believes about a request's real client IP and TLS state,
and the trusted-proxy/HSTS/redirect settings behind that belief. Use it to
verify a reverse-proxy deployment: `client_ip` must be your real address,
not the proxy's, or the PIN gate's per-IP lockout is keying every remote
user into one bucket. See [docs/deployment.md](docs/deployment.md).

## Devices

```bash
# List configured devices (local_key is always redacted)
curl http://localhost:8500/api/devices

# Add a device
curl -X POST http://localhost:8500/api/devices -H "Content-Type: application/json" -d '{
  "id": "bulb-2", "name": "Bedroom", "device_id": "...", "local_key": "...",
  "ip": "192.168.1.101", "version": 3.3
}'

# Remove a device
curl -X DELETE http://localhost:8500/api/devices/bulb-2

# Live status (labeled data_source: LIVE DATA)
curl http://localhost:8500/api/devices/bulb-1/status
```

## Power & brightness

```bash
curl -X POST http://localhost:8500/api/devices/bulb-1/power -d '{"on": true}' -H "Content-Type: application/json"
curl -X POST http://localhost:8500/api/devices/bulb-1/toggle
curl -X POST http://localhost:8500/api/devices/bulb-1/brightness -d '{"value": 60}' -H "Content-Type: application/json"
```

## Color — this is the main thing you asked about

```bash
# RGB
curl -X POST http://localhost:8500/api/devices/bulb-1/color \
  -H "Content-Type: application/json" \
  -d '{"r": 255, "g": 0, "b": 0}'

# HSV (h: 0-359, s/v: 0-100)
curl -X POST http://localhost:8500/api/devices/bulb-1/color/hsv \
  -H "Content-Type: application/json" \
  -d '{"h": 120, "s": 100, "v": 100}'

# Random color
curl -X POST http://localhost:8500/api/devices/bulb-1/color/random

# White mode with brightness + color temp (0=warm, 100=cool)
curl -X POST http://localhost:8500/api/devices/bulb-1/white \
  -H "Content-Type: application/json" \
  -d '{"brightness": 80, "color_temp": 60}'
```

## Quick actions

```bash
curl -X POST http://localhost:8500/api/devices/bulb-1/identify
curl -X POST http://localhost:8500/api/devices/bulb-1/flash-alert \
  -H "Content-Type: application/json" -d '{"r":255,"g":0,"b":0,"times":3}'
```

## Presets & favorites

```bash
curl http://localhost:8500/api/presets
curl -X POST http://localhost:8500/api/devices/bulb-1/presets/apply -d '{"preset_id":"ocean"}' -H "Content-Type: application/json"

curl http://localhost:8500/api/devices/bulb-1/favorites
curl -X POST http://localhost:8500/api/devices/bulb-1/favorites \
  -H "Content-Type: application/json" -d '{"name":"Cozy","r":255,"g":150,"b":80}'
curl -X DELETE http://localhost:8500/api/devices/bulb-1/favorites/<favorite_id>
```

## Scenes

```bash
curl http://localhost:8500/api/scenes
curl -X POST http://localhost:8500/api/devices/bulb-1/scenes/apply \
  -H "Content-Type: application/json" -d '{"scene_id":"movie_night"}'
```

## Effects

```bash
curl http://localhost:8500/api/effects
curl -X POST http://localhost:8500/api/devices/bulb-1/effects/start \
  -H "Content-Type: application/json" -d '{"effect":"rainbow","speed":1.5}'
curl -X POST http://localhost:8500/api/devices/bulb-1/effects/stop
curl http://localhost:8500/api/devices/bulb-1/effects/current
```

Effect IDs: `rainbow`, `pulse`, `strobe`, `candle`, `fade` (accepts optional
`color_a`/`color_b` as `[r,g,b]`), `color_loop`, `random`.

## Timers

```bash
# Sleep timer (fades out over the last 20% of duration, then powers off)
curl -X POST http://localhost:8500/api/devices/bulb-1/timers/sleep -d '{"minutes":30}' -H "Content-Type: application/json"
curl http://localhost:8500/api/devices/bulb-1/timers/sleep
curl -X DELETE http://localhost:8500/api/devices/bulb-1/timers/sleep

# Wake / sunrise timer
curl -X POST http://localhost:8500/api/devices/bulb-1/timers/wake \
  -H "Content-Type: application/json" \
  -d '{"time":"07:00","brightness":100,"color_temp":70,"fade_minutes":15}'
curl -X DELETE http://localhost:8500/api/devices/bulb-1/timers/wake
```

## Recurring schedule

```bash
curl http://localhost:8500/api/devices/bulb-1/schedule
curl -X POST http://localhost:8500/api/devices/bulb-1/schedule \
  -H "Content-Type: application/json" \
  -d '{"time":"22:30","days":["daily"],"action":"scene","params":{"scene_id":"night_light"}}'
curl -X DELETE http://localhost:8500/api/schedule/<rule_id>
```

`action` is one of `power_on`, `power_off`, `scene` (needs `params.scene_id`),
`preset` (needs `params.preset_id`). `days` is `["daily"]` or a list of
weekday numbers (0=Monday .. 6=Sunday).

## Groups (multi-bulb)

```bash
curl http://localhost:8500/api/groups
curl -X POST http://localhost:8500/api/groups/all/power -d '{"on":true}' -H "Content-Type: application/json"
curl -X POST http://localhost:8500/api/groups/all/color -d '{"r":0,"g":255,"b":180}' -H "Content-Type: application/json"
```

## History & diagnostics

```bash
curl http://localhost:8500/api/devices/bulb-1/history
curl -X POST http://localhost:8500/api/devices/bulb-1/test-connection
curl -X POST http://localhost:8500/api/devices/bulb-1/rescan
```

## Audio-reactive lighting

See `docs/music-reactive-lighting.md` for the modes and how the pipeline
works; `.claude/skills/bulb-dashboard-audio/` for a step-by-step guide.
v2 (see `iterations/003-audio-engine-v2/`) cut decision latency to
sub-15ms internally and separated it from `min_dwell_ms`, the minimum time
a color actually stays on the bulb so you can see it change.

```bash
# List capturable audio input devices, all 12 modes, role modes, and dwell defaults
curl http://localhost:8500/api/audio/devices

# Start a session — device_index comes from the list above
curl -X POST http://localhost:8500/api/devices/bulb-1/audio-reactive/start \
  -H "Content-Type: application/json" \
  -d '{"device_index": 1, "mode": "band_fixed", "sensitivity": 1.0, "min_dwell_ms": 90, "n_bands": 3}'

curl -X POST http://localhost:8500/api/devices/bulb-1/audio-reactive/stop

# Live band fractions/rms/beat + per-bulb sender status (latency, errors, dwell)
curl http://localhost:8500/api/devices/bulb-1/audio-reactive/status
```

Modes: `band_fixed`, `dominant_band`, `weighted_blend`, `vu_meter`,
`auto_rotate_hue`, `monochrome_pulse`, `strobe_on_drop`, `palette_cycle`,
`spectrum_gradient`, `band_flash_overlay`, `stereo_split`,
`breathing_silence`. `monochrome_hue` (0-359, `vu_meter`/`monochrome_pulse`
only), `n_bands` (3-16, `spectrum_gradient`/`band_flash_overlay` only), and
`min_dwell_ms` (floor 40ms, default 90ms) are optional on `start`.

### Multi-bulb orchestration

One shared audio analysis drives every bulb in a group, each with its own
independent send pacing (one slow bulb never blocks the others):

```bash
curl -X POST http://localhost:8500/api/groups/all/audio-reactive/start \
  -H "Content-Type: application/json" \
  -d '{"device_index": 1, "mode": "band_fixed", "role_mode": "phase_offset"}'

curl -X POST http://localhost:8500/api/groups/all/audio-reactive/stop
curl http://localhost:8500/api/groups/all/audio-reactive/status
```

`role_mode`: `unison` (identical across every bulb), `phase_offset` (same
effect, hue shifted per bulb for a chase look), `band_split` (bulb *i*
primarily driven by band *i* of an N-band split, N = bulb count).

## Remote access / PIN auth

See `docs/remote-access-security.md` before exposing this beyond your LAN
— Tailscale is the recommended path; DuckDNS+port-forward requires this
PIN gate at minimum.

```bash
# Enable (Settings UI does this too)
curl -X POST http://localhost:8500/api/system/remote-auth/enable \
  -H "Content-Type: application/json" -d '{"pin": "your-real-pin-here"}'

curl -X POST http://localhost:8500/api/auth/login -H "Content-Type: application/json" -d '{"pin":"your-real-pin-here"}'
# -> sets a signed session cookie; subsequent requests need it once enabled

curl -X POST http://localhost:8500/api/auth/logout
curl http://localhost:8500/api/system/remote-auth/disable -X POST
curl http://localhost:8500/api/auth/status   # {"enabled": bool, "authenticated": bool}
```

5 wrong PIN attempts from the same client locks that IP out for 5 minutes,
even for a subsequently-correct PIN — see `iterations/004-pin-gate-remote-auth/`
for what was tested. Repeat lockouts for the same IP double that wait (up to
24h by default), and both the threshold and the durations are configurable.

Weak PINs are refused outright, not warned about: under 6 characters, a
well-known PIN (`1234`, `0000`, …), any of the development/test PINs, a
single repeated character, a straight sequence, or a short pattern padded
out by repetition.

```bash
# Grade a candidate PIN without committing to it (what the Settings UI shows)
curl -X POST http://localhost:8500/api/system/remote-auth/pin-strength \
  -H "Content-Type: application/json" -d '{"pin": "1234"}'
# -> {"ok": false, "strength": "weak", "issues": [...], "hints": [...]}

# Change the household PIN. Revokes every existing session and rotates the
# signing key; the response carries a fresh cookie for the caller.
curl -X POST http://localhost:8500/api/system/remote-auth/pin \
  -H "Content-Type: application/json" -d '{"pin": "your-new-pin"}'

# Guest PINs — open the same gate, revocable on their own (max 5 active)
curl http://localhost:8500/api/system/remote-auth/pins
curl -X POST http://localhost:8500/api/system/remote-auth/pins \
  -H "Content-Type: application/json" \
  -d '{"pin": "guest-pin", "label": "Dog sitter", "expires_in_s": 604800}'
# Revoking a guest PIN also signs out the sessions it opened -- and only those
curl -X DELETE http://localhost:8500/api/system/remote-auth/pins/<pin_id>

# Session TTL and lockout policy (both also in Settings)
curl -X POST http://localhost:8500/api/system/remote-auth/session-ttl \
  -H "Content-Type: application/json" -d '{"session_ttl_s": 28800}'
curl -X POST http://localhost:8500/api/system/remote-auth/lockout-policy \
  -H "Content-Type: application/json" \
  -d '{"max_attempts": 5, "base_seconds": 300, "max_seconds": 86400}'

# Sessions
curl http://localhost:8500/api/auth/sessions
curl -X POST http://localhost:8500/api/auth/sessions/revoke \
  -H "Content-Type: application/json" -d '{"session_id": "<id>"}'
curl -X POST http://localhost:8500/api/auth/sessions/revoke-all
```

## API rate limiting

Separate from the PIN gate's lockout: a per-IP cap on overall request volume
to the public HTTP surface, with different allowances by endpoint
sensitivity. Loopback and LAN clients are exempt by default, so a local-only
setup is unaffected. Over-limit requests get `429` with a `Retry-After`
header. Counters and config are in-memory and reset on restart — the env
vars are the durable way to change a default.

```bash
curl http://localhost:8500/api/system/rate-limit
# -> {"enabled":true,"exempt_local":true,"window_s":60.0,
#     "limits":{"poll":600,"read":240,"write":120,"expensive":10}}

curl -X POST http://localhost:8500/api/system/rate-limit \
  -H "Content-Type: application/json" \
  -d '{"exempt_local": false, "limits": {"write": 60}}'

# How often limits are actually being hit, and by whom (also in Diagnostics)
curl http://localhost:8500/api/system/diagnostics/rate-limit
```

Tiers: `poll` covers the endpoints the dashboard polls on a timer (the audio
panel alone polls 200×/minute, so this budget is deliberately large), `read`
every other safe method, `write` every state-changing method, and
`expensive` the handful of routes that each kick off seconds of real network
I/O (`/api/system/scan`, `/rescan`, `/test-connection`, `/api/audio/calibrate`).
Env overrides: `SBD_RATE_LIMIT_POLL`, `_READ`, `_WRITE`, `_EXPENSIVE`,
`SBD_RATE_LIMIT_EXEMPT_LOCAL=0`.

This is enforced only in the HTTP middleware, so the audio-reactive engine's
internal per-bulb dispatch — which never enters the HTTP stack — can't spend
the budget. That's also why `min_dwell_ms` (how often a *bulb* is written to)
and these limits (how often a *client* may call the API) are separate
concerns that don't interact.

**Behind a reverse proxy**, "the same client" is only meaningful if the
backend can see past the proxy. Set `SBD_TRUSTED_PROXIES` (e.g.
`127.0.0.1,::1`) or every remote user shares one lockout bucket. It is
opt-in and defaults to trusting nothing: `X-Forwarded-For` is attacker
input otherwise, and believing it unconditionally would let anyone forge a
fresh source IP per guess. Full detail and the other TLS-related env vars
(`SBD_HSTS*`, `SBD_HTTPS_REDIRECT`) are in
[docs/deployment.md](docs/deployment.md).

The session cookie is `HttpOnly` and `SameSite=Lax` always, and picks up
`Secure` automatically when the request is actually HTTPS (direct TLS, or
`X-Forwarded-Proto: https` from a trusted proxy). It is deliberately *not*
set over plain HTTP — browsers discard a `Secure` cookie delivered over
plaintext, which would lock LAN users out of their own dashboard.

## Network auto-discovery

See `docs/network-discovery.md` for the full picture;
`.claude/skills/bulb-dashboard-discovery/` for a step-by-step guide.

```bash
curl http://localhost:8500/api/system/discovery
curl -X POST http://localhost:8500/api/system/scan
curl -X POST http://localhost:8500/api/system/discovery/interval \
  -H "Content-Type: application/json" -d '{"hours": 24}'
curl -X POST http://localhost:8500/api/system/discovery/<device_id>/ignore
curl -X POST http://localhost:8500/api/system/discovery/<device_id>/unignore
curl -X DELETE http://localhost:8500/api/system/discovery/<device_id>
```

## Interactive docs

FastAPI auto-generates Swagger UI at `http://localhost:8500/docs` — useful
for exploring/trying every endpoint without curl.
