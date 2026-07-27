# Design Insight: Music-Reactive Lighting

**Status: design doc only — nothing in this file is implemented.** This is
written to give a concrete, buildable plan for a follow-up project, per the
request to sketch out "how you'd make the light react to music, including
listening through a mic/VoiceMeeter." It intentionally is not wired into the
dashboard yet, because getting this to feel *good* needs iteration against
real audio hardware and a real ear, which is a separate work session from
building the core control dashboard.

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
difference is which `sounddevice` input index gets opened.

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

## How this would plug into the existing codebase

The clean integration point is as a new **effect**, following the exact
pattern `bulb_manager.py` already uses for `rainbow`/`pulse`/etc.:

```python
# sketch — not implemented
def _run_music_reactive(self, input_device_index):
    import sounddevice as sd
    import numpy as np

    rolling_bass_energy = collections.deque(maxlen=32)  # ~1s of history
    last_sent = 0

    def audio_callback(indata, frames, time_info, status):
        nonlocal last_sent
        now = time.time()
        if now - last_sent < 0.1:  # rate-limit to 10Hz
            return
        samples = indata[:, 0]
        windowed = samples * np.hanning(len(samples))
        spectrum = np.abs(np.fft.rfft(windowed))
        freqs = np.fft.rfftfreq(len(windowed), 1 / 44100)

        bass = spectrum[(freqs >= 20) & (freqs < 250)].sum()
        mid = spectrum[(freqs >= 250) & (freqs < 4000)].sum()
        treble = spectrum[(freqs >= 4000) & (freqs < 20000)].sum()

        rolling_bass_energy.append(bass)
        avg_bass = sum(rolling_bass_energy) / len(rolling_bass_energy)
        is_beat = bass > avg_bass * 1.5

        total = bass + mid + treble + 1e-9
        if bass / total > 0.5:
            hue = 10       # warm red/orange
        elif treble / total > 0.4:
            hue = 200      # cool blue
        else:
            hue = 280      # mid → violet

        brightness = min(100, 40 + (bass / total) * 60 + (25 if is_beat else 0))
        self.set_hsv(hue, 100, brightness)  # reuses the existing method — no new Tuya protocol code needed
        last_sent = now

    with sd.InputStream(device=input_device_index, channels=1, samplerate=44100, callback=audio_callback):
        while not self._effect_stop.is_set():
            self._effect_stop.wait(0.05)
```

Wiring it in for real would mean:
- Adding `sounddevice` + `numpy` to `requirements.txt`.
- A new `/api/devices/{id}/music-reactive/start` endpoint that takes an
  `input_device_index` (or name) and starts this as another background
  thread, reusing `stop_effect()`/`_effect_stop` the same way every other
  effect does.
- A Settings-panel dropdown listing available input devices
  (`sounddevice.query_devices()`) so you can pick the VoiceMeeter output (or
  your real mic) from the UI instead of hardcoding a device index.
- Tuning the hue-mapping and beat threshold by ear against your actual
  music library — this is the part that most benefits from being its own
  follow-up session rather than guessed upfront.

## Why this wasn't built into the v1 prototype

Getting a "does it look good reacting to real music" feature right needs
back-and-forth against your actual speakers/VoiceMeeter routing and your
actual taste in how reactive vs. how smooth it should feel — that's
fundamentally an iterative, ears-on process, not something to guess at in
one shot alongside the rest of this prototype. This doc exists so that
follow-up session starts with a concrete architecture instead of a blank
page.
