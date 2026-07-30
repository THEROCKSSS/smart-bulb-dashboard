import collections
import math
import threading
import time

import numpy as np
import sounddevice as sd

import audio_signal
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

ROLE_MODES = ["unison", "phase_offset", "band_split"]


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


def log_band_edges(n_bands, lo=20.0, hi=20000.0):
    """N+1 log-spaced edges from 20Hz-20kHz — log spacing gives bass more
    of the available bands than a linear split would, which matches how
    music actually distributes energy (and how ears perceive pitch)."""
    return list(np.logspace(math.log10(lo), math.log10(hi), n_bands + 1))


def analyze_frame(samples, sample_rate=SAMPLE_RATE, n_bands=None, band_edges=None, extra_band_edges=None):
    """Pure analysis step, shared by every mode and by both single-bulb and
    group (orchestrated) sessions. Returns energies (raw + normalized
    fractions for each band), rms, and the frequency arrays in case a mode
    wants the full spectrum (e.g. spectral centroid).

    Sanitizes non-finite input (NaN/Inf) and empty arrays before the FFT --
    found via fuzz testing (see backend/tests/test_audio_fuzz.py) that a
    single NaN sample silently poisons the ENTIRE output (rms, every band
    energy, every fraction all become NaN), which several modes then pass
    straight through to hue (e.g. band_fixed, dominant_band, harmonic_pairs)
    -- a NaN hue reaching the real bulb's set_hsv() call is a genuine bug
    class (garbage/disconnected audio devices can and do emit dropouts),
    not just a theoretical one. Nothing about this changes output for any
    well-formed finite input -- np.nan_to_num and the isfinite check are
    both no-ops when every sample is already finite.

    `extra_band_edges`, if given, additionally computes a second energies/
    fractions split from the SAME spectrum (no second FFT) -- used to expose
    a full-resolution band view for `/status` visualization independent of
    whatever narrower band split a given mode actually uses for its color
    logic (see AudioSession._process)."""
    samples = np.asarray(samples, dtype=np.float64)
    if samples.size == 0:
        samples = np.zeros(1, dtype=np.float64)
    elif not np.all(np.isfinite(samples)):
        samples = np.nan_to_num(samples, nan=0.0, posinf=1.0, neginf=-1.0)

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
    result = {
        "spectrum": spectrum, "freqs": freqs, "rms": rms,
        "energies": energies, "fractions": fractions, "band_edges": band_edges,
    }
    if extra_band_edges is not None:
        extra_energies = [
            _band_energy(spectrum, freqs, extra_band_edges[i], extra_band_edges[i + 1])
            for i in range(len(extra_band_edges) - 1)
        ]
        extra_total = sum(extra_energies) + 1e-9
        result["extra_energies"] = extra_energies
        result["extra_fractions"] = [e / extra_total for e in extra_energies]
        result["extra_band_edges"] = extra_band_edges
    return result


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

    LATENCY_HISTORY_LEN = 50  # rolling window exposed via status() for a latency-graph UI

    def __init__(self, controller, min_dwell_ms=DEFAULT_MIN_DWELL_MS):
        self.controller = controller
        self.min_dwell_ms = max(MIN_DWELL_FLOOR_MS, min_dwell_ms)
        self._pending = None
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._last_sent_at = 0.0
        self._last_latency_ms = None
        self._latency_history = collections.deque(maxlen=self.LATENCY_HISTORY_LEN)
        self._error = None
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
        return {"last_latency_ms": self._last_latency_ms, "error": self._error,
                "min_dwell_ms": self.min_dwell_ms,
                "latency_history_ms": list(self._latency_history)}

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
            except Exception as e:
                self._error = str(e)
            self._last_latency_ms = round((time.time() - t0) * 1000, 1)
            self._latency_history.append(self._last_latency_ms)
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
    beat_strength = round(bass_like / avg, 3) if avg > 1e-9 else 0.0

    # --- basic beat-flash / BPM data hook -------------------------------
    # Deliberately basic: derives BPM straight from this same is_beat signal
    # (a bass-energy spike over its own rolling average) rather than a real
    # beat/tempo estimator -- that's Phase A's lane if/when it lands. This
    # just gives a live-preview UI something real to flash/read without
    # inventing a competing BPM algorithm.
    BEAT_COOLDOWN_S = 0.12  # refuse to count the same physical hit twice while
                            # bass_like stays elevated across consecutive frames
    last_beat_at = ctx["last_beat_at"]
    beat_intervals = ctx["beat_intervals"]
    if is_beat and (now - last_beat_at) > BEAT_COOLDOWN_S:
        if last_beat_at > 0:
            beat_intervals.append(now - last_beat_at)
        ctx["last_beat_at"] = now
        last_beat_at = now

    bpm = None
    if len(beat_intervals) >= 3:
        # 30-200 BPM plausible range -- filters out stray huge gaps from a
        # long quiet passage between songs skewing the average.
        valid = [iv for iv in beat_intervals if 0.3 <= iv <= 2.0]
        if len(valid) >= 3:
            bpm = round(60.0 / (sum(valid) / len(valid)), 1)

    ctx["latest_bands"] = {
        "fractions": [round(f, 3) for f in fractions], "rms": round(rms, 5),
        "is_beat": is_beat, "is_hard_hit": is_hard_hit, "beat_strength": beat_strength,
        "ms_since_beat": round((now - last_beat_at) * 1000, 1) if last_beat_at > 0 else None,
        "bpm": bpm,
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
        "last_beat_at": 0.0,
        "beat_intervals": collections.deque(maxlen=8),
        "latest_bands": {
            "fractions": [], "rms": 0.0, "is_beat": False, "is_hard_hit": False,
            "beat_strength": 0.0, "ms_since_beat": None, "bpm": None,
        },
    }


class AudioSession:
    """Single-bulb audio-reactive session. See `_apply_mode` for what each
    mode actually computes; this class is just capture + lifecycle +
    dispatch via its own `BulbSender`."""

    def __init__(self, controller, device_index, mode="band_fixed", sensitivity=1.0,
                 monochrome_hue=280.0, n_bands=3, min_dwell_ms=DEFAULT_MIN_DWELL_MS,
                 device_key=None, agc_enabled=False, noise_gate_enabled=True,
                 dc_removal_enabled=True, noise_gate_floor=None, agc_target_rms=None,
                 agc_attack_ms=None, agc_release_ms=None, band_gains=None,
                 use_saved_calibration=True):
        self.controller = controller
        self.device_index = device_index
        self.mode = mode if mode in MODES else "band_fixed"
        self.n_bands = max(3, min(16, n_bands))
        self.band_edges = log_band_edges(self.n_bands)
        self.ctx = _new_ctx(sensitivity, monochrome_hue)
        self.sender = BulbSender(controller, min_dwell_ms)

        # Signal conditioning (Section 4: AGC / noise gate / clip detection /
        # DC removal / per-band gain). `device_key` lets a previously saved
        # per-device calibration (see audio_signal.calibrate_from_device)
        # supply the noise-gate floor automatically unless the caller passed
        # an explicit override.
        self.device_key = device_key
        calibration = (audio_signal.get_device_calibration(device_key)
                       if (use_saved_calibration and device_key) else None)
        floor = noise_gate_floor
        if floor is None:
            floor = calibration["noise_gate_floor"] if calibration else audio_signal.DEFAULT_NOISE_GATE_FLOOR
        self.conditioner = audio_signal.SignalConditioner(
            sample_rate=SAMPLE_RATE,
            agc_enabled=agc_enabled,
            noise_gate_enabled=noise_gate_enabled,
            dc_removal_enabled=dc_removal_enabled,
            noise_gate_floor=floor,
            target_rms=agc_target_rms or audio_signal.DEFAULT_AGC_TARGET_RMS,
            attack_ms=agc_attack_ms or audio_signal.DEFAULT_AGC_ATTACK_MS,
            release_ms=agc_release_ms or audio_signal.DEFAULT_AGC_RELEASE_MS,
            band_gains=band_gains,
        )

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
            "bands_full": self.ctx.get(
                "latest_full_bands", {"n_bands": self.n_bands, "fractions": [], "energies": []}),
            "signal": self.conditioner.last_meter or {},
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

        # Signal conditioning (DC removal / noise gate / AGC) runs on the
        # RAW captured samples -- the silence-timeout check above deliberately
        # uses `samples` (not conditioned), so a low noise-gate floor can
        # never mask genuine audio activity from the timeout's perspective.
        conditioned, meter = self.conditioner.process(samples)

        n_bands = self.n_bands if self.mode in ("spectrum_gradient", "band_flash_overlay", "harmonic_pairs") else 3
        mode_edges = log_band_edges(n_bands) if n_bands != 3 else None
        # `extra_band_edges=self.band_edges` reuses the SAME FFT to also
        # expose a full self.n_bands-resolution split for `/status`
        # (Section 5's "full spectrum bar visualizer" data source),
        # independent of whichever narrower split the active mode uses for
        # its own color logic.
        bands = analyze_frame(conditioned, band_edges=mode_edges, extra_band_edges=self.band_edges)
        bands = self.conditioner.apply_band_gains(bands)

        self.ctx["latest_full_bands"] = {
            "n_bands": self.n_bands,
            "fractions": [round(f, 4) for f in bands.get("extra_fractions", bands["fractions"])],
            "energies": [round(e, 6) for e in bands.get("extra_energies", bands["energies"])],
        }

        action = _apply_mode(self.mode, bands, self.ctx)
        self.sender.queue(action)


class GroupAudioSession:
    """Orchestrates several bulbs off ONE shared audio capture — analysis
    runs once per callback and every bulb gets its own derived target,
    rather than opening N redundant streams against the same device. Each
    bulb still has its own `BulbSender`, so one slow/offline bulb in the
    group can never stall the others.

    role_mode:
      - "unison"       — every bulb shows the identical color.
      - "phase_offset" — same effect, hue shifted per bulb (a simple chase).
      - "band_split"   — bulb i is primarily driven by band i of an
                          N-band split (N = number of bulbs), a literal
                          per-bulb "this one is the bass bulb" assignment.
    """

    def __init__(self, controllers, device_index, mode="band_fixed", role_mode="unison",
                 sensitivity=1.0, monochrome_hue=280.0, min_dwell_ms=DEFAULT_MIN_DWELL_MS):
        self.controllers = controllers
        self.device_index = device_index
        self.mode = mode if mode in MODES else "band_fixed"
        self.role_mode = role_mode if role_mode in ROLE_MODES else "unison"
        self.senders = [BulbSender(c, min_dwell_ms) for c in controllers]
        self.ctxs = [_new_ctx(sensitivity, monochrome_hue) for _ in controllers]
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

    def status(self):
        return {
            "active": self.is_alive(),
            "mode": self.mode,
            "role_mode": self.role_mode,
            "bulb_count": len(self.controllers),
            "bulbs": [{"bands": ctx["latest_bands"], "sender": s.status()}
                      for ctx, s in zip(self.ctxs, self.senders)],
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

    def _process(self, samples):
        now = time.time()
        n = len(self.controllers)
        rms_check = float(np.sqrt(np.mean(np.square(samples))))
        if rms_check > 0.0008:
            self._last_audio_at = now

        if self.role_mode == "band_split":
            bands = analyze_frame(samples, band_edges=log_band_edges(max(3, n)))
            for i in range(n):
                # each bulb sees a band-split view rotated so bulb i's
                # "primary" band comes first — reuses the same mode logic,
                # just with each bulb's fractions rolled to foreground its
                # assigned band.
                rolled = {**bands, "fractions": bands["fractions"][i:] + bands["fractions"][:i],
                          "energies": bands["energies"][i:] + bands["energies"][:i]}
                action = _apply_mode(self.mode, rolled, self.ctxs[i])
                self.senders[i].queue(action)
            return

        bands = analyze_frame(samples)
        base_action = None
        for i in range(n):
            action = _apply_mode(self.mode, bands, self.ctxs[i])
            if self.role_mode == "phase_offset" and action[0] == "hsv":
                offset = (360.0 / n) * i
                action = ("hsv", (action[1] + offset) % 360, action[2], action[3])
            self.senders[i].queue(action)


# ------------------------------------------------------------ sessions ---
_sessions = {}          # single-bulb sessions, keyed by device id
_group_sessions = {}    # group sessions, keyed by group id
_sessions_lock = threading.Lock()


def start_session(controller, device_index, mode="band_fixed", sensitivity=1.0,
                   monochrome_hue=280.0, n_bands=3, min_dwell_ms=DEFAULT_MIN_DWELL_MS,
                   device_key=None, agc_enabled=False, noise_gate_enabled=True,
                   dc_removal_enabled=True, noise_gate_floor=None, agc_target_rms=None,
                   agc_attack_ms=None, agc_release_ms=None, band_gains=None,
                   use_saved_calibration=True):
    with _sessions_lock:
        existing = _sessions.get(controller.cfg["id"])
        if existing and existing.is_alive():
            existing.stop()
        session = AudioSession(controller, device_index, mode, sensitivity, monochrome_hue, n_bands, min_dwell_ms,
                                device_key=device_key, agc_enabled=agc_enabled,
                                noise_gate_enabled=noise_gate_enabled, dc_removal_enabled=dc_removal_enabled,
                                noise_gate_floor=noise_gate_floor, agc_target_rms=agc_target_rms,
                                agc_attack_ms=agc_attack_ms, agc_release_ms=agc_release_ms,
                                band_gains=band_gains, use_saved_calibration=use_saved_calibration)
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
                         sensitivity=1.0, monochrome_hue=280.0, min_dwell_ms=DEFAULT_MIN_DWELL_MS):
    with _sessions_lock:
        existing = _group_sessions.get(group_id)
        if existing and existing.is_alive():
            existing.stop()
        session = GroupAudioSession(controllers, device_index, mode, role_mode, sensitivity, monochrome_hue, min_dwell_ms)
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
