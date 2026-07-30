import collections
import math
import threading
import time

import numpy as np
import sounddevice as sd

from scenes_presets import PRESET_COLORS

SAMPLE_RATE = 44100
# Capture block size controls DECISION latency (how fast we notice a change),
# not the bulb's own send rate (that's governed by DWELL_MS on the sender —
# see BulbSender below). 512 samples @ 44100Hz ~= 11.6ms — a big drop from
# the original 1024 (~23ms), verified in iteration 003 to still give enough
# real samples for band-energy estimates once zero-padded (see FFT_SIZE).
BLOCK_SIZE = 512
FFT_SIZE = 4096  # zero-pad the window before rfft for finer band resolution
                 # without adding latency (padding is free — it's the block
                 # size above that actually bounds how fresh the data is).

DEFAULT_MIN_DWELL_MS = 90  # matches this bulb's measured ~50-100ms round trip
MIN_DWELL_FLOOR_MS = 40    # refuse anything sillier than this — see docs
SILENCE_TIMEOUT_S = 300

FAILOVER_THRESHOLD = 3  # consecutive send failures before a bulb is reported
                        # "offline" in group status -- see BulbSender.status()
                        # and GroupAudioSession's per-bulb status. Crossing
                        # this never stops or restarts the group session;
                        # every other bulb's own independent BulbSender loop
                        # keeps sending on schedule regardless.

# Fixed hue anchors used by the 3-band modes.
BASS_HUE = 10.0
MID_HUE = 130.0
TREBLE_HUE = 230.0
LEFT_HUE = 200.0   # stereo_split anchors
RIGHT_HUE = 20.0
HARMONIC_HUE_A = 45.0    # harmonic_pairs anchors — 180 deg apart by construction
HARMONIC_HUE_B = (HARMONIC_HUE_A + 180.0) % 360
KICK_SNARE_BASE_HUE = 15.0  # kick_snare_split's resting hue before any snare accent

MODES = [
    "band_fixed", "dominant_band", "weighted_blend", "vu_meter",
    "auto_rotate_hue", "monochrome_pulse", "strobe_on_drop", "palette_cycle",
    "spectrum_gradient", "band_flash_overlay", "stereo_split", "breathing_silence",
    "harmonic_pairs", "kick_snare_split",
]

ROLE_MODES = ["unison", "phase_offset", "band_split", "wave", "mirror"]


def list_input_devices():
    devices = sd.query_devices()
    result = []
    for i, d in enumerate(devices):
        if d.get("max_input_channels", 0) > 0:
            result.append({
                "index": i,
                "name": d.get("name"),
                "max_input_channels": d.get("max_input_channels"),
                "default_samplerate": d.get("default_samplerate"),
            })
    return result


def _band_energy(spectrum, freqs, lo, hi):
    mask = (freqs >= lo) & (freqs < hi)
    return float(spectrum[mask].sum())


def _smooth_hue(current_deg, target_deg, alpha):
    """Circular-mean exponential smoothing. A plain linear blend
    (`old*(1-a) + target*a`) is wrong right at the 0/360 wrap boundary —
    e.g. blending 358 and 3 should land near 0/360 (they're 5 degrees
    apart), not near 180 (a naive average of the raw numbers). Found while
    reviewing `stereo_split`, whose hue target legitimately crosses that
    boundary; applied everywhere for consistency since any anchor
    arrangement could hit it."""
    cur = math.radians(current_deg)
    tgt = math.radians(target_deg)
    x = math.cos(cur) * (1 - alpha) + math.cos(tgt) * alpha
    y = math.sin(cur) * (1 - alpha) + math.sin(tgt) * alpha
    return math.degrees(math.atan2(y, x)) % 360


def _wave_brightness_scale(tick, index, n, period_ticks=40, floor=0.15):
    """Pure function backing the "wave" role mode: a brightness multiplier
    in [floor, 1.0] for bulb `index` (of `n`, in the order they were
    configured) at a given `tick`.

    `tick` is a per-GroupAudioSession frame counter (incremented once per
    processed audio callback), NOT wall-clock time -- deterministic and
    exactly reproducible in tests, unlike anything driven by time.time().
    One full traveling-wave crest sweeps end-to-end across the ordered
    bulb list every `period_ticks` frames; every bulb gets an identical
    base color (from `_apply_mode`) and only its brightness is scaled by
    where the crest currently is relative to that bulb's position.
    """
    if n <= 1:
        return 1.0
    position = (tick % period_ticks) / period_ticks * n  # crest position, 0..n
    dist = abs(index - position)
    width = max(0.75, n / 3.0)
    bump = math.exp(-(dist ** 2) / (2 * width ** 2))  # gaussian bump centered on the crest
    return floor + (1 - floor) * bump


def _mirror_hue(hue_deg, index, n, center_hue=0.0):
    """Pure function backing the "mirror" role mode: bulbs are paired
    front-to-back in the ordered list -- (0, n-1), (1, n-2), ... -- and the
    second bulb of each pair reflects the first bulb's hue around
    `center_hue` (mirrored = 2*center - hue, wrapped to 0..360). The
    "leader" of each pair (the lower index) always keeps the unmodified
    hue; an unpaired middle bulb in an odd-length list (index == partner)
    also keeps it unmodified, since it has no partner to mirror against.
    """
    partner = n - 1 - index
    if index <= partner:
        return hue_deg % 360  # leader (lower index of the pair), or the unpaired middle bulb
    return (2 * center_hue - hue_deg) % 360  # follower mirrors around center


def log_band_edges(n_bands, lo=20.0, hi=20000.0):
    """N+1 log-spaced edges from 20Hz-20kHz — log spacing gives bass more
    of the available bands than a linear split would, which matches how
    music actually distributes energy (and how ears perceive pitch)."""
    return list(np.logspace(math.log10(lo), math.log10(hi), n_bands + 1))


def analyze_frame(samples, sample_rate=SAMPLE_RATE, n_bands=None, band_edges=None):
    """Pure analysis step, shared by every mode and by both single-bulb and
    group (orchestrated) sessions. Returns energies (raw + normalized
    fractions for each band), rms, and the frequency arrays in case a mode
    wants the full spectrum (e.g. spectral centroid)."""
    windowed = samples * np.hanning(len(samples))
    spectrum = np.abs(np.fft.rfft(windowed, n=FFT_SIZE))
    freqs = np.fft.rfftfreq(FFT_SIZE, 1.0 / sample_rate)
    rms = float(np.sqrt(np.mean(np.square(samples))) + 1e-12)

    if band_edges is None:
        band_edges = log_band_edges(n_bands or 3) if n_bands else [20, 250, 4000, 20000]
    energies = [
        _band_energy(spectrum, freqs, band_edges[i], band_edges[i + 1])
        for i in range(len(band_edges) - 1)
    ]
    total = sum(energies) + 1e-9
    fractions = [e / total for e in energies]
    return {
        "spectrum": spectrum, "freqs": freqs, "rms": rms,
        "energies": energies, "fractions": fractions, "band_edges": band_edges,
    }


class BulbSender:
    """One per bulb. Owns every network call to that bulb's controller so
    the audio callback thread never touches the network directly — a real
    bug (found in iteration 002) showed that calling tinytuya's blocking
    set_* methods inline in the sounddevice callback freezes the entire
    capture pipeline whenever a bulb is slow/offline.

    Also owns dwell pacing: the audio side can compute a new target as fast
    as every callback (sub-15ms with BLOCK_SIZE=512), but this only ever
    sends the freshest target, at most once per `min_dwell_ms` — decoupling
    "how fast do we decide" from "how fast does the light actually change
    and how long does each color stay visible" per the two competing asks
    (near-instant reaction vs. actually being able to see it)."""

    def __init__(self, controller, min_dwell_ms=DEFAULT_MIN_DWELL_MS):
        self.controller = controller
        self.min_dwell_ms = max(MIN_DWELL_FLOOR_MS, min_dwell_ms)
        self._pending = None
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._last_sent_at = 0.0
        self._last_latency_ms = None
        self._error = None
        self._consecutive_failures = 0
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def queue(self, action):
        """Called from the audio callback. Must never block."""
        with self._lock:
            self._pending = action
        self._ready.set()

    def stop(self):
        self._stop.set()
        self._ready.set()
        if self._thread.is_alive():
            self._thread.join(timeout=3)

    def status(self):
        return {
            "last_latency_ms": self._last_latency_ms, "error": self._error,
            "min_dwell_ms": self.min_dwell_ms,
            "consecutive_failures": self._consecutive_failures,
            # Failover visibility only -- crossing the threshold never stops
            # this sender's loop or the group session; it keeps retrying on
            # every future queued action exactly as before, so the bulb
            # rejoins automatically the moment it's reachable again.
            "offline": self._consecutive_failures >= FAILOVER_THRESHOLD,
        }

    def _loop(self):
        while not self._stop.is_set():
            if not self._ready.wait(timeout=0.5):
                continue
            since_last = (time.time() - self._last_sent_at) * 1000
            wait_more = self.min_dwell_ms - since_last
            if wait_more > 0:
                if self._stop.wait(wait_more / 1000):
                    return
            with self._lock:
                action = self._pending
                self._pending = None
            self._ready.clear()
            if action is None or self._stop.is_set():
                continue
            t0 = time.time()
            try:
                kind = action[0]
                if kind == "hsv":
                    self.controller.set_hsv(action[1], action[2], action[3])
                elif kind == "rgb_brightness":
                    self.controller.set_rgb(action[1], action[2], action[3])
                    self.controller.set_brightness(action[4])
                self._error = None
                self._consecutive_failures = 0
            except Exception as e:
                self._error = str(e)
                self._consecutive_failures += 1
            self._last_latency_ms = round((time.time() - t0) * 1000, 1)
            self._last_sent_at = time.time()


def _apply_mode(mode, bands, ctx):
    """Computes the target bulb action for one mode given this frame's band
    data (`bands`, from analyze_frame) and a mutable per-session `ctx` dict
    holding smoothing state (smoothed_hue, rotate_hue, palette_idx, etc).
    Shared by AudioSession and GroupAudioSession so every mode works
    identically solo or orchestrated across bulbs."""
    rms = bands["rms"]
    fractions = bands["fractions"]
    gain = ctx["sensitivity"]
    now = time.time()

    rolling = ctx["rolling_bass"]
    bass_like = bands["energies"][0]
    rolling.append(bass_like)
    avg = sum(rolling) / len(rolling)
    is_beat = len(rolling) > 8 and bass_like > avg * 1.5
    is_hard_hit = len(rolling) > 8 and bass_like > avg * 2.2
    ctx["latest_bands"] = {
        "fractions": [round(f, 3) for f in fractions], "rms": round(rms, 5), "is_beat": is_beat,
    }

    base_brightness = min(100, max(4, 4 + rms * gain * 4500))
    pulse_brightness = min(100, base_brightness + (20 if is_beat else 0))

    if mode == "vu_meter":
        return ("hsv", ctx["monochrome_hue"], 100, base_brightness)

    if mode == "monochrome_pulse":
        sat = max(40, min(100, 100 - (1 - min(1, rms * gain * 20)) * 60))
        return ("hsv", ctx["monochrome_hue"], sat, pulse_brightness)

    if mode == "auto_rotate_hue":
        ctx["rotate_hue"] = (ctx["rotate_hue"] + 1.2) % 360
        return ("hsv", ctx["rotate_hue"], 100, pulse_brightness)

    if mode == "band_fixed":
        f = fractions if len(fractions) == 3 else _resample_to_3(bands)
        target = f[0] * BASS_HUE + f[1] * MID_HUE + f[2] * TREBLE_HUE
        ctx["smoothed_hue"] = _smooth_hue(ctx["smoothed_hue"], target, 0.4)
        return ("hsv", ctx["smoothed_hue"], 100, pulse_brightness)

    if mode == "dominant_band":
        f = fractions if len(fractions) == 3 else _resample_to_3(bands)
        dominant = max((f[0], BASS_HUE), (f[1], MID_HUE), (f[2], TREBLE_HUE), key=lambda x: x[0])
        ctx["smoothed_hue"] = _smooth_hue(ctx["smoothed_hue"], dominant[1], 0.2)
        return ("hsv", ctx["smoothed_hue"], 100, pulse_brightness)

    if mode == "weighted_blend":
        spectrum, freqs = bands["spectrum"], bands["freqs"]
        centroid = float((freqs * spectrum).sum() / spectrum.sum()) if spectrum.sum() > 0 else 0.0
        target = min(1.0, centroid / 6000.0) * 280
        ctx["smoothed_hue"] = _smooth_hue(ctx["smoothed_hue"], target, 0.3)
        return ("hsv", ctx["smoothed_hue"], 100, pulse_brightness)

    if mode == "strobe_on_drop":
        f = fractions if len(fractions) == 3 else _resample_to_3(bands)
        if is_hard_hit and (now - ctx["last_flash_at"]) > 0.15:
            ctx["last_flash_at"] = now
            return ("hsv", 0, 0, 100)
        return ("hsv", BASS_HUE, 100, max(8, base_brightness * 0.4))

    if mode == "palette_cycle":
        if is_beat:
            ctx["palette_idx"] = (ctx["palette_idx"] + 1) % len(PRESET_COLORS)
        r, g, b = PRESET_COLORS[ctx["palette_idx"]]["rgb"]
        return ("rgb_brightness", r, g, b, int(pulse_brightness))

    if mode == "spectrum_gradient":
        # N-band continuous gradient — the finer-grained sibling of
        # band_fixed, spread across as many bands as the session requested.
        n = len(fractions)
        anchors = np.linspace(BASS_HUE, TREBLE_HUE + 60, n)  # slight overshoot past treble into violet
        target = sum(f * a for f, a in zip(fractions, anchors))
        ctx["smoothed_hue"] = _smooth_hue(ctx["smoothed_hue"], target, 0.45)
        return ("hsv", ctx["smoothed_hue"], 100, pulse_brightness)

    if mode == "band_flash_overlay":
        n = len(fractions)
        anchors = np.linspace(BASS_HUE, TREBLE_HUE + 60, n)
        base_hue = sum(f * a for f, a in zip(fractions, anchors))
        per_band_rolling = ctx.setdefault("per_band_rolling", [collections.deque(maxlen=24) for _ in range(n)])
        hit_band = None
        for i, e in enumerate(bands["energies"]):
            per_band_rolling[i].append(e)
            avg_i = sum(per_band_rolling[i]) / len(per_band_rolling[i])
            if len(per_band_rolling[i]) > 8 and e > avg_i * 1.8:
                hit_band = i
                break
        if hit_band is not None:
            return ("hsv", anchors[hit_band], 100, min(100, base_brightness + 35))
        ctx["smoothed_hue"] = _smooth_hue(ctx["smoothed_hue"], base_hue, 0.4)
        return ("hsv", ctx["smoothed_hue"], 90, max(6, base_brightness * 0.5))

    if mode == "stereo_split":
        left_rms = ctx.get("left_rms", rms)
        right_rms = ctx.get("right_rms", rms)
        total_lr = left_rms + right_rms + 1e-9
        balance = (right_rms - left_rms) / total_lr  # -1 (all left) .. +1 (all right)
        span = (RIGHT_HUE - LEFT_HUE) % 360  # degrees from left anchor to right anchor
        target = (LEFT_HUE + (balance + 1) / 2 * span) % 360
        ctx["smoothed_hue"] = _smooth_hue(ctx["smoothed_hue"], target, 0.4)
        combined_brightness = min(100, max(4, 4 + (left_rms + right_rms) * gain * 2250))
        return ("hsv", ctx["smoothed_hue"], 100, combined_brightness)

    if mode == "breathing_silence":
        ctx["rotate_hue"] = (ctx["rotate_hue"] + 0.15) % 360
        period_s = 4.0
        breathing = 12 + 8 * (0.5 + 0.5 * math.sin(2 * math.pi * (now % period_s) / period_s))
        active_boost = max(0.0, min(60.0, rms * gain * 3000))
        brightness = min(100, breathing + active_boost)
        return ("hsv", ctx["rotate_hue"], 70 if active_boost < 5 else 100, brightness)

    if mode == "harmonic_pairs":
        # Find the two most energetic NON-ADJACENT bands (adjacent bands
        # are excluded because they're basically the same part of the
        # spectrum leaking across a band edge — the interesting "pair"
        # signal is two genuinely separate regions lighting up together,
        # e.g. bass + treble both hot while mid stays quiet).
        energies_list = bands["energies"]
        n = len(energies_list)
        best = None
        for i in range(n):
            for j in range(i + 2, n):
                total_e = energies_list[i] + energies_list[j]
                if best is None or total_e > best[0]:
                    best = (total_e, i, j)
        if best is None:
            # Fewer than 3 bands means no strictly non-adjacent pair
            # exists — fall back to the two extreme bands we do have.
            idx_a, idx_b = 0, (n - 1 if n > 1 else 0)
        else:
            _, idx_a, idx_b = best
        e_a, e_b = energies_list[idx_a], energies_list[idx_b]
        # ratio_a in [0, 1]: which of the pair currently dominates. The
        # "+1e-9" is the same divide-by-zero guard used elsewhere in this
        # file (see analyze_frame's `total`) — at true silence e_a == e_b
        # == 0, so ratio_a deterministically resolves to 0 every frame
        # (never an arbitrary/noisy value), which is what keeps this mode
        # from flickering in a quiet room: it always settles on HUE_B.
        #
        # NOTE: target is a plain linear walk from HUE_B to HUE_A along a
        # single fixed direction (not _smooth_hue's shortest-arc circular
        # mean). Because the two anchors are exactly 180 degrees apart,
        # the shortest arc between them is ambiguous (two equal-length
        # paths), and right at a 50/50 split (ratio_a == 0.5) that
        # ambiguity makes the circular mean numerically degenerate — the
        # two anchor vectors cancel to ~(0, 0), so atan2 returns whatever
        # direction floating-point rounding noise happens to favor,
        # flipping unpredictably frame to frame. Walking along one fixed
        # direction sidesteps that entirely: every ratio_a maps to exactly
        # one point on the circle, with no ambiguous case.
        ratio_a = e_a / (e_a + e_b + 1e-9)
        hue_span = (HARMONIC_HUE_A - HARMONIC_HUE_B) % 360  # == 180 by construction
        target = (HARMONIC_HUE_B + ratio_a * hue_span) % 360
        ctx["smoothed_hue"] = _smooth_hue(ctx["smoothed_hue"], target, 0.25)
        return ("hsv", ctx["smoothed_hue"], 100, pulse_brightness)

    if mode == "kick_snare_split":
        # Low band = kick-like pulses driving brightness; a mid/high band
        # = snare/hihat-like accents layered on top as a hue shift around
        # a fixed base hue. Uses `fractions` (already normalized 0..1)
        # rather than raw energies so the hue swing stays well-behaved
        # regardless of overall input loudness/gain.
        n = len(fractions)
        bass_frac = fractions[0] if n else 0.0
        snare_idx = min(n - 1, max(1, n // 2)) if n > 1 else 0
        snare_frac = fractions[snare_idx] if n else 0.0
        # Brightness is still the standard rms*gain pulse (so overall
        # volume still matters) but weighted up when bass fractionally
        # dominates the mix, i.e. an actual kick hit, and down otherwise.
        kick_brightness = min(100, max(4, 4 + rms * gain * 4500 * (0.4 + 1.2 * bass_frac)))
        accent = min(1.0, snare_frac * 3.0)
        target = (KICK_SNARE_BASE_HUE + accent * 90.0) % 360
        # At silence, fractions -> 0 for both bass and snare bands (same
        # near-zero-total behavior as analyze_frame), so `target` settles
        # deterministically on KICK_SNARE_BASE_HUE and brightness floors
        # at 4 — no flicker, matching dominant_band's silence convention.
        ctx["smoothed_hue"] = _smooth_hue(ctx["smoothed_hue"], target, 0.3)
        brightness = min(100, kick_brightness + (15 if is_beat else 0))
        return ("hsv", ctx["smoothed_hue"], 100, brightness)

    return ("hsv", BASS_HUE, 100, pulse_brightness)


def _resample_to_3(bands):
    """Collapses an N-band split back to bass/mid/treble fractions for
    modes written against the original 3-anchor scheme, when a session was
    started with a different band count."""
    n = len(bands["fractions"])
    if n == 3:
        return bands["fractions"]
    third = max(1, n // 3)
    f = bands["fractions"]
    bass = sum(f[:third])
    mid = sum(f[third:2 * third])
    treble = sum(f[2 * third:])
    total = bass + mid + treble + 1e-9
    return [bass / total, mid / total, treble / total]


def _new_ctx(sensitivity, monochrome_hue):
    return {
        "sensitivity": max(0.1, min(5.0, sensitivity)),
        "monochrome_hue": monochrome_hue % 360,
        "smoothed_hue": BASS_HUE,
        "rotate_hue": 0.0,
        "palette_idx": 0,
        "last_flash_at": 0.0,
        "rolling_bass": collections.deque(maxlen=40),
        "latest_bands": {"fractions": [], "rms": 0.0, "is_beat": False},
    }


class AudioSession:
    """Single-bulb audio-reactive session. See `_apply_mode` for what each
    mode actually computes; this class is just capture + lifecycle +
    dispatch via its own `BulbSender`."""

    def __init__(self, controller, device_index, mode="band_fixed", sensitivity=1.0,
                 monochrome_hue=280.0, n_bands=3, min_dwell_ms=DEFAULT_MIN_DWELL_MS):
        self.controller = controller
        self.device_index = device_index
        self.mode = mode if mode in MODES else "band_fixed"
        self.n_bands = max(3, min(16, n_bands))
        self.band_edges = log_band_edges(self.n_bands)
        self.ctx = _new_ctx(sensitivity, monochrome_hue)
        self.sender = BulbSender(controller, min_dwell_ms)

        self._stop = threading.Event()
        self._thread = None
        self._error = None
        self._last_audio_at = time.time()
        self._stereo = (mode == "stereo_split")

    def start(self):
        self.controller.stop_effect()
        self.controller._log("audio_reactive_start", {"mode": self.mode, "device_index": self.device_index})
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        self.sender.stop()
        self.controller._log("audio_reactive_stop", {"mode": self.mode})

    def is_alive(self):
        return self._thread is not None and self._thread.is_alive()

    def status(self):
        return {
            "active": self.is_alive(),
            "mode": self.mode,
            "device_index": self.device_index,
            "sensitivity": self.ctx["sensitivity"],
            "n_bands": self.n_bands,
            "bands": self.ctx["latest_bands"],
            "sender": self.sender.status(),
            "error": self._error,
        }

    def _run(self):
        channels = 2 if self._stereo else 1
        try:
            def callback(indata, frames, time_info, status_flags):
                self._process(indata)

            try:
                stream = sd.InputStream(device=self.device_index, channels=channels, samplerate=SAMPLE_RATE,
                                         blocksize=BLOCK_SIZE, latency="low", callback=callback)
            except Exception:
                if channels == 2:
                    channels = 1  # fall back to mono if the device can't do stereo
                    stream = sd.InputStream(device=self.device_index, channels=channels, samplerate=SAMPLE_RATE,
                                             blocksize=BLOCK_SIZE, latency="low", callback=callback)
                else:
                    raise
            with stream:
                while not self._stop.is_set():
                    if time.time() - self._last_audio_at > SILENCE_TIMEOUT_S:
                        self.controller._log("audio_reactive_timeout", {"mode": self.mode})
                        break
                    self._stop.wait(0.25)
        except Exception as e:
            self._error = str(e)
            self.controller._log("audio_reactive_error", {"mode": self.mode}, ok=False, error=str(e))

    def _process(self, indata):
        now = time.time()
        if self._stereo and indata.shape[1] >= 2:
            left, right = indata[:, 0], indata[:, 1]
            self.ctx["left_rms"] = float(np.sqrt(np.mean(np.square(left))) + 1e-12)
            self.ctx["right_rms"] = float(np.sqrt(np.mean(np.square(right))) + 1e-12)
            samples = (left + right) / 2.0
        else:
            samples = indata[:, 0]

        rms_check = float(np.sqrt(np.mean(np.square(samples))))
        if rms_check > 0.0008:
            self._last_audio_at = now

        n_bands = self.n_bands if self.mode in ("spectrum_gradient", "band_flash_overlay", "harmonic_pairs") else 3
        bands = analyze_frame(samples, band_edges=log_band_edges(n_bands) if n_bands != 3 else None)
        action = _apply_mode(self.mode, bands, self.ctx)
        self.sender.queue(action)


class GroupAudioSession:
    """Orchestrates several bulbs off ONE shared audio capture — analysis
    runs once per callback and every bulb gets its own derived target,
    rather than opening N redundant streams against the same device. Each
    bulb still has its own `BulbSender`, so one slow/offline bulb in the
    group can never stall the others (see FAILOVER_THRESHOLD / BulbSender —
    a bulb that starts failing just gets reported "offline" in status();
    the group thread never restarts and every other bulb's sender keeps
    dispatching on its own independent schedule).

    role_mode:
      - "unison"       — every bulb shows the identical color.
      - "phase_offset" — same effect, hue shifted per bulb. Even spacing
                          (360/n * i) by default, or an explicit
                          `hue_offsets[i]` in degrees when provided.
      - "band_split"   — bulb i is primarily driven by band i of an
                          N-band split (N = number of bulbs), a literal
                          per-bulb "this one is the bass bulb" assignment,
                          overridable per-bulb via `band_assignments[i]`.
      - "wave"         — every bulb shows the identical color, but
                          brightness is scaled by a traveling gaussian
                          "crest" that sweeps across the ordered bulb list
                          over `wave_period_ticks` audio frames.
      - "mirror"       — bulbs paired front-to-back in the ordered list
                          ((0, n-1), (1, n-2), ...); the second bulb of
                          each pair mirrors the first bulb's hue around
                          `mirror_center_hue`. An unpaired middle bulb (odd
                          n) keeps its own hue unmodified.

    `hue_offsets`, `brightness_scales`, and `band_assignments` are optional
    per-bulb override lists, indexed in the same order as `controllers`.
    `brightness_scales` applies as a final multiplier in EVERY role_mode
    (not just phase_offset), so e.g. "this bulb is dimmer than the others,
    scale it up" works regardless of which role_mode is active.
    """

    def __init__(self, controllers, device_index, mode="band_fixed", role_mode="unison",
                 sensitivity=1.0, monochrome_hue=280.0, min_dwell_ms=DEFAULT_MIN_DWELL_MS,
                 hue_offsets=None, brightness_scales=None, band_assignments=None,
                 mirror_center_hue=0.0, wave_period_ticks=40):
        self.controllers = controllers
        self.device_index = device_index
        self.mode = mode if mode in MODES else "band_fixed"
        self.role_mode = role_mode if role_mode in ROLE_MODES else "unison"
        self.senders = [BulbSender(c, min_dwell_ms) for c in controllers]
        self.ctxs = [_new_ctx(sensitivity, monochrome_hue) for _ in controllers]
        self.hue_offsets = hue_offsets
        self.brightness_scales = brightness_scales
        self.band_assignments = band_assignments
        self.mirror_center_hue = mirror_center_hue % 360
        self.wave_period_ticks = max(1, wave_period_ticks)
        self._wave_tick = 0
        self._stop = threading.Event()
        self._thread = None
        self._error = None
        self._last_audio_at = time.time()

    def start(self):
        for c in self.controllers:
            c.stop_effect()
            c._log("audio_reactive_group_start", {"mode": self.mode, "role_mode": self.role_mode})
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        for s in self.senders:
            s.stop()

    def is_alive(self):
        return self._thread is not None and self._thread.is_alive()

    def _role_label(self, i, n):
        """Human-readable description of what bulb `i` is currently doing,
        for the live per-bulb status field -- independent of whether that
        bulb is actually online (see `state` in status(), computed from the
        sender separately)."""
        if self.role_mode == "phase_offset":
            offset = self.hue_offsets[i] if self.hue_offsets and i < len(self.hue_offsets) else (360.0 / n) * i
            return f"phase_offset +{round(offset % 360, 1)} deg"
        if self.role_mode == "band_split":
            assigned = self.band_assignments[i] if self.band_assignments and i < len(self.band_assignments) else None
            assigned = i if assigned is None else assigned
            return f"band_split band {assigned}"
        if self.role_mode == "wave":
            return f"wave position {i}/{n}"
        if self.role_mode == "mirror":
            partner = n - 1 - i
            if i == partner:
                return "mirror center (unpaired)"
            return "mirror leader" if i < partner else f"mirror follower of {partner}"
        return "unison"

    def status(self):
        n = len(self.controllers)
        bulbs = []
        for i, (ctx, s) in enumerate(zip(self.ctxs, self.senders)):
            sender_status = s.status()
            if sender_status["offline"]:
                state = "offline"
            elif sender_status["error"]:
                state = "error"
            else:
                state = "active"
            bulbs.append({
                "index": i,
                "role": self._role_label(i, n),
                "bands": ctx["latest_bands"],
                "sender": sender_status,
                "state": state,
            })
        return {
            "active": self.is_alive(),
            "mode": self.mode,
            "role_mode": self.role_mode,
            "bulb_count": n,
            "bulbs": bulbs,
            "error": self._error,
        }

    def _run(self):
        try:
            def callback(indata, frames, time_info, status_flags):
                self._process(indata[:, 0])

            with sd.InputStream(device=self.device_index, channels=1, samplerate=SAMPLE_RATE,
                                 blocksize=BLOCK_SIZE, latency="low", callback=callback):
                while not self._stop.is_set():
                    if time.time() - self._last_audio_at > SILENCE_TIMEOUT_S:
                        break
                    self._stop.wait(0.25)
        except Exception as e:
            self._error = str(e)

    def _apply_brightness_scale(self, action, i):
        """Final per-bulb brightness multiplier, applied on top of
        whatever the role_mode already computed. Independent of role_mode
        so it composes with all of them (unison/phase_offset/band_split/
        wave/mirror alike)."""
        if not self.brightness_scales or i >= len(self.brightness_scales) or self.brightness_scales[i] is None:
            return action
        scale = max(0.0, self.brightness_scales[i])
        if action[0] == "hsv":
            return ("hsv", action[1], action[2], min(100, action[3] * scale))
        if action[0] == "rgb_brightness":
            return ("rgb_brightness", action[1], action[2], action[3], min(100, action[4] * scale))
        return action

    def _process(self, samples):
        now = time.time()
        n = len(self.controllers)
        rms_check = float(np.sqrt(np.mean(np.square(samples))))
        if rms_check > 0.0008:
            self._last_audio_at = now

        if self.role_mode == "band_split":
            bands = analyze_frame(samples, band_edges=log_band_edges(max(3, n)))
            n_split_bands = len(bands["fractions"])
            for i in range(n):
                # Each bulb sees a band-split view rotated so its assigned
                # band comes first — reuses the same mode logic, just with
                # each bulb's fractions rolled to foreground its band.
                # Default assignment is bulb i <-> band i; `band_assignments`
                # lets a specific bulb be pinned to any band regardless of
                # its position in the bulb list (e.g. bulb 0 pinned to the
                # treble band instead of bass).
                assigned = None
                if self.band_assignments and i < len(self.band_assignments):
                    assigned = self.band_assignments[i]
                assigned = i if assigned is None else (assigned % n_split_bands)
                rolled = {**bands, "fractions": bands["fractions"][assigned:] + bands["fractions"][:assigned],
                          "energies": bands["energies"][assigned:] + bands["energies"][:assigned]}
                action = _apply_mode(self.mode, rolled, self.ctxs[i])
                action = self._apply_brightness_scale(action, i)
                self.senders[i].queue(action)
            return

        bands = analyze_frame(samples)
        for i in range(n):
            action = _apply_mode(self.mode, bands, self.ctxs[i])
            if self.role_mode == "phase_offset" and action[0] == "hsv":
                if self.hue_offsets and i < len(self.hue_offsets) and self.hue_offsets[i] is not None:
                    offset = self.hue_offsets[i]
                else:
                    offset = (360.0 / n) * i
                action = ("hsv", (action[1] + offset) % 360, action[2], action[3])
            elif self.role_mode == "wave" and action[0] == "hsv":
                scale = _wave_brightness_scale(self._wave_tick, i, n, self.wave_period_ticks)
                action = ("hsv", action[1], action[2], min(100, action[3] * scale))
            elif self.role_mode == "mirror" and action[0] == "hsv":
                mirrored = _mirror_hue(action[1], i, n, self.mirror_center_hue)
                action = ("hsv", mirrored, action[2], action[3])
            action = self._apply_brightness_scale(action, i)
            self.senders[i].queue(action)

        self._wave_tick += 1


# ------------------------------------------------------------ sessions ---
_sessions = {}          # single-bulb sessions, keyed by device id
_group_sessions = {}    # group sessions, keyed by group id
_sessions_lock = threading.Lock()


def start_session(controller, device_index, mode="band_fixed", sensitivity=1.0,
                   monochrome_hue=280.0, n_bands=3, min_dwell_ms=DEFAULT_MIN_DWELL_MS):
    with _sessions_lock:
        existing = _sessions.get(controller.cfg["id"])
        if existing and existing.is_alive():
            existing.stop()
        session = AudioSession(controller, device_index, mode, sensitivity, monochrome_hue, n_bands, min_dwell_ms)
        _sessions[controller.cfg["id"]] = session
        session.start()
        return session


def stop_session(device_id):
    with _sessions_lock:
        session = _sessions.pop(device_id, None)
    if session:
        session.stop()
        return True
    return False


def get_session_status(device_id):
    with _sessions_lock:
        session = _sessions.get(device_id)
    if not session:
        return {"active": False}
    return session.status()


def start_group_session(group_id, controllers, device_index, mode="band_fixed", role_mode="unison",
                         sensitivity=1.0, monochrome_hue=280.0, min_dwell_ms=DEFAULT_MIN_DWELL_MS,
                         hue_offsets=None, brightness_scales=None, band_assignments=None,
                         mirror_center_hue=0.0, wave_period_ticks=40):
    with _sessions_lock:
        existing = _group_sessions.get(group_id)
        if existing and existing.is_alive():
            existing.stop()
        session = GroupAudioSession(controllers, device_index, mode, role_mode, sensitivity, monochrome_hue,
                                     min_dwell_ms, hue_offsets, brightness_scales, band_assignments,
                                     mirror_center_hue, wave_period_ticks)
        _group_sessions[group_id] = session
        session.start()
        return session


def stop_group_session(group_id):
    with _sessions_lock:
        session = _group_sessions.pop(group_id, None)
    if session:
        session.stop()
        return True
    return False


def get_group_session_status(group_id):
    with _sessions_lock:
        session = _group_sessions.get(group_id)
    if not session:
        return {"active": False}
    return session.status()


# ------------------------------------------------------- input device health
def validate_device_index(device_index):
    """Returns (ok, error_message). Meant to be called BEFORE starting a
    session so a saved `device_index` that no longer matches any connected
    device (the classic "reboot re-numbered my audio devices" failure)
    fails fast with one clear, actionable message instead of silently
    starting a session whose capture thread can never produce audio and
    whose only symptom is "the bulb just... doesn't react"."""
    try:
        devices = sd.query_devices()
    except Exception as e:
        return False, f"could not query audio devices: {e}"
    if device_index < 0 or device_index >= len(devices):
        return False, (
            f"input device index {device_index} not found (only {len(devices)} "
            f"audio devices currently connected) -- it may have been "
            f"unplugged or renumbered since this was configured; re-select "
            f"an input device from /api/audio/devices"
        )
    if devices[device_index].get("max_input_channels", 0) < 1:
        name = devices[device_index].get("name")
        return False, f"device index {device_index} ('{name}') has no input channels"
    return True, None


def device_health_check(device_index, duration_s=0.3):
    """Open a short-lived capture stream against `device_index`, grab a
    small burst of real samples, and report whether it actually produced
    audio -- without starting a full audio-reactive session. Lets the UI
    validate a device selection (and troubleshoot "no reaction" complaints)
    before committing to `start_session`/`start_group_session`."""
    ok, err = validate_device_index(device_index)
    if not ok:
        return {"ok": False, "error": err, "device_index": device_index}

    info = sd.query_devices(device_index)
    max_channels = info.get("max_input_channels", 0)
    channels = min(2, max_channels) or 1

    captured = {"frames": 0, "peak": 0.0}

    def callback(indata, frames, time_info, status_flags):
        captured["frames"] += frames
        if frames:
            peak = float(np.abs(indata).max())
            if peak > captured["peak"]:
                captured["peak"] = peak

    try:
        with sd.InputStream(device=device_index, channels=channels, samplerate=SAMPLE_RATE,
                             blocksize=BLOCK_SIZE, callback=callback):
            time.sleep(duration_s)
    except Exception as e:
        return {"ok": False, "error": str(e), "device_index": device_index, "channels_tested": channels}

    return {
        "ok": captured["frames"] > 0,
        "device_index": device_index,
        "name": info.get("name"),
        "channels_tested": channels,
        "frames_captured": captured["frames"],
        "peak_amplitude": round(captured["peak"], 5),
        "silent": captured["frames"] > 0 and captured["peak"] < 0.0005,
    }
