# API Reference

Base URL: `http://localhost:8500` (or your host/port). All bodies are JSON.
Replace `bulb-1` with your device's `id` from `config.json`.

## System

```bash
curl http://localhost:8500/api/system/health
curl http://localhost:8500/api/system/info
```

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
for what was tested.

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

## Security audit log

See `docs/security-secrets.md` for severities, alerting defaults and the
incident-response checklist. Distinct from `/api/devices/<id>/history`,
which is what the *bulbs* did.

```bash
# Search/filter. All filters AND together; `q` is a substring match over
# the whole entry, which is how "find everything about 10.0.0.5" works.
curl "http://localhost:8500/api/security/events?limit=100&min_severity=warning"
curl "http://localhost:8500/api/security/events?event=login_lockout&q=10.0.0.5"
curl "http://localhost:8500/api/security/events?include_rotated=true"

# Export for external review. JSON keeps the prev/hmac chain fields, so the
# export can still be verified independently.
curl "http://localhost:8500/api/security/events/export?format=csv" -o events.csv

# Tamper check. Read-only by design -- there is deliberately no "re-seal"
# button, since that would let tampering be papered over with one click.
curl http://localhost:8500/api/security/verify
# -> {"ok": true, "complete": true, "entries": 42, "first_bad_seq": null, ...}
#    ok=true + complete=false means retention pruned old segments (normal),
#    not that anything was tampered with.

curl -X POST http://localhost:8500/api/security/events/rotate
curl -X POST http://localhost:8500/api/security/self-test   # canary + wiring report
curl "http://localhost:8500/api/security/digest?days=7"     # reports even a quiet week

# Alerts (local queue; the webhook is off by default)
curl http://localhost:8500/api/security/alerts
curl -X POST http://localhost:8500/api/security/alerts/ack

# Settings
curl http://localhost:8500/api/security/config
curl -X POST http://localhost:8500/api/security/config \
  -H "Content-Type: application/json" \
  -d '{"alert_min_severity": "warning", "webhook_enabled": true,
       "webhook_url": "https://example.com/hook", "retention_days": 90}'

# Where each secret lives and what it's worth -- never any values
curl http://localhost:8500/api/security/secrets
```

## Backup & restore

**A backup contains every bulb's `local_key` in plaintext.** Read
`docs/backup-restore.md` before automating any of this.

```bash
# Encrypted (recommended). Omitting `password` produces a plain zip AND an
# explicit `warning` field in the response -- there is no silent default.
curl -X POST http://localhost:8500/api/backups \
  -H "Content-Type: application/json" \
  -d '{"password": "…", "note": "before the move", "exclude": ["lightshows"]}'

curl http://localhost:8500/api/backups          # newest first, + retention setting
curl http://localhost:8500/api/backups/options  # what can be excluded / selectively restored
curl -o backup.zip http://localhost:8500/api/backups/<name>/download

# Passwords go in a POST body, never a query string: a query string lands in
# access logs, browser history and referrer headers.
curl -X POST http://localhost:8500/api/backups/<name>/verify \
  -H "Content-Type: application/json" -d '{"password": "…"}'
curl -X POST http://localhost:8500/api/backups/<name>/preflight \
  -H "Content-Type: application/json" -d '{"password": "…"}'
curl -X POST http://localhost:8500/api/backups/<name>/diff \
  -H "Content-Type: application/json" -d '{"password": "…"}'

# Restore. `confirm` is required (409 without it); a pre-restore safety
# backup is always taken first. `sections` omitted = full restore.
curl -X POST http://localhost:8500/api/backups/<name>/restore \
  -H "Content-Type: application/json" \
  -d '{"password": "…", "confirm": true, "sections": ["favorites", "schedules"]}'
# -> includes {"remote_access": {"enabled_before": …, "enabled_after": …,
#              "changed": false}} -- a restore can never flip the PIN gate.

curl -X DELETE http://localhost:8500/api/backups/<name>   # overwritten, then unlinked
curl -X POST http://localhost:8500/api/backups/settings \
  -H "Content-Type: application/json" -d '{"keep": 10}'
```

## Interactive docs

FastAPI auto-generates Swagger UI at `http://localhost:8500/docs` — useful
for exploring/trying every endpoint without curl.
