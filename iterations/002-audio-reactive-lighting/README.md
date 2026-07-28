# 002 — Audio-Reactive Lighting

## Goal
Turn `docs/music-reactive-lighting.md` (previously a design-only doc) into a
real, working feature: capture audio from a chosen input device (VoiceMeeter
virtual cable or a real microphone — this machine has both), analyze it in
real time, and drive the bulb's color/brightness from it, with several
distinct interpretation modes per the user's request ("bass has a different
color... I want multiple different features around that idea").

## Approach
- `backend/audio_reactive.py`: `AudioSession` class using `sounddevice` +
  `numpy`. Captures via `sd.InputStream` with a callback, runs a Hann-window
  FFT per ~23ms block, splits into bass/mid/treble energy, tracks a rolling
  bass average for simple beat/hard-hit detection, and maps the result to a
  bulb command through the existing `controller.set_hsv()`/`set_rgb()`.
- 8 selectable modes: `band_fixed`, `dominant_band`, `weighted_blend`,
  `vu_meter`, `auto_rotate_hue`, `monochrome_pulse`, `strobe_on_drop`,
  `palette_cycle` — covering the "each frequency band is a different color"
  ask plus several distinct interpretation styles.
- Bulb commands rate-limited to 9Hz, always sending the latest computed
  value rather than queuing (matches the original design doc's stated
  hardware ceiling for cheap Wi-Fi bulbs).
- New endpoints (`main.py`): `GET /api/audio/devices`,
  `POST/GET /api/devices/{id}/audio-reactive/start|stop|status`.
- New "Audio Reactive" tab in the frontend: device/mode pickers, a
  sensitivity slider, a live bass/mid/treble bar meter polling `/status`
  every 300ms, start/stop.
- Installed `sounddevice`+`numpy` into the project's own `backend/venv`
  (never the shared `hermes-agent` venv — see project convention).

## What happened
Wiring, device listing, and the FFT/band-split math all worked as designed.
Two real bugs were found through actual testing before this could be called
done — both are the kind that only show up when you run the code against
real hardware/signals rather than reading it.

## Failures

**1. Brightness floor never actually reached the floor.** The original
brightness formula was `min(100, max(3, 25 + rms*gain*3500))`. The
`max(3, ...)` guard looked like it should let brightness dim to as low as
3% during silence, but `25 + 0 = 25` always beats `3`, so the `max(3, ...)`
was dead code — true silence still produced 25% brightness, not a dim
floor. **Found by**: a unit test feeding a literal zeroed (`np.zeros`)
waveform through the real `AudioSession._process()` and asserting
brightness stayed low; it came back at 25 instead of near-zero. **Fixed**
by changing the additive constant from `25` to `4` (`4 + rms*gain*4500`),
so silence now genuinely dims toward a low floor and the scaling factor was
bumped to compensate for typical rms ranges. Re-verified with the same
test — brightness now reads ~4 for silence.

**2. Bulb I/O inside the audio callback froze the entire capture pipeline
when the bulb was slow/offline — a real, reproducible bug, not a
hypothetical.** The original code called
`self.controller.set_hsv(...)` directly from inside `_process()`, which
itself is called synchronously from PortAudio's own callback thread
(`sd.InputStream(..., callback=callback)`). tinytuya's `set_colour()`
blocks on a real TCP socket connect/send. Testing happened to coincide with
the physical bulb being offline (confirmed independently via `ping` timing
out and a raw `tinytuya scan` finding 0 devices — see
[[001-network-auto-discovery]] for the same root cause). Starting a real
audio-reactive session against the live microphone and polling
`/audio-reactive/status` every second for 4 seconds returned **byte-for-byte
identical** band values across all 4 polls — the analysis had frozen after
the very first frame, because the callback thread was stuck inside the
blocking `set_colour()` call and never returned to process new audio
buffers. This would affect not just a fully offline bulb but any slow/laggy
response, actively working against the rate-limiting this feature was
supposed to have.

## Fix
Decoupled bulb I/O from the audio callback entirely. `AudioSession` now
runs a second background thread (`_sender_loop`) that owns all actual
`controller.set_*` calls. `_process()` (called from the audio callback)
only ever calls `_queue_action(...)`, which stores the latest desired
action in a single-slot field behind a lock and sets a
`threading.Event` — it never blocks. The sender thread wakes on that event,
sends the latest action, and drops anything queued in the meantime,
regardless of how long the previous send took. This preserves the
"always send the newest value, never build a backlog" rate-limiting intent
from the original design doc, but now applies it to the network call too,
not just the analysis rate.

## Verification
1. **Real hardware, the exact failure condition**: with the physical bulb
   confirmed offline (same state as iteration 001), started a real session
   on the physical Fifine microphone (`device_index: 2`) via the actual
   API. Before the fix: 4 consecutive 1-second-apart polls of
   `/audio-reactive/status` returned identical band values — frozen. After
   the fix: 5 consecutive polls each showed different, plausible values
   (e.g. `bass/mid/treble` shifting frame to frame, `rms` moving, one poll
   catching a real `is_beat: true` from ambient room noise) — the pipeline
   is provably live, not stuck, even with the bulb unreachable the entire
   time.
2. **Clean start/stop**: `POST .../start` → poll → `POST .../stop` →
   `GET .../status` returns `{"active": false}` with no hang.
3. **A second real device**: repeated the start/poll/stop cycle against
   `device_index: 1` (`Voicemeeter Out B1`, the actual VoiceMeeter output
   this machine is configured with) in `vu_meter` mode — confirmed it opens
   and reports live bands the same way.
4. **Synthetic signal correctness** (`test_audio_reactive_logic.py`,
   against the real `AudioSession._process()`, not a reimplementation):
   - A 60Hz tone converges `band_fixed`'s hue near the bass anchor (10°);
     an 8000Hz tone converges it near the treble anchor (230°).
   - `dominant_band` locks tightly (within 15°) onto the bass anchor for a
     pure bass tone.
   - `vu_meter` brightness rises with input loudness while hue never
     changes.
   - A zeroed (silent) signal produces low, stable brightness — confirming
     fix #1 above.
5. Not yet verified: the actual visual "does this look good" quality of
   each mode against real music, since the bulb was offline for the whole
   testing window. That's consistent with the original design doc's stated
   philosophy — getting the feel right needs ears-on iteration against real
   music and the actual bulb, which is a natural follow-up once the bulb is
   back on Wi-Fi.
