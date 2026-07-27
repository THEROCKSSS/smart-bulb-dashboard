---
name: bulb-dashboard-control
description: "Drive the Smart Bulb Dashboard API: change color, run effects, apply scenes, set timers."
---

# Smart Bulb Dashboard — Control

## When to use
User wants to change bulb color/brightness, apply a mood scene, run an
animated effect, set a sleep/wake timer, or otherwise operate a bulb already
configured in this project (see `bulb-dashboard-setup` skill if not
configured yet). Assumes the backend is running (`http://localhost:8500`
by default) and a device `id` exists in `backend/config.json`.

## The procedure

### Check it's alive first
```bash
curl -s http://localhost:8500/api/devices/<id>/status
```
Look for `"online": true`. If `false`, the bulb likely lost power/Wi-Fi —
this is common with cheap Wi-Fi bulbs and not a dashboard bug. Power-cycle
the physical bulb and retry before debugging further.

### Change color
```bash
# RGB
curl -X POST http://localhost:8500/api/devices/<id>/color \
  -H "Content-Type: application/json" -d '{"r":255,"g":0,"b":0}'

# HSV (h: 0-359 degrees, s/v: 0-100)
curl -X POST http://localhost:8500/api/devices/<id>/color/hsv \
  -H "Content-Type: application/json" -d '{"h":120,"s":100,"v":100}'
```

### Change brightness (mode-aware — safe in both white and color mode)
```bash
curl -X POST http://localhost:8500/api/devices/<id>/brightness \
  -H "Content-Type: application/json" -d '{"value":60}'
```
This dims the *current* color if the bulb is in colour mode, or the white
brightness if in white mode — it does not silently switch modes (that was a
real bug, fixed; see `bulb_manager.py`'s `set_brightness()` docstring).

### Apply a scene / preset (see `/api/scenes` and `/api/presets` for the full lists)
```bash
curl -X POST http://localhost:8500/api/devices/<id>/scenes/apply \
  -H "Content-Type: application/json" -d '{"scene_id":"movie_night"}'

curl -X POST http://localhost:8500/api/devices/<id>/presets/apply \
  -H "Content-Type: application/json" -d '{"preset_id":"ocean"}'
```

### Run / stop an animated effect
```bash
curl -X POST http://localhost:8500/api/devices/<id>/effects/start \
  -H "Content-Type: application/json" -d '{"effect":"rainbow","speed":1.5}'
curl -X POST http://localhost:8500/api/devices/<id>/effects/stop
```
Effect IDs: `rainbow`, `pulse`, `strobe`, `candle`, `fade`, `color_loop`,
`random`. `speed` is a multiplier (1.0 = default pace); effects run in a
background thread until explicitly stopped or the process restarts.

### Timers
```bash
# Sleep timer — fades out over the last ~20% of duration, then turns off
curl -X POST http://localhost:8500/api/devices/<id>/timers/sleep \
  -H "Content-Type: application/json" -d '{"minutes":30}'

# Wake/sunrise timer
curl -X POST http://localhost:8500/api/devices/<id>/timers/wake \
  -H "Content-Type: application/json" \
  -d '{"time":"07:00","brightness":100,"color_temp":70,"fade_minutes":15}'
```

### Full reference
`API.md` in the repo root has every endpoint (groups, schedule rules,
favorites, diagnostics). `http://localhost:8500/docs` has interactive
Swagger docs generated straight from the running code.

## Pitfalls

1. **Don't hand-write raw Tuya `dps` payloads** — always go through this
   API's endpoints (`/color`, `/brightness`, etc.), which already handle the
   mode-aware brightness logic and the HSV↔dp24 hex encoding correctly.
   Bypassing it (e.g. calling tinytuya directly) reintroduces the
   brightness-mode-flip bug this project already fixed once.
2. **Rapid-fire commands** — cheap Tuya Wi-Fi bulbs round-trip a command in
   roughly 50-100ms. Sending commands faster than ~10/sec (e.g. from a
   custom script looping tightly) will queue up and visibly lag. The
   built-in effects already rate-limit themselves appropriately — mimic
   their pacing if writing a new automation.
3. **Status looking incomplete right after a color change** — some
   firmware responses only return the delta `dps` that changed, not a full
   snapshot. The backend already merges deltas into a cached full state
   (`bulb_manager.py`'s `_last_dps`), so always read status via this API,
   not directly from a raw tinytuya call.

## Verification
After any change, re-fetch status and confirm the field you changed
actually moved:
```bash
curl -s http://localhost:8500/api/devices/<id>/status
```
e.g. after setting `{"h":120,...}`, confirm `"hue": 120` (or close to it —
HSV↔RGB round-tripping can shift it by a degree or two) in the response.
