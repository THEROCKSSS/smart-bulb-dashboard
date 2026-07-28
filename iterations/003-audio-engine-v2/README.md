# 003 — Audio Engine v2: Lower Latency, More Modes, Multi-Bulb Orchestration

## Goal
Rework the audio-reactive engine so decision latency is as low as
practically possible ("down to the millisecond" was the literal ask),
while keeping a configurable minimum dwell time so color changes stay
perceivable; add many more interpretation modes and configurable band
counts; and add multi-bulb orchestration (unison/phase-offset/band-split)
reusing the existing `groups` concept, ahead of buying more physical bulbs.

## Approach
- **Latency**: dropped capture block size from 1024 → 512 samples
  (~23ms → ~11.6ms per callback), zero-padding to a 4096-point FFT so band
  energy estimates stay reasonably accurate despite the smaller window
  (padding is free — it doesn't add latency, only interpolates the
  spectrum). Removed the old artificial rate-gate inside the analysis step
  entirely — `_process()` now computes and queues a fresh target on
  *every* callback (~86Hz), so decision latency is bounded only by the
  block time, not by an arbitrary throttle.
- **Dwell**: split "how fast we decide" from "how fast the bulb actually
  changes and how long each color is visible" into a new `BulbSender`
  class — one per bulb, running its own thread, enforcing
  `min_dwell_ms` (default 90ms, floor 40ms, both configurable) between
  real sends, but always sending the *freshest* queued value, never a
  backlog. This directly answers the two competing asks (near-instant
  reaction vs. actually being able to see it) by making them independently
  tunable instead of one setting fighting the other.
- **More modes**: added `spectrum_gradient` (continuous hue across a
  configurable 3-16 band split), `band_flash_overlay` (ambient gradient
  base + per-band accent flashes on individual spikes), `stereo_split`
  (hue driven by left/right channel balance, needs 2-channel input),
  `breathing_silence` (slow ambient breathing instead of going flat during
  quiet passages) — bringing the total to 12 modes.
- **Orchestration**: new `GroupAudioSession` runs ONE shared capture
  stream and analysis pass, then derives a per-bulb target for every bulb
  in a group via three role modes (`unison`, `phase_offset`, `band_split`),
  each bulb still getting its own independent `BulbSender` so one slow
  bulb can't stall the others.

## What happened
The core rework (latency/dwell split, new modes, orchestration) worked
largely as designed. Code review during the rewrite caught one real
correctness bug before it ever reached testing; the rest of the failures
below were test-design mistakes on my part, not product bugs — documented
anyway since they reveal genuinely non-obvious behavior of the new
`BulbSender` timing model that's worth understanding before extending it.

## Failures

**1. Circular hue smoothing (real bug, caught in review).** Every mode
that eases toward a target hue used a plain linear blend
(`old*(1-a) + target*a`). This breaks at the 0°/360° wrap boundary — e.g.
blending 358° and 3° (only 5° apart on the color wheel) with a linear
blend produces ~180°, a completely wrong color. This was latent but
harmless in the original 3 modes (their anchors — 10°, 130°, 230° — never
range far enough to cross the wrap), but the new `stereo_split` mode's
target *does* cross it (`LEFT_HUE=200` sweeping up through `380%360=20`).
**Fixed** by adding `_smooth_hue()`, a proper circular-mean blend (average
unit vectors, then `atan2` back to degrees), applied to all 6 smoothing
call sites for consistency, not just the one that would have visibly
broken.

**2. Test assumed BulbSender's dwell window applies even to a session's
very first send.** First test attempt queued a value expecting it to be
"held" for the dwell period before a stale value could overwrite it — but
a sender with no prior send has no dwell to respect, so the very first
queued value goes out almost immediately. Not a bug: there's no reason to
artificially delay the first-ever command. Fixed the test to establish a
real dwell window first (send once, then test overwrite behavior within
the *next* window).

**3. Test slept longer than the dwell window before queuing a "stale"
value.** Second attempt fixed #2 but then slept 150ms (more than the
100ms dwell) before queuing the stale value — by then the previous dwell
window had already elapsed, so the "stale" value was sent immediately
too, producing 3 sends instead of the expected 2. Fixed by queuing well
inside the still-open dwell window (20ms after the first send, versus a
100ms dwell), which correctly exercised the "overwrite before send"
path and produced exactly 2 sends, the second being the freshest value.

## Verification
1. **Circular smoothing**: `_smooth_hue(358, 3, 0.5)` → landed near 0/360
   (not ~180), confirming the wrap case now works; a normal mid-range
   blend (`_smooth_hue(10, 130, 0.5)` → ~70) still behaves as expected.
2. **Dwell enforcement**: queuing 40 actions in rapid succession (5ms
   apart) against a sender with `min_dwell_ms=80` produced only 4 actual
   sends, with measured gaps of 84-94ms between them — dwell is real and
   respected even under a flood of updates.
3. **Freshness guarantee**: with a correctly-timed test (see Failure #3),
   confirmed exactly 2 real sends where 2 values were queued within one
   dwell window plus the initial one — the stale intermediate value never
   reached the controller.
4. **New modes**, tested against the real `AudioSession`/`GroupAudioSession`
   classes with synthetic tones: `spectrum_gradient` correctly places a
   60Hz tone at a lower hue than a 9kHz tone across a 6-band split;
   `stereo_split` correctly biases hue toward the anchor matching whichever
   synthetic stereo channel was louder; `breathing_silence` never crashes
   on a literal zeroed signal and its brightness genuinely oscillates
   rather than sitting flat.
5. **Orchestration**, tested with 3 fake controllers: `unison` produces
   identical hues across all 3 bulbs (their independent smoothing states
   evolve identically given identical shared input — confirmed, not just
   assumed); `phase_offset` spaces 3 bulbs' hues ~120° apart as expected;
   `band_split` produces genuinely different hues per bulb under a single
   pure-bass tone (proving each bulb really is weighted toward a different
   band, not just relabeled).
6. **Real hardware**: restarted the backend, confirmed `GET /api/audio/devices`
   reports all 12 modes / 3 role modes / dwell defaults; started a real
   capture session (`spectrum_gradient`, physical mic, `n_bands: 6`) and
   confirmed `fractions` in `/status` genuinely differ between polls a
   second apart (e.g. `[0.008,...,0.606]` → `[0.001,...,0.654]`) — the
   pipeline is live, matching the regression check from iteration 002.
   Also exercised the new group endpoints end-to-end
   (`POST /api/groups/all/audio-reactive/start|stop`) against the real
   "all" group (currently 1 bulb) — clean start, live status, clean stop.
7. **Known limitation observed, not fixed**: with the bulb still offline,
   `sender.last_latency_ms`/`error` stayed `null` for at least 7 seconds
   after starting a session — suggesting tinytuya's connect attempt against
   a genuinely unreachable device takes longer than that to fail. This
   doesn't freeze anything (the audio analysis kept updating throughout,
   the whole point of decoupling the sender), but it does mean that bulb's
   single sender thread stays occupied with one long-hanging attempt for a
   while, delaying how quickly it picks up a fresh value once the bulb
   actually comes back. Worth a future iteration (e.g. an explicit shorter
   socket timeout passed to tinytuya specifically for audio-reactive sends)
   if it proves annoying in practice — not addressed here since it needs
   the bulb online to tune against real timing.
