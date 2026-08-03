# Audio latency: modes, settings and the honest budget

**Audio-reactive lighting does not work in the Docker container on its own.**
This is the single most surprising fact about how this dashboard is deployed,
so it comes first. A Linux container on Docker Desktop for Windows has no
access to the host's audio devices. Start a session there and it will run,
report itself running, and never react to a sound — no error, just a bulb that
sits still.

Two things fix that, and you pick between them. Everything else in this
document is detail underneath that choice.

---

## 1. Which mode do I run?

|  | **Bridge mode** | **Native mode** |
|---|---|---|
| Dashboard | keeps serving from the container | container stopped, host serves |
| Added capture latency | ~1–2ms loopback hop | zero |
| Audio devices visible | on the host, streamed in | directly |
| Start it | `tools\start-audio-bridge.cmd` | `tools\native-audio-mode.cmd` |
| **Pick it for** | everyday use, remote use, leaving it running | judging presets by ear |

**Bridge mode** is the default and what you want almost always. A small Windows
capture tool grabs audio via WASAPI and streams raw PCM into the container over
loopback TCP on port 8503. The dashboard never goes down.

**Native mode** stops the container and runs the backend directly on Windows
with direct WASAPI access. It is one command each way and it serves the *same*
`127.0.0.1:8502`, so your bookmark and the tailnet URL both keep working:

```bat
tools\native-audio-mode.cmd            :: switch to native, Ctrl-C to end
tools\native-audio-mode.cmd status     :: which mode is serving right now
tools\native-audio-mode.cmd off        :: restore the container after a hard kill
```

The container is restored on every exit path — Ctrl-C, a crash, a hard kill.
If something unrelated is holding the port it refuses *before* stopping the
container, and names the process.

Use native mode when your judgement is the instrument: tuning the genre
presets, deciding whether a mode "feels" right against real music. Use bridge
mode for everything else.

---

## 2. Why isn't it instant?

Because a lamp on Wi-Fi is slower than every piece of software in this project
put together. Read this before you touch a single setting.

### The budget, stage by stage

Measured on this project's real hardware (`Bytech A19` at `192.168.0.134`) over
a 12-second session on real audio, 2058 frames:

| Stage | Typical | p95 | Worst seen | Who controls it |
|---|---|---|---|---|
| **Capture** | 5.8ms | 27.4ms | 44.9ms | you (hop size) |
| **Analysis** | 0.6ms | 0.9ms | 5.8ms | you (window size) |
| **Software total** | **6.4ms** | 28.3ms | — | **you** |
| **Bulb round-trip** | **11.2ms** | **63.6ms** | **130.7ms** | **nobody** |

The software budget is **6.4ms**, inside the 10ms target. The bulb is **11.2ms
typical and 63.6ms at p95** — and in an earlier run on the same bulb the same
hour it measured **51.9ms typical, 163.6ms at p95, 637ms at worst**.

Read that again, because it is the whole point: **the bulb's own round-trip
varies by more than an order of magnitude, and at its worst it is a hundred
times the entire software budget.** No setting in this document changes it. It
is Wi-Fi, it is Tuya's protocol, it is the lamp's firmware. If the lights feel
laggy and you have already checked the Latency panel and seen the software
stages sitting at single-digit milliseconds, the answer is not in here — the
answer is the bulb, and the only fixes are a better AP, a closer AP, or a
different lamp.

`DEFAULT_MIN_DWELL_MS = 90` exists because of this. There is no point deciding
a new colour every 6ms when the bulb needs ~50–150ms to show you one.

### Where to see it yourself

The **Audio → Latency** card reports all three stages live, per session, with
typical/p95/worst and a count of late and dropped frames. It is measured, not
estimated. If a number in this document disagrees with that card, believe the
card.

---

## 3. The settings, and what each one costs

### Hop size — sets latency

How often analysis runs. **Default 256 samples (5.8ms).**

- **Shorter** → lower latency, more CPU. Analysis runs more often, and each run
  costs the same.
- **Longer** → less CPU, higher latency.

Floor is 64 samples. Going below ~128 is buying latency the bulb will
immediately squander; measured analysis cost is 0.17ms per hop, so a 256-sample
hop runs at about **3% of one core**.

### Window size — sets frequency resolution

How much audio each analysis run sees. **Default 1024 samples (23.2ms, 43Hz per
bin).** Must be a whole number of hops.

This one is not a free dial, and the default is a measured compromise rather
than a guess. A longer window resolves bass more finely — but it also *smears
the transient it is trying to locate*, because a kick enters the window
gradually over more hops and the onset peak flattens. Measured against the
dense-mix tempo fixture, tracking six known tempos:

| Window | Bass resolution | Tempos tracked |
|---|---|---|
| 1024 | 43Hz | **6 / 6** |
| 2048 | 21.5Hz | 5 / 6 (174 BPM read as 178.2) |
| 4096 | 10.8Hz | 3 / 6, every estimate biased high |

So: **do not raise the window to "improve" bass.** It measurably costs beat
accuracy. Frequency resolution and time resolution trade against each other and
beat detection needs the time side.

Note that `FFT_SIZE = 4096` already zero-pads every window. Zero-padding
interpolates bins; it does not add real resolution. Real resolution comes from
window *length* — which is exactly why raising it has a real cost.

### Minimum dwell — how long each colour stays

**Default 90ms.** How often the bulb is actually commanded, independent of how
often a colour is decided. Matched to the bulb's measured round-trip. Lower it
and you queue commands the bulb cannot service; the sender only ever sends the
freshest one, so the effect is wasted work, not faster light.

### Sensitivity, noise gate, AGC

Covered in [audio-modes](audio-modes.md). They change *what* colour comes out,
not *when* — none of them are latency settings.

---

## 4. It's misbehaving — what do I change?

| Symptom | Most likely cause | What to change |
|---|---|---|
| **Nothing happens at all, no error** | session running in the container with no bridge | start the bridge, or switch to native mode |
| Bridge chip says **"waiting"** | backend is listening, no capture tool connected | run `tools\start-audio-bridge.cmd` |
| Bridge chip says **"silent"** | bridge connected, no sound on the selected device | play something; run `--probe` to find the device that actually has audio |
| Bridge chip says **"off"** | backend started with the listener disabled | restart with `SBD_AUDIO_BRIDGE` unset or `1` |
| **Lags behind the music by a fixed amount** | the bulb, almost certainly | check the Latency card — if software stages are single-digit ms, this is the hardware floor |
| **Stutters / jumps** | late or dropped frames | check the Latency card's frame counts; raise the hop, or close whatever is loading the CPU |
| **Dropped frames climbing** | capture underrun | raise hop size; on the bridge, check the host tool isn't competing with a game |
| **Reacts to the wrong thing** (vocals, not the kick) | mode or band choice, not latency | see [audio-modes](audio-modes.md); try a bass-led mode |
| **Beat detection misses or double-fires** | window too long | lower window size back to 1024 |
| **Tempo reads high** | window too long — see the table above | set window to 1024 |
| **Drops out after a while** | silence timeout (5 min) or the bulb fell off Wi-Fi | check the bulb is reachable; the bulb on this project drops off periodically |
| **Colours change but the lamp doesn't** | dwell shorter than the bulb can service | raise `min_dwell_ms` toward 90ms+ |
| **Everything is fine but CPU is high** | hop too short | raise hop size; 256 is ~3% of a core, 64 is ~12% |

---

## 5. Setting up the audio source on Windows

Capturing "whatever is playing on the speakers" needs a loopback source. Two
starting points:

### You already have Voicemeeter or VB-Cable

You are done — you have loopback devices already. Run:

```bat
tools\start-audio-bridge.cmd --probe
```

It plays nothing and simply reports which device actually has signal on it, so
you do not have to guess between the fourteen entries that look identical.
Then:

```bat
tools\start-audio-bridge.cmd --device <index>
```

On this development machine the working device is **85** (`CABLE Output
(VB-Audio Virtual Cable)`, WASAPI), which is why that is the built-in default.

### You have no virtual audio software

Two options:

1. **Use a microphone.** Nothing to install — pick any real input with
   `--list`. It reacts to the room rather than to the file, which for a party
   is arguably better and for preset tuning is worse.
2. **Install VB-Cable** (free) and set it as your default playback device, or
   route just your music player to it. Then capture `CABLE Output`. This is the
   clean path if you want the dashboard to react to exactly what is playing and
   nothing else.

WASAPI loopback can also capture an output device directly on some systems; the
bridge prefers WASAPI for exactly this reason.

### A trap worth knowing

**The bridge resamples, and that is not optional.** WASAPI shared mode refuses
any sample rate other than the device's own. Ask for 44100 on a 48000Hz device
and you get `Invalid sample rate [PaErrorCode -9997]`. The bridge captures at
the device's native rate and resamples to 44100 on the host before sending.
Verified end to end at 48000→44100 with thousands of frames and zero drops.

Device pickers are also deceptive: PortAudio enumerates every physical device
once per Windows host API, so this machine lists **72 entries for roughly 14
real devices**. The dashboard collapses those duplicates and prefers WASAPI,
because picking the wrong copy silently selects a slower backend.

---

## 6. What "late" and "dropped" mean on the Latency card

- **Late** — a block arrived more than twice its nominal period after the
  previous one. Some lateness is normal and not a fault: audio backends
  commonly deliver in bursts (several blocks back-to-back, then a pause), which
  is why the card also shows the raw delivery gaps separately. On this machine
  DirectSound runs ~18% "late" by that definition while sounding perfect.
- **Dropped** — samples that existed and never reached analysis: a PortAudio
  input overflow, or a frame the bridge dropped from a full queue. **Dropped is
  the number that matters.** A climbing dropped count is a real problem; a high
  late count with zero drops usually is not.

The card reports capture latency as the *floor plus lateness*, never as the
median gap between callbacks — on a bursty backend the median gap is near zero
while the audio is no fresher, and reporting it would flatter the pipeline into
claiming a target it does not meet.

---

## See also

- [audio-modes](audio-modes.md) — all 20 modes, presets, and tuning by symptom
- [music-reactive-lighting](music-reactive-lighting.md) — how the analysis works
- [deployment](deployment.md) — running the container, TLS, reverse proxy
