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

## Interactive docs

FastAPI auto-generates Swagger UI at `http://localhost:8500/docs` — useful
for exploring/trying every endpoint without curl.
