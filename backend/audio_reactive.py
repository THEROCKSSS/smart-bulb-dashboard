import collections
import math
import random
import re
import statistics
import threading
import time

import numpy as np
import sounddevice as sd

import audio_signal
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

FAILOVER_THRESHOLD = 3  # consecutive send failures before a bulb is reported
                        # "offline" in group status -- see BulbSender.status()
                        # and GroupAudioSession's per-bulb status. Crossing
                        # this never stops or restarts the group session;
                        # every other bulb's own independent BulbSender loop
                        # keeps sending on schedule regardless.

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
MIRROR_CENTER_HUE = 270.0    # mirror_mode's fixed reflection point
MIRROR_SWING_DEG = 80.0      # max degrees mirror_mode swings off-center each direction
RANDOM_WALK_MAX_STEP_DEG = 5.0  # per-frame bound on random_walk_hue's step size

# Shared "is there real audio right now" floor -- same value the session
# loops already use inline for their own silence-timeout tracking; pulled
# out as a named constant here because the new modes/TempoTracker below
# need to reference the exact same threshold rather than a second magic
# number that could drift out of sync with it.
SILENCE_RMS_THRESHOLD = 0.0008
SILENCE_FLASH_LONG_THRESHOLD_S = 3.0  # how long a pause counts as "a long silence" for silence_flash_recover

# crescendo_ramp's trend window is frame-count-based, not wall-clock, so it
# behaves identically in tests (which drive it frame-by-frame with no real
# sleeping) and at real audio rates. At BLOCK_SIZE=512/44100Hz each frame is
# ~11.6ms, so 200 frames ~= 2.3s -- "building over a couple of seconds".
CRESCENDO_WINDOW_FRAMES = 200
CRESCENDO_SENSITIVITY = 3.0  # scales the raw (second_half/first_half - 1) ratio into a 0..1 ramp

MODES = [
    "band_fixed", "dominant_band", "weighted_blend", "vu_meter",
    "auto_rotate_hue", "monochrome_pulse", "strobe_on_drop", "palette_cycle",
    "spectrum_gradient", "band_flash_overlay", "stereo_split", "breathing_silence",
    "harmonic_pairs", "kick_snare_split",
    "energy_contour", "bass_only_pulse", "mirror_mode", "random_walk_hue",
    "silence_flash_recover", "crescendo_ramp",
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
        self._consecutive_failures = 0
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
        return {
            "last_latency_ms": self._last_latency_ms, "error": self._error,
            "min_dwell_ms": self.min_dwell_ms,
            "latency_history_ms": list(self._latency_history),
            "consecutive_failures": self._consecutive_failures,
            # Failover visibility only -- crossing the threshold never stops
            # this sender's loop or the group session; it keeps retrying on
            # every future queued action exactly as before, so the bulb
            # rejoins automatically the moment it's reachable again.
            "offline": self._consecutive_failures >= FAILOVER_THRESHOLD,
            "restart_count": self._restart_count,
        }

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
                self._consecutive_failures = 0
            except Exception as e:
                self._error = str(e)
                self._consecutive_failures += 1
            self._last_latency_ms = round((time.time() - t0) * 1000, 1)
            self._latency_history.append(self._last_latency_ms)
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

    if mode == "energy_contour":
        # Hue is locked to the user's chosen monochrome hue; only
        # saturation and brightness track a *smoothed* energy envelope
        # (an EMA of rms*gain, not the instantaneous value), so this reads
        # as a slow-moving "contour" rather than monochrome_pulse's more
        # instantaneous beat-punchy response.
        envelope = ctx.setdefault("energy_envelope", 0.0)
        instant = min(1.0, rms * gain * 20)
        envelope = envelope * 0.85 + instant * 0.15
        ctx["energy_envelope"] = envelope
        sat = max(0, min(100, 40 + 60 * envelope))
        brightness = max(4, min(100, 4 + 96 * envelope))
        return ("hsv", ctx["monochrome_hue"], sat, brightness)

    if mode == "bass_only_pulse":
        # Brightness-only pulse driven purely by the bass band's fractional
        # share of the mix (not overall rms alone) -- hue never moves.
        bass_frac = fractions[0] if fractions else 0.0
        brightness = min(100, max(4, 4 + rms * gain * 4500 * bass_frac * 3))
        return ("hsv", ctx["monochrome_hue"], 100, brightness)

    if mode == "mirror_mode":
        # Hue mirrors around a fixed center point as the treble/bass
        # balance shifts (treble-heavy swings one way, bass-heavy swings
        # the other, both by the same amount off MIRROR_CENTER_HUE) while
        # brightness breathes independently -- a literal "breathing color"
        # combination of the two ideas in the roadmap description.
        f = fractions if len(fractions) == 3 else _resample_to_3(bands)
        balance = f[2] - f[0]  # treble frac minus bass frac, in [-1, 1]
        target = (MIRROR_CENTER_HUE + balance * MIRROR_SWING_DEG) % 360
        ctx["smoothed_hue"] = _smooth_hue(ctx["smoothed_hue"], target, 0.3)
        period_s = 3.0
        breathing = 30 + 20 * (0.5 + 0.5 * math.sin(2 * math.pi * (now % period_s) / period_s))
        active_boost = max(0.0, min(50.0, rms * gain * 3000))
        brightness = min(100, breathing + active_boost)
        return ("hsv", ctx["smoothed_hue"], 100, brightness)

    if mode == "random_walk_hue":
        # A bounded random walk instead of auto_rotate_hue's fixed-rate
        # rotation -- each frame's step is a small, capped random nudge
        # (never more than RANDOM_WALK_MAX_STEP_DEG regardless of how loud
        # the input is) so the motion feels organic rather than mechanical,
        # while still wrapping the full 0-360 circle like every other hue
        # here. Reuses ctx["rotate_hue"] as its running position -- the
        # same "current rotating hue" slot auto_rotate_hue uses, since the
        # two modes are never active in the same ctx simultaneously.
        step = random.uniform(-RANDOM_WALK_MAX_STEP_DEG, RANDOM_WALK_MAX_STEP_DEG)
        ctx["rotate_hue"] = (ctx["rotate_hue"] + step) % 360
        return ("hsv", ctx["rotate_hue"], 100, pulse_brightness)

    if mode == "silence_flash_recover":
        # Tracks how long the input has been near-silent; the instant real
        # audio resumes after a *long* silence, fire exactly one bright
        # white flash before falling back into normal band_fixed-style
        # reactive color. A short pause (shorter than the threshold) never
        # flashes -- this is specifically about "audio came back after a
        # while", not every tiny gap between notes.
        silence_since = ctx.get("silence_since")
        if rms < SILENCE_RMS_THRESHOLD:
            if silence_since is None:
                ctx["silence_since"] = now
            return ("hsv", ctx["monochrome_hue"], 60, 4)

        was_long_silence = (
            silence_since is not None
            and (now - silence_since) > SILENCE_FLASH_LONG_THRESHOLD_S
        )
        ctx["silence_since"] = None
        if was_long_silence:
            return ("hsv", 0, 0, 100)
        f = fractions if len(fractions) == 3 else _resample_to_3(bands)
        target = f[0] * BASS_HUE + f[1] * MID_HUE + f[2] * TREBLE_HUE
        ctx["smoothed_hue"] = _smooth_hue(ctx["smoothed_hue"], target, 0.4)
        return ("hsv", ctx["smoothed_hue"], 100, pulse_brightness)

    if mode == "crescendo_ramp":
        # Detects a *sustained rising trend* in rms (not just "it's loud
        # right now" -- that's strobe_on_drop's job) by comparing the
        # average of the first half vs. the second half of a rolling
        # window, then ramps brightness/saturation up ahead of the
        # anticipated peak. A steady loud signal with no rise produces
        # ramp ~= 0, same as silence -- only a genuine build triggers it.
        window = ctx.setdefault("crescendo_window", collections.deque(maxlen=CRESCENDO_WINDOW_FRAMES))
        window.append(rms)
        ramp = 0.0
        if len(window) >= CRESCENDO_WINDOW_FRAMES:
            half = CRESCENDO_WINDOW_FRAMES // 2
            snapshot = list(window)
            first_avg = sum(snapshot[:half]) / half
            second_avg = sum(snapshot[half:]) / (len(snapshot) - half)
            if first_avg > 1e-9:
                rising_ratio = max(0.0, (second_avg - first_avg) / first_avg)
                ramp = min(1.0, rising_ratio * CRESCENDO_SENSITIVITY)
        ctx["crescendo_ramp_level"] = ramp
        sat = min(100, 55 + ramp * 45)
        brightness = min(100, base_brightness + ramp * 35)
        return ("hsv", ctx["monochrome_hue"], sat, brightness)

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


# ------------------------------------------------------------- tempo/BPM ---
BEAT_SENSITIVITY_PRESETS = {
    # Multiplier `k` in `adaptive_threshold = mean(onset) + k*std(onset)`.
    # Lower k pulls the threshold closer to the noise floor, so more onsets
    # clear it -- that's "Aggressive". Higher k demands a sharper spike --
    # "Subtle".
    "subtle": 2.6,
    "normal": 1.8,
    "aggressive": 1.1,
}
DEFAULT_BEAT_SENSITIVITY = "normal"

TEMPO_MIN_BPM = 60.0
TEMPO_MAX_BPM = 200.0
TEMPO_HISTORY_SECONDS = 8.0
# Must exceed the max-lag frame count (period of TEMPO_MIN_BPM / frame_dt)
# so the very first autocorrelation attempt already has a full lag range
# to search, rather than silently truncating it.
TEMPO_MIN_HISTORY_FRAMES = 120
TEMPO_SMOOTHING_ALPHA = 0.25  # EMA applied across updates so BPM doesn't jump frame to frame
OCTAVE_BIAS_FRACTION = 0.6  # see _estimate_bpm -- prefers the fundamental lag over a 2x/3x alias
SILENCE_RESET_SECONDS = 4.0  # continuous near-silence longer than this drops the tempo estimate
TAP_TEMPO_MAX_GAP_S = 2.0     # a gap longer than this between taps starts a fresh tap sequence
TAP_TEMPO_MAX_TAPS = 8


# ------------------------------------------------------ genre/mood presets --
# Each bundles mode + sensitivity (gain) + dwell + band count + a
# monochrome-hue anchor (used by mono-hue modes, ignored otherwise) +
# beat-sensitivity preset + a palette subset (PRESET_COLORS ids) under one
# name, so a whole audio-reactive "feel" can be applied in a single call.
AUDIO_GENRE_PRESETS = [
    {
        "id": "edm_party", "name": "EDM / Party",
        "mode": "palette_cycle", "sensitivity": 1.8, "min_dwell_ms": 45, "n_bands": 3,
        "monochrome_hue": 300.0, "beat_sensitivity": "aggressive",
        "palette": ["magenta", "cyan", "hot_pink", "violet", "lime"],
        "description": "Fast dwell, high-contrast palette cycling, aggressive beat threshold for big-room energy.",
    },
    {
        "id": "chill_ambient", "name": "Chill / Ambient",
        "mode": "breathing_silence", "sensitivity": 0.6, "min_dwell_ms": 250, "n_bands": 3,
        "monochrome_hue": 200.0, "beat_sensitivity": "subtle",
        "palette": ["sky", "lavender", "teal", "turquoise"],
        "description": "Slow dwell, narrow hue range, breathing baseline for quiet/ambient listening.",
    },
    {
        "id": "rock_live", "name": "Rock / Live",
        "mode": "band_fixed", "sensitivity": 1.3, "min_dwell_ms": 90, "n_bands": 3,
        "monochrome_hue": 10.0, "beat_sensitivity": "normal",
        "palette": ["red", "orange", "gold"],
        "description": "Bass-forward warm palette using the fixed 3-band mapping.",
    },
    {
        "id": "classical_acoustic", "name": "Classical / Acoustic",
        "mode": "vu_meter", "sensitivity": 0.8, "min_dwell_ms": 200, "n_bands": 3,
        "monochrome_hue": 45.0, "beat_sensitivity": "subtle",
        "palette": ["white_warm", "gold"],
        "description": "Brightness-only, minimal hue movement, gentle dwell for dynamic acoustic material.",
    },
    {
        "id": "hip_hop_bass_heavy", "name": "Hip-Hop / Bass-Heavy",
        "mode": "bass_only_pulse", "sensitivity": 1.6, "min_dwell_ms": 70, "n_bands": 3,
        "monochrome_hue": 15.0, "beat_sensitivity": "normal",
        "palette": ["red", "coral", "gold"],
        "description": "Deep warm hue, brightness pulses purely off the bass band.",
    },
    {
        "id": "jazz_improv", "name": "Jazz / Improv",
        "mode": "dominant_band", "sensitivity": 1.0, "min_dwell_ms": 140, "n_bands": 3,
        "monochrome_hue": 40.0, "beat_sensitivity": "subtle",
        "palette": ["amber", "teal", "violet"],
        "description": "Wider dynamic tolerance and slower smoothing for improvised, less-metronomic material.",
    },
    {
        "id": "lofi_study", "name": "Lo-fi / Study",
        "mode": "breathing_silence", "sensitivity": 0.5, "min_dwell_ms": 300, "n_bands": 3,
        "monochrome_hue": 220.0, "beat_sensitivity": "subtle",
        "palette": ["lavender", "white_cool", "sky"],
        "description": "Very slow, desaturated breathing baseline for background study/focus listening.",
    },
    {
        "id": "metal_hardcore", "name": "Metal / Hardcore",
        "mode": "strobe_on_drop", "sensitivity": 2.2, "min_dwell_ms": 40, "n_bands": 3,
        "monochrome_hue": 0.0, "beat_sensitivity": "aggressive",
        "palette": ["red", "white_cool"],
        "description": "High-contrast strobe-on-drop, fast dwell, aggressive threshold for hard/fast material.",
    },
]

BPM_PRESET_SUGGESTIONS = [
    # (min_bpm inclusive, max_bpm exclusive, preset_id) -- a dismissible
    # heuristic starting point, not a musicological claim (real genres
    # overlap heavily in BPM).
    (0.0, 70.0, "lofi_study"),
    (70.0, 90.0, "chill_ambient"),
    (90.0, 105.0, "classical_acoustic"),
    (105.0, 120.0, "jazz_improv"),
    (120.0, 135.0, "hip_hop_bass_heavy"),
    (135.0, 150.0, "rock_live"),
    (150.0, 175.0, "edm_party"),
    (175.0, 10_000.0, "metal_hardcore"),
]


def find_genre_preset(preset_id):
    return next((p for p in AUDIO_GENRE_PRESETS if p["id"] == preset_id), None)


def _slugify_preset_name(name):
    slug = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
    return slug or "custom_preset"


def build_custom_preset(name, mode, sensitivity=1.0, min_dwell_ms=DEFAULT_MIN_DWELL_MS, n_bands=3,
                         monochrome_hue=280.0, beat_sensitivity=DEFAULT_BEAT_SENSITIVITY,
                         palette=None, description="", preset_id=None):
    """Validates and builds a user-savable custom audio preset bundle, in
    the same shape as an AUDIO_GENRE_PRESETS entry (mode + sensitivity +
    dwell + n_bands + monochrome_hue + beat_sensitivity + palette under a
    name). Pure/side-effect-free -- the caller (the `/api/audio/presets/
    custom` route) owns actually persisting the returned dict. Raises
    ValueError with a human-readable message on any invalid field, which
    callers turn into a 400."""
    if not name or not name.strip():
        raise ValueError("preset name is required")
    if mode not in MODES:
        raise ValueError(f"unknown mode '{mode}', expected one of {MODES}")
    if beat_sensitivity not in BEAT_SENSITIVITY_PRESETS:
        raise ValueError(f"unknown beat_sensitivity '{beat_sensitivity}', expected one of {list(BEAT_SENSITIVITY_PRESETS)}")
    if min_dwell_ms < MIN_DWELL_FLOOR_MS:
        raise ValueError(f"min_dwell_ms below the safety floor of {MIN_DWELL_FLOOR_MS}ms")
    palette = list(palette or [])
    valid_ids = {p["id"] for p in PRESET_COLORS}
    unknown = [pid for pid in palette if pid not in valid_ids]
    if unknown:
        raise ValueError(f"unknown palette color id(s): {unknown}")
    return {
        "id": preset_id or _slugify_preset_name(name),
        "name": name.strip(),
        "mode": mode,
        "sensitivity": max(0.1, min(5.0, float(sensitivity))),
        "min_dwell_ms": int(min_dwell_ms),
        "n_bands": max(3, min(16, int(n_bands))),
        "monochrome_hue": float(monochrome_hue) % 360,
        "beat_sensitivity": beat_sensitivity,
        "palette": palette,
        "description": description,
        "custom": True,
    }


def suggest_preset_for_bpm(bpm):
    if bpm is None:
        return None
    for lo, hi, preset_id in BPM_PRESET_SUGGESTIONS:
        if lo <= bpm < hi:
            return preset_id
    return None


class TempoTracker:
    """Real BPM estimation via autocorrelation of an onset-strength signal
    (a spectral-flux-style positive-only frame-to-frame rise in rms), plus
    a beat-confidence score, an adaptive beat-sensitivity threshold, manual
    tap-tempo fallback, and silence-aware reset so a tempo estimate never
    survives a long pause.

    One instance lives per AudioSession/GroupAudioSession -- tempo is a
    property of the audio stream itself, not of any individual bulb, so a
    group session shares one tracker the same way it shares one
    `analyze_frame` call per callback.
    """

    def __init__(self, frame_dt=None, beat_sensitivity=DEFAULT_BEAT_SENSITIVITY):
        self.frame_dt = frame_dt or (BLOCK_SIZE / SAMPLE_RATE)
        maxlen = max(TEMPO_MIN_HISTORY_FRAMES, int(TEMPO_HISTORY_SECONDS / self.frame_dt))
        self.onset_history = collections.deque(maxlen=maxlen)
        self._prev_energy = 0.0
        self.bpm = None
        self.confidence = 0.0
        self.last_onset = 0.0
        self.is_beat = False
        self.beat_sensitivity = (
            beat_sensitivity if beat_sensitivity in BEAT_SENSITIVITY_PRESETS else DEFAULT_BEAT_SENSITIVITY
        )
        self._silence_frames = 0
        self._silence_reset_frames = max(1, int(SILENCE_RESET_SECONDS / self.frame_dt))
        self.tap_times = []
        self.tap_bpm = None

    def set_sensitivity(self, preset):
        if preset in BEAT_SENSITIVITY_PRESETS:
            self.beat_sensitivity = preset
            return True
        return False

    @property
    def adaptive_threshold(self):
        if len(self.onset_history) < 2:
            return 0.0
        arr = np.array(self.onset_history)
        k = BEAT_SENSITIVITY_PRESETS[self.beat_sensitivity]
        return float(arr.mean() + k * arr.std())

    def update(self, rms):
        """Feed one frame's rms value. Call exactly once per audio callback
        (same cadence as `_apply_mode`)."""
        onset = max(0.0, rms - self._prev_energy)
        self._prev_energy = rms
        self.onset_history.append(onset)
        self.last_onset = onset
        self.is_beat = onset > 0.0 and onset > self.adaptive_threshold

        if rms < SILENCE_RMS_THRESHOLD:
            self._silence_frames += 1
        else:
            self._silence_frames = 0

        if self._silence_frames > self._silence_reset_frames:
            # A tempo carried over from before a long pause is more likely
            # to mislead than help (song ended, long silent intro, etc.),
            # so drop it rather than let it go stale silently.
            self.bpm = None
            self.confidence = 0.0
            self.onset_history.clear()
            return

        if len(self.onset_history) < TEMPO_MIN_HISTORY_FRAMES:
            return

        raw_bpm, confidence = self._estimate_bpm()
        self.confidence = confidence
        if raw_bpm is None:
            return
        if self.bpm is None:
            self.bpm = raw_bpm
        else:
            self.bpm = self.bpm * (1 - TEMPO_SMOOTHING_ALPHA) + raw_bpm * TEMPO_SMOOTHING_ALPHA

    def _estimate_bpm(self):
        arr = np.array(self.onset_history, dtype=float)
        arr = arr - arr.mean()
        energy = float(np.dot(arr, arr))
        if energy <= 1e-12:
            return None, 0.0
        n = len(arr)
        corr = np.correlate(arr, arr, mode="full")
        corr = corr[n - 1:]  # keep lag >= 0 only (length n, index i == lag i)
        min_lag = max(1, int(60.0 / TEMPO_MAX_BPM / self.frame_dt))
        max_lag = min(len(corr) - 1, int(60.0 / TEMPO_MIN_BPM / self.frame_dt))
        if min_lag >= max_lag:
            return None, 0.0
        segment = corr[min_lag:max_lag + 1]
        global_offset = int(np.argmax(segment))
        global_val = float(segment[global_offset])
        if global_val <= 0 or corr[0] <= 0:
            return None, 0.0
        # Octave-bias correction: a periodic pulse train's autocorrelation
        # peaks at every integer multiple of the true period (T, 2T, 3T,
        # ...), and discretization noise can make one of those *aliases*
        # (usually 2T) score marginally higher than the true fundamental
        # at T -- a well-known "half-tempo" failure mode for naive
        # autocorrelation beat tracking. Mitigate it the standard way:
        # scan from the smallest (fastest/highest-BPM) lag upward and take
        # the first one that's already within OCTAVE_BIAS_FRACTION of the
        # global peak, rather than blindly taking whichever lag happens to
        # score highest. This doesn't attempt full half/double-time
        # detection (that's its own separate, UI-facing feature) -- it
        # just prefers the fundamental over its alias when both are
        # clearly strong peaks.
        threshold = OCTAVE_BIAS_FRACTION * global_val
        peak_offset = global_offset
        for i, val in enumerate(segment):
            if val >= threshold:
                peak_offset = i
                break
        peak_lag = min_lag + peak_offset
        peak_val = float(segment[peak_offset])
        confidence = max(0.0, min(1.0, global_val / corr[0]))
        bpm = 60.0 / (peak_lag * self.frame_dt)
        return bpm, confidence

    def tap(self, timestamp=None):
        """Manual tap-tempo fallback/override. Pass `timestamp` explicitly
        for deterministic testing; defaults to wall-clock time in
        production use."""
        now = timestamp if timestamp is not None else time.time()
        if self.tap_times and (now - self.tap_times[-1]) > TAP_TEMPO_MAX_GAP_S:
            self.tap_times = []  # gap too long -- start a fresh tap sequence
        self.tap_times.append(now)
        self.tap_times = self.tap_times[-TAP_TEMPO_MAX_TAPS:]
        if len(self.tap_times) >= 2:
            intervals = [b - a for a, b in zip(self.tap_times, self.tap_times[1:]) if b > a]
            if intervals:
                median_interval = statistics.median(intervals)
                self.tap_bpm = 60.0 / median_interval if median_interval > 0 else None
        return self.tap_bpm

    def reset_tap(self):
        self.tap_times = []
        self.tap_bpm = None

    def status(self):
        return {
            "bpm": round(self.bpm, 1) if self.bpm is not None else None,
            "confidence": round(self.confidence, 3),
            "beat_sensitivity": self.beat_sensitivity,
            "adaptive_threshold": round(self.adaptive_threshold, 6),
            "is_beat": self.is_beat,
            "tap_bpm": round(self.tap_bpm, 1) if self.tap_bpm is not None else None,
            "suggested_preset": suggest_preset_for_bpm(self.bpm),
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
                 beat_sensitivity=DEFAULT_BEAT_SENSITIVITY,
                 device_key=None, agc_enabled=False, noise_gate_enabled=True,
                 dc_removal_enabled=True, noise_gate_floor=None, agc_target_rms=None,
                 agc_attack_ms=None, agc_release_ms=None, band_gains=None,
                 use_saved_calibration=True,
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
        self.tempo = TempoTracker(beat_sensitivity=beat_sensitivity)

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
            "bands_full": self.ctx.get(
                "latest_full_bands", {"n_bands": self.n_bands, "fractions": [], "energies": []}),
            "signal": self.conditioner.last_meter or {},
            "sender": self.sender.status(),
            "tempo": self.tempo.status(),
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
        self.tempo.update(rms_check)

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
                 beat_sensitivity=DEFAULT_BEAT_SENSITIVITY,
                 hue_offsets=None, brightness_scales=None, band_assignments=None,
                 mirror_center_hue=0.0, wave_period_ticks=40,
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
        self.tempo = TempoTracker(beat_sensitivity=beat_sensitivity)
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
                "paused": i in self._paused_bulbs and now < self._paused_bulbs[i],
            })
        return {
            "active": self.is_alive(),
            "mode": self.mode,
            "role_mode": self.role_mode,
            "bulb_count": n,
            "bulbs": bulbs,
            "tempo": self.tempo.status(),
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
        self.tempo.update(rms_check)

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
                _finalize_and_queue(i, action)
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
            _finalize_and_queue(i, action)

        self._wave_tick += 1


# ------------------------------------------------------------ sessions ---
_sessions = {}          # single-bulb sessions, keyed by device id
_group_sessions = {}    # group sessions, keyed by group id
_sessions_lock = threading.Lock()
_last_capture = {}      # device_id -> captured lightshow points, kept after stop() for export

_rate_limit_hits = {}   # rate-limit key -> deque of call timestamps
_rate_limit_lock = threading.Lock()


def start_session(controller, device_index, mode="band_fixed", sensitivity=1.0,
                   monochrome_hue=280.0, n_bands=3, min_dwell_ms=DEFAULT_MIN_DWELL_MS,
                   beat_sensitivity=DEFAULT_BEAT_SENSITIVITY,
                   device_key=None, agc_enabled=False, noise_gate_enabled=True,
                   dc_removal_enabled=True, noise_gate_floor=None, agc_target_rms=None,
                   agc_attack_ms=None, agc_release_ms=None, band_gains=None,
                   use_saved_calibration=True,
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
                                beat_sensitivity=beat_sensitivity,
                                device_key=device_key, agc_enabled=agc_enabled,
                                noise_gate_enabled=noise_gate_enabled, dc_removal_enabled=dc_removal_enabled,
                                noise_gate_floor=noise_gate_floor, agc_target_rms=agc_target_rms,
                                agc_attack_ms=agc_attack_ms, agc_release_ms=agc_release_ms,
                                band_gains=band_gains, use_saved_calibration=use_saved_calibration,
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


def tap_session_tempo(device_id, timestamp=None):
    with _sessions_lock:
        session = _sessions.get(device_id)
    if not session:
        return None
    return session.tempo.tap(timestamp)


def set_session_beat_sensitivity(device_id, preset):
    with _sessions_lock:
        session = _sessions.get(device_id)
    if not session:
        return False
    return session.tempo.set_sensitivity(preset)


def get_active_session(device_id):
    """Public accessor for the live AudioSession object (if any), so
    callers like the lightshow-export route don't need to reach into the
    module-private `_sessions` dict directly."""
    with _sessions_lock:
        session = _sessions.get(device_id)
    return session if session and session.is_alive() else None


def start_group_session(group_id, controllers, device_index, mode="band_fixed", role_mode="unison",
                         sensitivity=1.0, monochrome_hue=280.0, min_dwell_ms=DEFAULT_MIN_DWELL_MS,
                         beat_sensitivity=DEFAULT_BEAT_SENSITIVITY,
                         hue_offsets=None, brightness_scales=None, band_assignments=None,
                         mirror_center_hue=0.0, wave_period_ticks=40,
                         max_duration_s=None, warmup_s=0.0, max_flash_rate_hz=None,
                         disable_flash_heavy=False, silence_auto_off=True, fallback_device_index=None):
    validate_start_config(n_bands=3, min_dwell_ms=min_dwell_ms, max_duration_s=max_duration_s,
                          warmup_s=warmup_s, mode=mode, disable_flash_heavy=disable_flash_heavy)
    with _sessions_lock:
        existing = _group_sessions.get(group_id)
        if existing and existing.is_alive():
            existing.stop()
        session = GroupAudioSession(controllers, device_index, mode, role_mode, sensitivity, monochrome_hue,
                                     min_dwell_ms, beat_sensitivity=beat_sensitivity,
                                     hue_offsets=hue_offsets, brightness_scales=brightness_scales,
                                     band_assignments=band_assignments,
                                     mirror_center_hue=mirror_center_hue, wave_period_ticks=wave_period_ticks,
                                     max_duration_s=max_duration_s, warmup_s=warmup_s,
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


def tap_group_tempo(group_id, timestamp=None):
    with _sessions_lock:
        session = _group_sessions.get(group_id)
    if not session:
        return None
    return session.tempo.tap(timestamp)


def set_group_beat_sensitivity(group_id, preset):
    with _sessions_lock:
        session = _group_sessions.get(group_id)
    if not session:
        return False
    return session.tempo.set_sensitivity(preset)


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
