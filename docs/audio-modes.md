# Audio-Reactive Modes — what each one does, and which to pick

20 modes and 24 genre presets. This explains what each actually does, so you
can pick on purpose rather than by cycling through them.

**One caveat, stated up front and honestly:** the preset values are reasoned
from what each mode's maths does, not tuned by ear. The bulb has been offline
or untested against real music for most of this project's build sessions.
Treat them as informed starting points. If something feels wrong, it probably
is — the two dials worth touching first are **sensitivity** and **dwell**.

---

## Just tell me what to use

| Situation | Preset | Why |
|---|---|---|
| **Party, mixed playlist** | `Party / Mixed Playlist` | Never wrong for the current track. Also never tailored to it. |
| **Party, electronic** | `House / Four-on-the-Floor` | The whole room pulses on the kick. Reads as one instrument, not a light show. |
| **Party, hard/fast** | `Drum & Bass` or `Techno / Dark` | Built for 165+ BPM, where anything slower smears. |
| **General listening** | `Pop / Radio` | The safest default. Blends all bands so no single one dominates. |
| **Slow / background** | `R&B / Soul` or `Movie Night` | Follows phrasing, not percussion. Long dwell, no twitching. |
| **Nothing playing** | `Ambient / Drone` | Keeps moving without implying a beat that isn't there. |

**Photosensitivity:** two modes are flash-heavy — `strobe_on_drop` and
`band_flash_overlay`. A hard cap of **3 flashes/second** is enforced in the
send path (WCAG 2.3.1, ITU-R BT.1702), so they can't exceed it even if
configured to. If anyone present is photosensitive, use the
**disable-flash-heavy** toggle rather than relying on the cap alone.

---

## How the engine works

Audio arrives in **512-sample blocks at 44.1 kHz** — about **86 frames per
second**. Each block gets a Hann window and a 4096-point FFT, then the
spectrum is summed into **log-spaced bands** (log, not linear, because that
matches how music distributes energy and how ears hear pitch).

Two numbers come out of every frame and drive everything else:

- **Band fractions** — each band's share of total energy. *Fractions*, so
  turning the volume up doesn't change the colour, only the brightness.
- **RMS** — overall loudness, used for brightness and silence detection.

**Beat detection uses the bass band, not the whole mix.** This matters more
than it sounds. Broadband loudness sums everything, so a sustained pad or a
loud vocal raises the floor a kick has to clear — and the beat gets buried
exactly when the track is densest. Measured across 70–174 BPM on a dense mix:
broadband found the tempo **1 time in 9**; the bass band found it **9 times in
9**. Broadband remains the fallback for sources with no low end.

### The two dials that matter

**`sensitivity`** (0.5–2.0) multiplies input gain. Too low and quiet tracks
barely register; too high and everything saturates so the lights stop
distinguishing loud from quiet. Start at 1.0 and adjust toward the material.

**`min_dwell_ms`** is how long a colour must stay before it may change —
independent of how fast analysis runs. This exists because the bulb has a real
round trip (typically 50–200 ms). Analysis at 86 fps would outrun it, so dwell
paces what's actually sent.

- **40–60 ms** — fast electronic. Below the 40 ms floor is refused.
- **90 ms** — the default, roughly this bulb's measured round trip.
- **200–400 ms** — slow, ambient, or background use.

Dwell is *also* the main comfort control: short dwell on slow music looks
nervous, long dwell on fast music looks late.

---

## The 20 modes

### Colour follows the spectrum

| Mode | What it does | Use when |
|---|---|---|
| `band_fixed` | Each band owns a fixed hue; you see the blend. | You want the mix's *shape* visible. Predictable. |
| `dominant_band` | Snaps to the loudest band's hue. | You want obvious, decisive colour changes. |
| `weighted_blend` | Blends all band hues by energy share. | Mixed playlists. The most forgiving mode here. |
| `spectrum_gradient` | Full N-band gradient across the spectrum. | Textural music where arrangement matters more than beat. |
| `harmonic_pairs` | Two most-energetic non-adjacent bands → complementary hues. | Wideband, unpredictable audio (games, film). |
| `stereo_split` | Left/right channel balance drives hue. | Genuinely stereo material. Useless on mono. |

### Brightness follows loudness

| Mode | What it does | Use when |
|---|---|---|
| `vu_meter` | Brightness tracks level; hue barely moves. | Acoustic and classical. Calm, never distracting. |
| `monochrome_pulse` | One hue, brightness pulses. | Speech, podcasts, or a fixed room colour. |
| `bass_only_pulse` | Brightness driven purely by bass share; hue pinned. | Four-on-the-floor. The kick *is* the visual. |
| `energy_contour` | Smoothed energy envelope, slow-moving. | Following vocal phrasing rather than drums. |
| `crescendo_ramp` | Detects a sustained rise and ramps *ahead* of the peak. | Orchestral and film score. Makes builds feel anticipated. |

### Motion-led

| Mode | What it does | Use when |
|---|---|---|
| `auto_rotate_hue` | Hue rotates continuously; beat drives brightness. | Long mixed sets. Always moving, never track-specific. |
| `palette_cycle` | Steps through a fixed palette on beats. | Parties where you want *your* colours, not the spectrum's. |
| `random_walk_hue` | Hue takes small bounded random steps. | Beatless ambient. Organic rather than mechanical. |
| `mirror_mode` | Hue mirrors around a centre as treble/bass shift; brightness breathes. | Slow material with a wide frequency sweep. |
| `breathing_silence` | Slow sine breathing when input is quiet. | A room that should look alive between tracks. |

### Event-driven

| Mode | What it does | Use when |
|---|---|---|
| `kick_snare_split` | Bass → brightness, mid/high → hue accent. | Breakbeats. Separates the two halves of a groove. |
| `silence_flash_recover` | Dims through quiet, one bright flash when audio returns. | Sets with hard stops and drops. |
| `strobe_on_drop` ⚡ | Hard flash on a detected drop. | Peak-time electronic **only**. Flash-heavy. |
| `band_flash_overlay` ⚡ | Per-band flashes over a base colour. | Dense, loud material with no clean band separation. Flash-heavy. |

⚡ = flash-heavy; subject to the 3 flashes/second cap.

---

## The 24 presets

A preset is just a saved set of arguments — mode, sensitivity, dwell, band
count, hue and beat sensitivity. Applying one is identical to entering those
values by hand, so anything a preset does you can reproduce and adjust.

### Party

| Preset | Mode | Tempo | Dwell |
|---|---|---|---|
| `Metal / Hardcore` ⚡ | `strobe_on_drop` | 160-200 BPM | 40 ms |
| `Drum & Bass` | `kick_snare_split` | 165-180 BPM | 40 ms |
| `EDM / Party` | `palette_cycle` | 140-150 BPM | 45 ms |
| `Techno / Dark` ⚡ | `strobe_on_drop` | 128-150 BPM | 45 ms |
| `Punk / Garage` ⚡ | `band_flash_overlay` | 150-200 BPM | 50 ms |
| `House / Four-on-the-Floor` | `bass_only_pulse` | 118-130 BPM | 55 ms |
| `Party / Mixed Playlist` | `auto_rotate_hue` | 100-140 BPM | 60 ms |
| `Hip-Hop / Bass-Heavy` | `bass_only_pulse` | 85-115 BPM | 70 ms |
| `Funk / Disco` | `palette_cycle` | 105-125 BPM | 70 ms |

### General listening

| Preset | Mode | Tempo | Dwell |
|---|---|---|---|
| `Party / Mixed Playlist` | `auto_rotate_hue` | 100-140 BPM | 60 ms |
| `Hip-Hop / Bass-Heavy` | `bass_only_pulse` | 85-115 BPM | 70 ms |
| `Funk / Disco` | `palette_cycle` | 105-125 BPM | 70 ms |
| `Gaming` | `harmonic_pairs` | n/a | 70 ms |
| `Rock / Live` | `band_fixed` | 110-140 BPM | 90 ms |
| `Pop / Radio` | `weighted_blend` | 100-130 BPM | 90 ms |
| `Indie / Alternative` | `spectrum_gradient` | 100-140 BPM | 110 ms |
| `Jazz / Improv` | `dominant_band` | 100-160 BPM | 140 ms |
| `Country / Folk` | `vu_meter` | 80-120 BPM | 180 ms |
| `Classical / Acoustic` | `vu_meter` | any | 200 ms |
| `Podcast / Voice` | `monochrome_pulse` | n/a | 300 ms |

### Slow and background

| Preset | Mode | Tempo | Dwell |
|---|---|---|---|
| `R&B / Soul` | `energy_contour` | 60-95 BPM | 140 ms |
| `Reggae / Dub` | `bass_only_pulse` | 60-90 BPM | 160 ms |
| `Country / Folk` | `vu_meter` | 80-120 BPM | 180 ms |
| `Classical / Acoustic` | `vu_meter` | any | 200 ms |
| `Orchestral / Film Score` | `crescendo_ramp` | any | 220 ms |
| `Chill / Ambient` | `breathing_silence` | 60-90 BPM | 250 ms |
| `Lo-fi / Study` | `breathing_silence` | 70-90 BPM | 300 ms |
| `Movie Night` | `energy_contour` | n/a | 350 ms |
| `Ambient / Drone` | `random_walk_hue` | no beat | 400 ms |

The dashboard also **suggests a preset from the detected BPM** — a starting
point drawn from tempo alone, not a claim about genre. Real genres overlap
heavily in BPM, so treat it as a nudge.

---

## Tuning by symptom

| Symptom | Try |
|---|---|
| Colours change too fast, feels frantic | Raise `min_dwell_ms` (try 150–250) |
| Feels sluggish, misses the beat | Lower `min_dwell_ms` toward 40–60 |
| Barely reacts on quiet tracks | Raise `sensitivity` toward 1.5–2.0 |
| Everything saturates, always bright | Lower `sensitivity` toward 0.6–0.8 |
| Beat detection wanders on dense mixes | Set beat sensitivity to `aggressive` |
| False beats in quiet passages | Set beat sensitivity to `subtle` |
| Too intense / uncomfortable | Enable disable-flash-heavy, raise dwell |

If the bulb lags behind the music generally, check per-bulb latency on the
**Diagnostics** tab. A round trip well above ~200 ms is a network problem, not
a tuning one — no dwell setting fixes a slow link.

---

## Multi-bulb

With more than one bulb, a **role mode** decides how they relate:

- **`unison`** — all identical. Reads as one large light.
- **`phase_offset`** — hue offset per bulb, producing a chase.
- **`band_split`** — each bulb owns a frequency band. The most informative
  arrangement: you can *see* the bass and treble separately.
- **`wave`** — hue sweeps across the group in sequence.
- **`mirror`** — bulbs mirror around a centre hue.

`band_split` needs bulbs positioned so the split reads spatially; scattered
around a room it just looks like they disagree.

---

## Related

- `docs/music-reactive-lighting.md` — design history and the latency/dwell rationale
- `docs/observability.md` — where per-bulb latency and beat data are surfaced
- `SECURITY.md` — the photosensitive-safety cap and its standards basis
