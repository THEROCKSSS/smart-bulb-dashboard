# Audio-Reactive Lighting

**Status: implemented.** This started as a design-only doc; the pipeline
described below is now real code in `backend/audio_reactive.py`, wired into
the dashboard as the **Audio Reactive** tab. What's still outstanding is
purely *tuning by ear* against real music with the physical bulb online —
the mode-selection architecture, FFT analysis, beat detection, and
rate-limited bulb updates are all built and tested (see
`iterations/002-audio-reactive-lighting/` for exactly what was tested and
two real bugs that testing caught).

## Quick start
1. Go to the **Audio Reactive** tab.
2. Pick an input device — a VoiceMeeter output (e.g. "Voicemeeter Out B1")
   captures whatever's playing on the PC; a real microphone picks up
   whatever's audible in the room. Both are real, tested capture paths on
   this machine.
3. Pick a mode (see the table below) and hit Start. Sensitivity adjusts how
   strongly volume maps to brightness.
4. The live bar meter shows bass/mid/treble energy and beat detection in
   real time, whether or not the bulb itself is reachable — useful for
   confirming the right input device is selected even before the bulb
   responds.

## The 14 implemented modes (v2 added 4, v0.3.0 added 2 more — see `iterations/003-audio-engine-v2/`)

| Mode | What it does |
|---|---|
| `band_fixed` | Bass/mid/treble are blended into hue by whichever is loudest (bass→red/orange, mid→green, treble→blue). This is the literal "bass is a different color" mode. |
| `dominant_band` | Hue snaps toward whichever single band currently dominates, smoothed rather than jump-cut. |
| `weighted_blend` | Continuous hue driven by the spectral centroid (overall tonal brightness of the sound) instead of 3 fixed anchors — less "3 buckets," more a smooth sweep. |
| `vu_meter` | Fixed hue (your choice), brightness is a direct volume meter. Simplest, cheapest mode — good baseline/diagnostic. |
| `auto_rotate_hue` | Hue slowly cycles on its own regardless of content; brightness/beat pulses still track the audio. |
| `monochrome_pulse` | One color of your choice, pulses brighter on the beat, desaturates slightly when quiet. |
| `strobe_on_drop` | Dim warm baseline; a hard bass hit (well above the rolling average, not just any beat) triggers a full white flash. |
| `palette_cycle` | Steps through the dashboard's 25 built-in color presets, advancing one step per detected beat. |
| `spectrum_gradient` | Continuous hue gradient across a configurable 3-16 band split (`n_bands`) — a much finer-grained sibling of `band_fixed`. |
| `band_flash_overlay` | Same N-band gradient as a low-brightness ambient base, with brief full-brightness accent flashes whenever any individual band spikes above its own rolling average. |
| `stereo_split` | Hue leans toward a left or right anchor based on which stereo channel is louder — needs a 2-channel-capable input device (falls back to mono behavior otherwise). |
| `breathing_silence` | During quiet passages, brightness does a slow ~4s breathing oscillation instead of going flat/dark, so the bulb still looks "alive"; smoothly hands over to normal reactive brightness once real audio returns. |
| `harmonic_pairs` | Finds the two most-energetic non-adjacent bands and blends between two complementary (180°-apart) hue anchors by their energy ratio, walking a fixed direction rather than the shortest arc (see the note below on why). |
| `kick_snare_split` | Bass-band energy drives brightness (kick-like pulses); a separate mid/high band drives a hue swing layered on top (snare/hihat-like accent). |

**A real bug, found via its own test suite:** `harmonic_pairs`'s first version blended its two hue anchors with the same shortest-arc circular mean every other mode uses. Since the anchors are exactly 180° apart, a 50/50 energy split makes the two hue vectors cancel to `(0, 0)`, and `atan2` returns whatever direction floating-point noise happens to favor — a real, silent flicker at the single most common "two bands both hot" case. Fixed by walking a fixed linear direction between the two anchors instead, which has no ambiguous case.

## Latency vs. visibility (v2)

The original v1 pipeline rate-limited the *analysis* step itself to ~9Hz,
which meant both "how fast we notice a change" and "how fast the bulb
changes" were the same number, fighting each other. v2 splits these:

- **Decision latency** — how fast the pipeline notices something and
  computes a new target color — is now sub-15ms (512-sample capture
  blocks at 44.1kHz ≈ 11.6ms, zero-padded to a 4096-point FFT for
  reasonable band accuracy despite the smaller window). Every audio
  callback computes and queues a fresh target; nothing artificially
  throttles this anymore.
- **Display dwell** — how long a color actually has to stay on the bulb
  before the next one can replace it — is a separate, tunable
  `min_dwell_ms` (default 90ms, floor 40ms) enforced by a per-bulb
  `BulbSender`. This is what actually answers "I want it fast but I still
  want to be able to see it" — turn dwell down for snappier reaction, up
  for a calmer, more legible feel.
- The sender always sends the *freshest* computed value when its dwell
  window opens, never a backlog — so raising the analysis rate never
  causes lag, it just means the eventual send reflects more up-to-date
  audio.

## Multi-bulb orchestration (v2)

`GroupAudioSession` runs one shared audio analysis across every bulb in a
group (no redundant capture streams), then derives a per-bulb target via
one of three role modes: `unison` (identical), `phase_offset` (same
effect, hue shifted per bulb — a simple chase), `band_split` (bulb *i*
primarily driven by band *i*, e.g. a literal "bass bulb"/"treble bulb"
setup). Each bulb keeps its own independent `BulbSender`, so one slow or
offline bulb in a group never stalls the others. Tested with fake
controllers (unison produces identical output, phase_offset spaces hues
evenly, band_split genuinely differentiates bulbs) and against the real
API with a 1-bulb group — see `iterations/003-audio-engine-v2/`. Real
multi-bulb visual testing needs a second physical bulb (see `ROADMAP.md`).

## The two input modes

### Mode A — System audio loopback (matches what you described with VoiceMeeter)

You already use VoiceMeeter to route audio, which makes this the more
convenient path in practice — the trick is that VoiceMeeter creates a
**virtual audio input device** that mirrors whatever is playing on the PC.

1. Install VoiceMeeter (Banana/Potato work too) if not already set up.
2. Set Windows' default **playback** device to `VoiceMeeter Input`.
3. In VoiceMeeter, route `Hardware Out A1` to your real speakers/headphones
   (so you still hear the music normally).
4. VoiceMeeter also exposes `VoiceMeeter Output (VB-Audio VoiceMeeter VAIO)`
   as a **recording/input** device system-wide — any app that lists
   microphones will see it as a selectable "mic." Our capture script selects
   this device instead of your real microphone, so it "hears" whatever is
   playing on the PC, cleanly, without room noise.

This is functionally the same trick DJ/streaming software uses to route
"what you hear" into something that expects a microphone input.

Alternative without VoiceMeeter: Windows WASAPI supports **loopback capture**
natively — libraries like `sounddevice` can open an output device in
loopback mode and get the same signal without any virtual cable software.
Worth trying first since it needs zero extra installs; VoiceMeeter is more
flexible if you're already routing multiple sources.

### Mode B — Real microphone (ambient listening)

Point a normal mic at whatever's actually playing in the room (speakers,
someone else's music, a TV). Lower fidelity (picks up room echo/noise,
whatever else is happening in the room) but needs zero routing setup — good
for a "just works" fallback, worse for precision.

Both modes feed the exact same analysis pipeline below — the only
difference is which `sounddevice` input index gets opened. **Both were
tested for real on this machine**: capturing from `Voicemeeter Out B1`
(index 1) and from the physical Fifine microphone (index 2) both open
cleanly and produce live, changing band data — see
`iterations/002-audio-reactive-lighting/` for the actual test output.

## Processing pipeline

```
audio in (mono, ~44.1kHz)
  → chunk into ~30-50ms windows (a few hundred samples)
  → apply a Hann window (reduces spectral leakage)
  → FFT (numpy.fft.rfft)
  → bucket into 3 bands: bass (20-250Hz), mid (250-4000Hz), treble (4-20kHz)
  → per-band energy = sum of magnitude² in that band
  → exponential moving average smoothing (avoid single-frame spikes)
  → simple beat/onset detection: is instantaneous bass energy > 1.5x the
    rolling average of the last ~1s? → treat as a "beat" pulse
  → map to HSV:
      - hue: either slowly auto-rotating over time, OR driven by which band
        currently dominates (bass-heavy → warm reds/oranges, treble-heavy →
        blues/whites) — both are worth trying, hue-by-dominant-band tends to
        read as more "reactive"
      - saturation: fixed near 100 (keeps colors vivid)
      - value/brightness: baseline from overall RMS energy, with a short
        brightness pulse on each detected beat
  → rate-limit and send to the bulb (see below)
```

## The real constraint: cheap Wi-Fi bulbs are slow

This bulb (and basically every sub-$20 Tuya Wi-Fi bulb) round-trips a color
command in **roughly 50-100ms** measured locally in this project's own
diagnostics (`test-connection` endpoint). That's a hard ceiling of maybe
10-15 commands/second before commands start queueing up and the bulb visibly
lags behind the music instead of tracking it. Sending a new color every audio
frame (e.g. 30ms = ~33Hz) will NOT work well on this class of hardware.

Practical approach:
- Run the audio analysis loop as fast as you want internally (audio
  processing is cheap).
- But **rate-limit the actual bulb commands** to ~8-10Hz max, always sending
  the *latest* computed color/brightness rather than queuing every frame
  (drop stale frames, never let a backlog build up — a backlog means the
  bulb visibly lags behind the music by however many queued commands are
  ahead of it).
- Expect this to feel like a bulb that "responds to the vibe" of the music
  (brightness pulses on beats, color drifts with the tonal balance) rather
  than a tight per-note LED-strip visualizer. That's a hardware ceiling, not
  a software one — a real per-pixel reactive strip needs a local
  microcontroller (e.g. ESP32 + WS2812) doing the pixel timing itself, which
  is a fundamentally different (and much faster) architecture than
  Tuya's cloud-oriented Wi-Fi bulb protocol.

## Where this actually lives in the codebase

- `backend/audio_reactive.py` — `AudioSession` class. `_process()` is the
  real version of the pipeline sketched above (FFT → band split → beat
  detection → mode dispatch). One important difference from the original
  sketch: bulb I/O is **not** called directly from the audio callback.
  Testing found that tinytuya's blocking socket calls, if run inline in the
  PortAudio callback thread, freeze the entire capture pipeline whenever the
  bulb is slow or offline. The real implementation queues the desired
  action and a separate `_sender_loop` thread does the actual
  `set_hsv()`/`set_rgb()` call, so the audio analysis never blocks on the
  network. See `iterations/002-audio-reactive-lighting/` for the exact
  failure this fixed.
- `backend/main.py` — `GET /api/audio/devices`,
  `POST/GET /api/devices/{id}/audio-reactive/start|stop|status`.
- `frontend/app.js` — `renderAudio()`, the **Audio Reactive** tab, including
  the live bass/mid/treble bar meter.
- Session lifecycle deliberately does **not** reuse `bulb_manager.py`'s
  `_effect_thread`/`_effect_stop` (the mechanism `rainbow`/`pulse`/etc. use)
  — it's a separate `AudioSession` object per device instead, since it has
  its own sender thread and its own always-latest-value semantics that
  don't map cleanly onto the simpler effect-loop pattern.

## Known limitations / good next steps

- **Tuning by ear is still outstanding.** The hue anchors (`BASS_HUE=10`,
  `MID_HUE=130`, `TREBLE_HUE=230`), beat threshold (`1.5x` rolling average),
  and hard-hit threshold (`2.2x`) are reasonable starting values, verified
  correct in *direction* (bass tones land near the bass anchor, louder
  audio raises brightness, silence dims) via synthetic tones — not yet
  verified to *feel* good against real music with the bulb online, since
  the physical bulb was offline for this entire testing session. Adjust the
  constants at the top of `audio_reactive.py` once you can listen and watch
  at the same time.
- **Auto-gain isn't implemented** — the sensitivity slider is a manual
  multiplier, not an automatic level normalizer. If a source is
  consistently too quiet/loud, adjust sensitivity rather than expecting the
  engine to compensate on its own.
- **Multi-bulb / per-band-per-bulb assignment** (one bulb reacts to bass,
  another to treble) is still roadmap-only — needs a second physical bulb
  to build and test against (see `ROADMAP.md`).

## Why this wasn't built into the v1 prototype

Getting a "does it look good reacting to real music" feature right needs
back-and-forth against your actual speakers/VoiceMeeter routing and your
actual taste in how reactive vs. how smooth it should feel — that's
fundamentally an iterative, ears-on process, not something to guess at in
one shot alongside the rest of this prototype. This doc exists so that
follow-up session starts with a concrete architecture instead of a blank
page.
