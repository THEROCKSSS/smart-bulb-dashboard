import collections
import math
import threading
import time

import numpy as np
import sounddevice as sd

import audio_presets
import audio_safety
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
MIN_DWELL_MS_CEILING = 5000   # config-validation ceiling — see validate_start_config
SILENCE_TIMEOUT_S = 300

# --- Week 1 Phase D: session hardening / management constants ---------------
# A short, explicit socket timeout specifically for audio-reactive bulb
# sends. Documented as a known unresolved limitation in
# iterations/003-audio-engine-v2/README.md: without this, a slow/offline
# bulb ties up its BulbSender thread for tinytuya's full default connection
# timeout (5s, see tinytuya.core.XenonDevice's default) on a single send
# attempt. 2s is short enough that one bad send doesn't meaningfully delay
# picking up the next fresh queued value, but long enough to not spuriously
# time out a bulb that's just a little slow on the local network.
AUDIO_SEND_SOCKET_TIMEOUT_S = 2.0
DEFAULT_SOCKET_TIMEOUT_S = 5.0  # tinytuya's own default — restored on sender.stop()
WATCHDOG_STALL_S = 6.0          # generous margin over AUDIO_SEND_SOCKET_TIMEOUT_S
WATCHDOG_POLL_INTERVAL_S = 1.0  # how often the watchdog checks for a stall
CAPTURE_MAXLEN = 20000          # ~30+ min of captured points at the default dwell

N_BANDS_MIN, N_BANDS_MAX = 3, 16
MAX_DURATION_S_CEILING = 24 * 3600
WARMUP_S_CEILING = 120
MAX_CONSECUTIVE_STREAM_FAILURES = 5  # graceful-degradation retry budget before giving up

DEFAULT_AUTO_RESUME_GRACE_S = 8.0    # auto-resume-after-manual-command grace period
APPLAUSE_COOLDOWN_S = 4.0
APPLAUSE_FLASH_DURATION_S = 0.35

RATE_LIMIT_MAX_CALLS = 6
RATE_LIMIT_WINDOW_S = 10.0


class AudioConfigError(ValueError):
    """Raised on an out-of-range session config — callers (main.py) turn
    this into a clear HTTP 400 rather than a generic 500."""

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


# --------------------------------------------------- Phase D pure helpers ---
# Small, independently-testable functions used by session start validation,
# session lifecycle (warmup/applause), and device fallback. None of these
# touch _apply_mode or the MODES list — they wrap/validate around it.

def validate_start_config(n_bands, min_dwell_ms, max_duration_s=None, warmup_s=None,
                           mode=None, disable_flash_heavy=False):
    """Raises AudioConfigError with a clear message on an out-of-range
    value. Callers (main.py) turn this into an HTTP 400."""
    if not isinstance(n_bands, int) or not (N_BANDS_MIN <= n_bands <= N_BANDS_MAX):
        raise AudioConfigError(f"n_bands must be an integer between {N_BANDS_MIN} and {N_BANDS_MAX}, got {n_bands!r}")
    if not isinstance(min_dwell_ms, (int, float)) or min_dwell_ms < MIN_DWELL_FLOOR_MS:
        raise AudioConfigError(f"min_dwell_ms below the safety floor of {MIN_DWELL_FLOOR_MS}ms, got {min_dwell_ms!r}")
    if min_dwell_ms > MIN_DWELL_MS_CEILING:
        raise AudioConfigError(f"min_dwell_ms above the sane ceiling of {MIN_DWELL_MS_CEILING}ms, got {min_dwell_ms!r}")
    if max_duration_s is not None and (not isinstance(max_duration_s, (int, float)) or not (0 < max_duration_s <= MAX_DURATION_S_CEILING)):
        raise AudioConfigError(f"max_duration_s must be between 0 and {MAX_DURATION_S_CEILING}, got {max_duration_s!r}")
    if warmup_s is not None and (not isinstance(warmup_s, (int, float)) or not (0 <= warmup_s <= WARMUP_S_CEILING)):
        raise AudioConfigError(f"warmup_s must be between 0 and {WARMUP_S_CEILING}, got {warmup_s!r}")
    if mode is not None and disable_flash_heavy and audio_safety.is_flash_heavy(mode):
        raise AudioConfigError(
            f"mode '{mode}' is flash-heavy but disable_flash_heavy is set — choose an ambient "
            f"mode or leave disable_flash_heavy off"
        )


def apply_warmup(action, elapsed_s, warmup_s):
    """Fades brightness in from ~0 over the first `warmup_s` seconds of a
    session instead of snapping straight to whatever the mode computes, by
    scaling the brightness component of `action` by elapsed/warmup_s (hue
    and saturation pass through unchanged so the *color* is already correct
    from the first frame, only intensity ramps)."""
    if not warmup_s or warmup_s <= 0 or elapsed_s >= warmup_s:
        return action
    factor = max(0.0, min(1.0, elapsed_s / warmup_s))
    if action[0] == "hsv":
        return (action[0], action[1], action[2], action[3] * factor)
    return (action[0], action[1], action[2], action[3], action[4] * factor)


def _one_shot_flash_action(action):
    """Used by the applause/cheer detector below: pop brightness to full for
    one flash burst while leaving hue/saturation (the mode's own color
    choice) alone. Still passes back through audio_safety.apply_flash_cap,
    so it can never itself exceed the flash-rate safety ceiling."""
    if action[0] == "hsv":
        return (action[0], action[1], action[2], 100)
    return (action[0], action[1], action[2], action[3], 100)


def detect_applause(bands, ctx, now, cooldown_s=APPLAUSE_COOLDOWN_S):
    """Broadband loud-burst detector, distinct from the bass-only beat
    detector _apply_mode already computes (`is_beat`/`is_hard_hit`, driven
    solely by the low band via `ctx["rolling_bass"]`). Applause/cheering
    reads as a sudden, roughly *flat* rise across every band (closer to
    white noise) rather than a bass-dominated kick — that spectral-flatness
    check is what distinguishes this from a normal beat/kick hit."""
    rms = bands["rms"]
    fractions = bands["fractions"]
    rolling = ctx.setdefault("_applause_rolling_rms", collections.deque(maxlen=60))
    rolling.append(rms)
    last_at = ctx.get("_applause_last_at", 0.0)
    if len(rolling) < 20 or (now - last_at) < cooldown_s:
        return False
    avg = sum(rolling) / len(rolling)
    max_frac = max(fractions) if fractions else 1.0
    is_broadband = max_frac < 0.55  # no single band dominates -> not a musical kick
    is_loud_burst = rms > max(avg * 2.5, 1e-6) and rms > 0.02
    if is_loud_burst and is_broadband:
        ctx["_applause_last_at"] = now
        return True
    return False


def resolve_device_index(requested_index, fallback_index=None):
    """If `requested_index` still names a real input-capable device, use it
    unchanged. Otherwise (device disappeared / bad index) fall back to
    `fallback_index` if one is configured, else `None` — which tells
    sounddevice to use the system default input device instead of erroring.
    Returns (index_to_use, fallback_was_used)."""
    try:
        devices = sd.query_devices()
        if requested_index is not None and 0 <= requested_index < len(devices):
            if devices[requested_index].get("max_input_channels", 0) > 0:
                return requested_index, False
    except Exception:
        pass
    return fallback_index, True


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
        self._restart_count = 0

        # Section 9: a short, explicit socket timeout specifically for
        # audio-reactive sends (see AUDIO_SEND_SOCKET_TIMEOUT_S's docstring
        # above for why — iterations/003's known unresolved limitation).
        # Guarded because the pytest fake device (and possibly a real
        # tinytuya device on an old version) may not expose this method.
        try:
            self.controller.set_socket_timeout(AUDIO_SEND_SOCKET_TIMEOUT_S)
        except Exception:
            pass

        # Light-show capture (Section 12): every action this sender actually
        # attempts to send, with a timestamp relative to sender creation.
        self._capture_t0 = time.time()
        self._captured = collections.deque(maxlen=CAPTURE_MAXLEN)

        # Watchdog (Section 9): if the send thread stalls inside a blocking
        # network call for longer than WATCHDOG_STALL_S, restart it. The
        # stalled thread is abandoned (daemon, harmless) rather than killed —
        # Python has no safe way to force-terminate a thread mid-syscall.
        self._last_heartbeat = time.time()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self._watchdog_thread = threading.Thread(target=self._watchdog_loop, daemon=True)
        self._watchdog_thread.start()

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
        if self._watchdog_thread.is_alive():
            self._watchdog_thread.join(timeout=2)
        try:
            self.controller.set_socket_timeout(DEFAULT_SOCKET_TIMEOUT_S)
        except Exception:
            pass

    def status(self):
        return {"last_latency_ms": self._last_latency_ms, "error": self._error,
                "min_dwell_ms": self.min_dwell_ms, "restart_count": self._restart_count}

    def get_captured_points(self):
        """Converts every captured raw action into the {"t","h","s","v"}
        shape used by audio_lightshow's export/replay."""
        import colorsys
        points = []
        for t, action in self._captured:
            if action[0] == "hsv":
                h, s, v = action[1], action[2], action[3]
            else:
                _, r, g, b, brightness = action
                hh, ss, _vv = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
                h, s, v = hh * 360, ss * 100, brightness
            points.append({"t": round(t, 4), "h": round(h, 2), "s": round(s, 2), "v": round(v, 2)})
        return points

    def _watchdog_loop(self):
        while not self._stop.wait(WATCHDOG_POLL_INTERVAL_S):
            stalled_for = time.time() - self._last_heartbeat
            if stalled_for > WATCHDOG_STALL_S and self._thread.is_alive():
                self._error = f"sender stalled for {stalled_for:.1f}s (likely a hung bulb send) — restarting"
                self._restart_count += 1
                self._thread = threading.Thread(target=self._loop, daemon=True)
                self._last_heartbeat = time.time()
                self._thread.start()

    def _loop(self):
        while not self._stop.is_set():
            self._last_heartbeat = time.time()
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
            self._captured.append((time.time() - self._capture_t0, action))
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
            self._last_sent_at = time.time()
            self._last_heartbeat = time.time()


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
    dispatch via its own `BulbSender`.

    Week 1 Phase D additions (session management/hardening — none of this
    touches `_apply_mode`'s per-mode formulas):
      - `max_duration_s`: an auto-stop ceiling distinct from the silence
        timeout (e.g. "never run longer than 2 hours unattended").
      - `warmup_s`: fades brightness in from ~0 instead of snapping to full
        on start (see `apply_warmup`).
      - `auto_resume_grace_s`: how long a manual color/scene command pauses
        this session for before it resumes reacting on its own (see
        `pause_for_manual`/`notify_manual_command`).
      - `max_flash_rate_hz` / `disable_flash_heavy`: the photosensitive-
        epilepsy safety cap (see audio_safety.apply_flash_cap), enforced
        every frame regardless of mode.
      - `silence_auto_off`: when the existing silence timeout fires, also
        power the bulb off instead of just stopping the reactive loop.
      - device fallback + graceful stream-error restart: see `_run`.
    """

    def __init__(self, controller, device_index, mode="band_fixed", sensitivity=1.0,
                 monochrome_hue=280.0, n_bands=3, min_dwell_ms=DEFAULT_MIN_DWELL_MS,
                 max_duration_s=None, warmup_s=0.0, auto_resume_grace_s=DEFAULT_AUTO_RESUME_GRACE_S,
                 max_flash_rate_hz=None, disable_flash_heavy=False, max_brightness_swing=None,
                 silence_auto_off=True, fallback_device_index=None):
        self.controller = controller
        self.requested_device_index = device_index
        self.device_index, self.device_fallback_used = resolve_device_index(device_index, fallback_device_index)
        self.fallback_device_index = fallback_device_index
        self.mode = mode if mode in MODES else "band_fixed"
        self.n_bands = max(N_BANDS_MIN, min(N_BANDS_MAX, n_bands))
        self.band_edges = log_band_edges(self.n_bands)
        self.ctx = _new_ctx(sensitivity, monochrome_hue)
        self.sender = BulbSender(controller, min_dwell_ms)

        self._stop = threading.Event()
        self._thread = None
        self._error = None
        self._last_audio_at = time.time()
        self._stereo = (mode == "stereo_split")

        self._started_at = time.time()
        self.max_duration_s = max_duration_s
        self.warmup_s = max(0.0, warmup_s or 0.0)
        self.auto_resume_grace_s = auto_resume_grace_s
        settings = audio_safety.get_safety_settings()
        self.max_flash_rate_hz = audio_safety.clamp_max_flash_rate(
            max_flash_rate_hz if max_flash_rate_hz is not None else settings["max_flash_rate_hz"])
        self.disable_flash_heavy = disable_flash_heavy or settings["disable_flash_heavy"]
        self.max_brightness_swing = max_brightness_swing
        self.silence_auto_off = silence_auto_off
        self._paused_until = None
        self._restart_count = 0

    def start(self):
        self.controller.stop_effect()
        self.controller._log("audio_reactive_start", {
            "mode": self.mode, "device_index": self.device_index,
            "device_fallback_used": self.device_fallback_used,
        })
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        _last_capture[self.controller.cfg["id"]] = self.sender.get_captured_points()
        self.sender.stop()
        self.controller._log("audio_reactive_stop", {"mode": self.mode})

    def is_alive(self):
        return self._thread is not None and self._thread.is_alive()

    # -- Section 8: manual-command auto-pause / auto-resume -----------------
    def pause_for_manual(self, grace_s=None):
        grace = self.auto_resume_grace_s if grace_s is None else grace_s
        self._paused_until = time.time() + grace
        self.controller._log("audio_reactive_auto_pause", {"grace_s": grace})

    def _check_paused(self):
        if self._paused_until is None:
            return False
        if time.time() >= self._paused_until:
            self._paused_until = None
            self.controller._log("audio_reactive_auto_resume", {})
            return False
        return True

    def confirmation(self):
        """Section 8: exactly what was actually applied on start, so a
        client never has to guess (config may have been clamped/resolved
        from what was requested)."""
        return {
            "device_id": self.controller.cfg["id"],
            "device_index": self.device_index,
            "requested_device_index": self.requested_device_index,
            "device_fallback_used": self.device_fallback_used,
            "mode": self.mode,
            "n_bands": self.n_bands,
            "min_dwell_ms": self.sender.min_dwell_ms,
            "sensitivity": self.ctx["sensitivity"],
            "max_duration_s": self.max_duration_s,
            "warmup_s": self.warmup_s,
            "max_flash_rate_hz": self.max_flash_rate_hz,
        }

    def status(self):
        now = time.time()
        return {
            "active": self.is_alive(),
            "mode": self.mode,
            "device_index": self.device_index,
            "device_fallback_used": self.device_fallback_used,
            "sensitivity": self.ctx["sensitivity"],
            "n_bands": self.n_bands,
            "bands": self.ctx["latest_bands"],
            "sender": self.sender.status(),
            "error": self._error,
            "started_at": self._started_at,
            "elapsed_s": round(now - self._started_at, 1),
            "max_duration_s": self.max_duration_s,
            "warmup_s": self.warmup_s,
            "paused": self._paused_until is not None and now < self._paused_until,
            "paused_until": self._paused_until,
            "max_flash_rate_hz": self.max_flash_rate_hz,
            "restart_count": self._restart_count,
        }

    def _run(self):
        consecutive_failures = 0
        while not self._stop.is_set():
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
                    consecutive_failures = 0  # a clean open resets the failure streak
                    while not self._stop.is_set():
                        if time.time() - self._last_audio_at > SILENCE_TIMEOUT_S:
                            self.controller._log("audio_reactive_timeout", {"mode": self.mode})
                            if self.silence_auto_off:
                                try:
                                    self.controller.power(False)
                                    self.controller._log("audio_reactive_silence_auto_off", {})
                                except Exception as e:
                                    self.controller._log("audio_reactive_silence_auto_off", ok=False, error=str(e))
                            return
                        if self.max_duration_s is not None and (time.time() - self._started_at) > self.max_duration_s:
                            self.controller._log("audio_reactive_max_duration_reached",
                                                  {"max_duration_s": self.max_duration_s})
                            return
                        self._stop.wait(0.25)
                return
            except Exception as e:
                self._error = str(e)
                self.controller._log("audio_reactive_error", {"mode": self.mode}, ok=False, error=str(e))
                consecutive_failures += 1
                self._restart_count += 1
                if self._stop.is_set() or consecutive_failures > MAX_CONSECUTIVE_STREAM_FAILURES:
                    self.controller._log("audio_reactive_gave_up", {"consecutive_failures": consecutive_failures})
                    return
                # Graceful degradation (Section 9): don't just die on a
                # stream error — re-resolve the device (this is also what
                # picks up the configured fallback if the device
                # disappeared) and retry after a short backoff.
                self.device_index, self.device_fallback_used = resolve_device_index(
                    self.requested_device_index, self.fallback_device_index)
                if self._stop.wait(min(5.0, 0.5 * consecutive_failures)):
                    return

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

        if self.warmup_s > 0:
            action = apply_warmup(action, now - self._started_at, self.warmup_s)

        # Section 14: applause/cheer one-shot flash, gated by its own
        # cooldown and independent of any mode's own logic.
        if detect_applause(bands, self.ctx, now):
            self.ctx["_applause_flash_until"] = now + APPLAUSE_FLASH_DURATION_S
        if self.ctx.get("_applause_flash_until", 0) > now:
            action = _one_shot_flash_action(action)

        # Section 13: the non-bypassable photosensitive-epilepsy flash cap —
        # applied last, after every other transformation above, so nothing
        # (mode, warmup, applause flash) can exceed it.
        action = audio_safety.apply_flash_cap(action, self.ctx, self.max_flash_rate_hz, self.max_brightness_swing)

        # Section 8: auto-pause while a manual command is in its grace
        # window — don't overwrite whatever the user just manually set.
        if self._check_paused():
            return
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
                 sensitivity=1.0, monochrome_hue=280.0, min_dwell_ms=DEFAULT_MIN_DWELL_MS,
                 max_duration_s=None, warmup_s=0.0, max_flash_rate_hz=None,
                 disable_flash_heavy=False, silence_auto_off=True, fallback_device_index=None):
        self.controllers = controllers
        self.requested_device_index = device_index
        self.device_index, self.device_fallback_used = resolve_device_index(device_index, fallback_device_index)
        self.fallback_device_index = fallback_device_index
        self.mode = mode if mode in MODES else "band_fixed"
        self.role_mode = role_mode if role_mode in ROLE_MODES else "unison"
        self.senders = [BulbSender(c, min_dwell_ms) for c in controllers]
        self.ctxs = [_new_ctx(sensitivity, monochrome_hue) for _ in controllers]
        self._stop = threading.Event()
        self._thread = None
        self._error = None
        self._last_audio_at = time.time()

        self._started_at = time.time()
        self.max_duration_s = max_duration_s
        self.warmup_s = max(0.0, warmup_s or 0.0)
        settings = audio_safety.get_safety_settings()
        self.max_flash_rate_hz = audio_safety.clamp_max_flash_rate(
            max_flash_rate_hz if max_flash_rate_hz is not None else settings["max_flash_rate_hz"])
        self.disable_flash_heavy = disable_flash_heavy or settings["disable_flash_heavy"]
        self.silence_auto_off = silence_auto_off
        self._restart_count = 0
        self._paused_bulbs = {}  # controller index -> resume-at timestamp

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

    # -- Section 8: pause just the one bulb a manual command touched --------
    def pause_bulb_for_manual(self, device_id, grace_s=None):
        for i, c in enumerate(self.controllers):
            if c.cfg["id"] == device_id:
                grace = DEFAULT_AUTO_RESUME_GRACE_S if grace_s is None else grace_s
                self._paused_bulbs[i] = time.time() + grace
                c._log("audio_reactive_auto_pause", {"grace_s": grace})
                return True
        return False

    def status(self):
        now = time.time()
        return {
            "active": self.is_alive(),
            "mode": self.mode,
            "role_mode": self.role_mode,
            "bulb_count": len(self.controllers),
            "bulbs": [{"bands": ctx["latest_bands"], "sender": s.status(),
                       "paused": i in self._paused_bulbs and now < self._paused_bulbs[i]}
                      for i, (ctx, s) in enumerate(zip(self.ctxs, self.senders))],
            "error": self._error,
            "started_at": self._started_at,
            "elapsed_s": round(now - self._started_at, 1),
            "max_duration_s": self.max_duration_s,
            "warmup_s": self.warmup_s,
            "max_flash_rate_hz": self.max_flash_rate_hz,
            "restart_count": self._restart_count,
            "device_index": self.device_index,
            "device_fallback_used": self.device_fallback_used,
        }

    def _run(self):
        consecutive_failures = 0
        while not self._stop.is_set():
            try:
                def callback(indata, frames, time_info, status_flags):
                    self._process(indata[:, 0])

                with sd.InputStream(device=self.device_index, channels=1, samplerate=SAMPLE_RATE,
                                     blocksize=BLOCK_SIZE, latency="low", callback=callback):
                    consecutive_failures = 0
                    while not self._stop.is_set():
                        if time.time() - self._last_audio_at > SILENCE_TIMEOUT_S:
                            if self.silence_auto_off:
                                for c in self.controllers:
                                    try:
                                        c.power(False)
                                    except Exception:
                                        pass
                            return
                        if self.max_duration_s is not None and (time.time() - self._started_at) > self.max_duration_s:
                            return
                        self._stop.wait(0.25)
                return
            except Exception as e:
                self._error = str(e)
                consecutive_failures += 1
                self._restart_count += 1
                if self._stop.is_set() or consecutive_failures > MAX_CONSECUTIVE_STREAM_FAILURES:
                    return
                self.device_index, self.device_fallback_used = resolve_device_index(
                    self.requested_device_index, self.fallback_device_index)
                if self._stop.wait(min(5.0, 0.5 * consecutive_failures)):
                    return

    def _process(self, samples):
        now = time.time()
        n = len(self.controllers)
        rms_check = float(np.sqrt(np.mean(np.square(samples))))
        if rms_check > 0.0008:
            self._last_audio_at = now

        def _finalize_and_queue(i, action):
            if self.warmup_s > 0:
                action = apply_warmup(action, now - self._started_at, self.warmup_s)
            action = audio_safety.apply_flash_cap(action, self.ctxs[i], self.max_flash_rate_hz)
            resume_at = self._paused_bulbs.get(i)
            if resume_at is not None:
                if now < resume_at:
                    return
                del self._paused_bulbs[i]
            self.senders[i].queue(action)

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
                _finalize_and_queue(i, action)
            return

        bands = analyze_frame(samples)
        for i in range(n):
            action = _apply_mode(self.mode, bands, self.ctxs[i])
            if self.role_mode == "phase_offset" and action[0] == "hsv":
                offset = (360.0 / n) * i
                action = ("hsv", (action[1] + offset) % 360, action[2], action[3])
            _finalize_and_queue(i, action)


# ------------------------------------------------------------ sessions ---
_sessions = {}          # single-bulb sessions, keyed by device id
_group_sessions = {}    # group sessions, keyed by group id
_sessions_lock = threading.Lock()
_last_capture = {}      # device_id -> captured lightshow points, kept after stop() for export

_rate_limit_hits = {}   # rate-limit key -> deque of call timestamps
_rate_limit_lock = threading.Lock()


def start_session(controller, device_index, mode="band_fixed", sensitivity=1.0,
                   monochrome_hue=280.0, n_bands=3, min_dwell_ms=DEFAULT_MIN_DWELL_MS,
                   max_duration_s=None, warmup_s=0.0, auto_resume_grace_s=DEFAULT_AUTO_RESUME_GRACE_S,
                   max_flash_rate_hz=None, disable_flash_heavy=False, max_brightness_swing=None,
                   silence_auto_off=True, fallback_device_index=None):
    """Raises AudioConfigError on an invalid config (caller turns this into
    an HTTP 400) before anything is started. On success, persists this
    config as the "last known good session" for this device (Section 8's
    one-click resume-after-restart) and returns the started AudioSession —
    call `.confirmation()` on it for exactly what was applied."""
    validate_start_config(n_bands, min_dwell_ms, max_duration_s, warmup_s, mode, disable_flash_heavy)
    with _sessions_lock:
        existing = _sessions.get(controller.cfg["id"])
        if existing and existing.is_alive():
            existing.stop()
        session = AudioSession(controller, device_index, mode, sensitivity, monochrome_hue, n_bands, min_dwell_ms,
                                max_duration_s=max_duration_s, warmup_s=warmup_s,
                                auto_resume_grace_s=auto_resume_grace_s, max_flash_rate_hz=max_flash_rate_hz,
                                disable_flash_heavy=disable_flash_heavy, max_brightness_swing=max_brightness_swing,
                                silence_auto_off=silence_auto_off, fallback_device_index=fallback_device_index)
        _sessions[controller.cfg["id"]] = session
        session.start()
    audio_presets.save_last_session(controller.cfg["id"], {
        "device_index": device_index, "mode": mode, "sensitivity": sensitivity,
        "monochrome_hue": monochrome_hue, "n_bands": n_bands, "min_dwell_ms": min_dwell_ms,
        "max_duration_s": max_duration_s, "warmup_s": warmup_s,
        "auto_resume_grace_s": auto_resume_grace_s, "max_flash_rate_hz": max_flash_rate_hz,
        "disable_flash_heavy": disable_flash_heavy,
    })
    return session


def resume_last_session(controller):
    """Section 8: one-click 'resume last session' after a restart. Returns
    the started AudioSession, or None if no last-known-good config exists
    for this device."""
    record = audio_presets.load_last_session(controller.cfg["id"])
    if not record:
        return None
    cfg = record["config"]
    return start_session(
        controller,
        cfg.get("device_index"),
        cfg.get("mode", "band_fixed"),
        cfg.get("sensitivity", 1.0),
        cfg.get("monochrome_hue", 280.0),
        cfg.get("n_bands", 3),
        cfg.get("min_dwell_ms", DEFAULT_MIN_DWELL_MS),
        max_duration_s=cfg.get("max_duration_s"),
        warmup_s=cfg.get("warmup_s", 0.0),
        auto_resume_grace_s=cfg.get("auto_resume_grace_s", DEFAULT_AUTO_RESUME_GRACE_S),
        max_flash_rate_hz=cfg.get("max_flash_rate_hz"),
        disable_flash_heavy=cfg.get("disable_flash_heavy", False),
    )


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


def get_active_session(device_id):
    """Public accessor for the live AudioSession object (if any), so
    callers like the lightshow-export route don't need to reach into the
    module-private `_sessions` dict directly."""
    with _sessions_lock:
        session = _sessions.get(device_id)
    return session if session and session.is_alive() else None


def start_group_session(group_id, controllers, device_index, mode="band_fixed", role_mode="unison",
                         sensitivity=1.0, monochrome_hue=280.0, min_dwell_ms=DEFAULT_MIN_DWELL_MS,
                         max_duration_s=None, warmup_s=0.0, max_flash_rate_hz=None,
                         disable_flash_heavy=False, silence_auto_off=True, fallback_device_index=None):
    validate_start_config(n_bands=3, min_dwell_ms=min_dwell_ms, max_duration_s=max_duration_s,
                          warmup_s=warmup_s, mode=mode, disable_flash_heavy=disable_flash_heavy)
    with _sessions_lock:
        existing = _group_sessions.get(group_id)
        if existing and existing.is_alive():
            existing.stop()
        session = GroupAudioSession(controllers, device_index, mode, role_mode, sensitivity, monochrome_hue,
                                     min_dwell_ms, max_duration_s=max_duration_s, warmup_s=warmup_s,
                                     max_flash_rate_hz=max_flash_rate_hz, disable_flash_heavy=disable_flash_heavy,
                                     silence_auto_off=silence_auto_off, fallback_device_index=fallback_device_index)
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


# --------------------------------------------------- Section 8: conflicts --
def check_group_conflict(device_ids):
    """Returns the subset of `device_ids` that are currently in an *active
    solo* audio-reactive session — starting a group session across these
    devices without addressing this would leave two independent senders
    fighting over the same bulb."""
    with _sessions_lock:
        return [d for d in device_ids if d in _sessions and _sessions[d].is_alive()]


def check_solo_conflict(device_id):
    """The symmetric check: group session(s) currently active that already
    include this device."""
    with _sessions_lock:
        return [gid for gid, gs in _group_sessions.items()
                if gs.is_alive() and any(c.cfg["id"] == device_id for c in gs.controllers)]


# ------------------------------------------- Section 8: manual auto-pause --
def notify_manual_command(device_id, grace_s=None):
    """Called by main.py's manual color/scene/power/preset routes right
    after a command actually reaches the device, so a running audio-
    reactive session (solo, or just this bulb within a group session) backs
    off for a grace period instead of immediately overwriting what the user
    just manually set."""
    with _sessions_lock:
        solo = _sessions.get(device_id)
        groups = list(_group_sessions.values())
    if solo and solo.is_alive():
        solo.pause_for_manual(grace_s)
    for gs in groups:
        if gs.is_alive():
            gs.pause_bulb_for_manual(device_id, grace_s)


# ------------------------------------------------- Section 12: lightshow ---
def get_last_capture(device_id):
    """Points captured by the most recent session (solo) for this device,
    kept around after stop() so a lightshow export can happen after the
    fact, not only while still running."""
    return _last_capture.get(device_id, [])


# ------------------------------------------ Section 9: rate limiting -------
def check_rate_limit(key, max_calls=RATE_LIMIT_MAX_CALLS, window_s=RATE_LIMIT_WINDOW_S):
    """Simple sliding-window rate limit for the start/stop endpoints
    themselves (distinct from any per-bulb dwell/flash pacing) — returns
    False if `key` has already made `max_calls` within the last
    `window_s` seconds."""
    now = time.time()
    with _rate_limit_lock:
        dq = _rate_limit_hits.setdefault(key, collections.deque())
        while dq and now - dq[0] > window_s:
            dq.popleft()
        if len(dq) >= max_calls:
            return False
        dq.append(now)
        return True
