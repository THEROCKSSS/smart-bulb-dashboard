---
name: bulb-dashboard-audio
description: "Set up and drive audio-reactive lighting on the Smart Bulb Dashboard: pick an input device, choose from 12 modes, tune sensitivity/dwell, orchestrate multiple bulbs."
---

# Smart Bulb Dashboard — Audio Reactive

## When to use
User wants the bulb to react to music/audio — either PC audio (via
VoiceMeeter or WASAPI loopback) or a real microphone. Assumes the backend is
running (`bulb-dashboard-setup` skill if not) and `sounddevice`/`numpy` are
installed (already in `requirements.txt`).

## The procedure

### 1. Find the right input device
```bash
curl -s http://localhost:8500/api/audio/devices
```
Look for a `Voicemeeter Out ...` or `CABLE Output ...` entry if the user
routes PC audio through VoiceMeeter/VB-Cable — that captures whatever's
playing on the machine. Otherwise pick a `Microphone (...)` entry for
ambient/room listening. Note the `index` field.

If using VoiceMeeter: Windows' **playback** device must be set to
`Voicemeeter Input` first, or nothing will be routed through it to capture.

### 2. Start a session
```bash
curl -X POST http://localhost:8500/api/devices/<id>/audio-reactive/start \
  -H "Content-Type: application/json" \
  -d '{"device_index": 1, "mode": "band_fixed", "sensitivity": 1.0, "min_dwell_ms": 90, "n_bands": 3}'
```
Modes (see `docs/music-reactive-lighting.md` for what each does — 12
total): `band_fixed`, `dominant_band`, `weighted_blend`, `vu_meter`,
`auto_rotate_hue`, `monochrome_pulse`, `strobe_on_drop`, `palette_cycle`,
`spectrum_gradient`, `band_flash_overlay`, `stereo_split`,
`breathing_silence`.
- `vu_meter`/`monochrome_pulse` also accept `monochrome_hue` (0-359).
- `spectrum_gradient`/`band_flash_overlay` also accept `n_bands` (3-16).
- `min_dwell_ms` (floor 40, default 90) controls how long each color stays
  visible before the next can replace it — this is separate from decision
  latency (which is always sub-15ms internally as of v2); lower it for a
  snappier feel, raise it if changes feel too fast to actually see.

### 2b. Orchestrate multiple bulbs (once there's more than one)
```bash
curl -X POST http://localhost:8500/api/groups/<group_id>/audio-reactive/start \
  -H "Content-Type: application/json" \
  -d '{"device_index": 1, "mode": "band_fixed", "role_mode": "phase_offset"}'
curl -X POST http://localhost:8500/api/groups/<group_id>/audio-reactive/stop
curl -s http://localhost:8500/api/groups/<group_id>/audio-reactive/status
```
`role_mode`: `unison` (identical), `phase_offset` (chase effect, hue
shifted per bulb), `band_split` (bulb *i* primarily driven by band *i*).
One shared audio analysis drives the whole group — don't also start an
individual `/audio-reactive/start` session on a bulb that's already in an
active group session, they'll both try to control it.

### 3. Watch it work
```bash
curl -s http://localhost:8500/api/devices/<id>/audio-reactive/status
```
Returns `bands: {fractions: [...], rms, is_beat}` (an N-length array, 3 by
default) plus `sender: {last_latency_ms, error, min_dwell_ms}` — the
fractions should be visibly different across repeated calls while audio is
playing. The dashboard's **Audio Reactive** tab shows this live as a bar
meter, polling every ~300ms.

### 4. Stop it
```bash
curl -X POST http://localhost:8500/api/devices/<id>/audio-reactive/stop
```
Also auto-stops after 5 minutes of near-silence.

## Pitfalls

1. **Bands frozen at the exact same values across multiple status polls** —
   this was a real bug (bulb I/O blocking the audio callback when the bulb
   was slow/offline), already fixed by decoupling bulb sends onto their own
   thread. If you see this on current code, it's a *new* issue, not the old
   one — check the bulb's own reachability first (`/test-connection`), then
   look at `audio_reactive.py`'s `_sender_loop`/`_queue_action` for
   regressions.
2. **No devices listed, or starting a session errors immediately** —
   PortAudio couldn't enumerate/open a device. Confirm `sounddevice` is
   installed in the same venv running the backend (`pip show sounddevice`),
   and that this isn't running inside Docker (audio devices generally
   aren't passed through to containers).
3. **Don't call `sounddevice`/tinytuya directly from a custom script while
   a session is running** — two processes fighting over the same audio
   device or bulb will misbehave. Use the API's start/stop, which tracks
   one session per device ID (`audio_reactive._sessions`) and cleanly
   replaces any prior session for that device.
4. **Silence produces a low but nonzero brightness (~4%), not full off** —
   intentional (a real bug where the floor never actually dropped below
   25% was found and fixed — see `iterations/002-audio-reactive-lighting/`).
   If the baseline feels too bright or too dim during quiet passages,
   adjust the constant in `audio_reactive.py`'s `base_brightness`
   calculation, not the sensitivity slider (sensitivity scales with
   volume, it doesn't change the floor).

## Verification
After starting a session, confirm it's actually alive and reacting, not
just accepted:
```bash
curl -s http://localhost:8500/api/devices/<id>/audio-reactive/status
# wait a second or two, then:
curl -s http://localhost:8500/api/devices/<id>/audio-reactive/status
```
The `bands` values should differ between the two calls whenever there's
real audio playing (or even just room noise). Identical values across
multiple calls seconds apart means the pipeline is stuck — see Pitfall #1.
