"""Reusable synthetic-audio test harness for the audio-reactive pipeline
(Section 10 of the Week 1 audio work: W1-176-190).

Every generator here produces plain numpy float64 arrays scaled to roughly
[-1, 1], matching what `sounddevice` hands the real `AudioSession._process`
callback (see backend/audio_reactive.py). Nothing in this module touches a
real audio device, a real bulb, or relies on wall-clock sleeps -- every
fixture is fully deterministic given its parameters/seed, which is what
makes the golden-value, fuzz, and latency tests built on top of it
reproducible.

Other test files (test_audio_golden.py, test_audio_fuzz.py,
test_audio_latency.py, test_audio_signal.py) import from this module rather
than re-implementing tone/noise generation or block-iteration helpers --
that reuse is the actual point of this file existing on its own.
"""
import os
import sys
import time

import numpy as np

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import audio_reactive as ar  # noqa: E402

SAMPLE_RATE = ar.SAMPLE_RATE
BLOCK_SIZE = ar.BLOCK_SIZE


# ------------------------------------------------------------- generators --
def make_tone(freq_hz, duration_s, sample_rate=SAMPLE_RATE, amplitude=0.5, phase=0.0):
    """A single pure sine tone."""
    n = int(duration_s * sample_rate)
    t = np.arange(n) / sample_rate
    return (amplitude * np.sin(2 * np.pi * freq_hz * t + phase)).astype(np.float64)


def make_multi_tone(freqs_amplitudes, duration_s, sample_rate=SAMPLE_RATE):
    """Sum of several (freq_hz, amplitude) sine tones -- e.g. a synthetic
    stand-in for "bass + mid + treble all present" without a real music
    file, so golden-value tests can target one band deliberately."""
    n = int(duration_s * sample_rate)
    t = np.arange(n) / sample_rate
    out = np.zeros(n, dtype=np.float64)
    for freq, amp in freqs_amplitudes:
        out += amp * np.sin(2 * np.pi * freq * t)
    return out


def make_silence(duration_s, sample_rate=SAMPLE_RATE):
    return np.zeros(int(duration_s * sample_rate), dtype=np.float64)


def duration_for_n_blocks(n_blocks, block_size=BLOCK_SIZE, sample_rate=SAMPLE_RATE):
    """Returns a duration_s that yields EXACTLY `n_blocks` full blocks with
    no short/zero-padded trailing block -- used by tests that check
    steady-state convergence (AGC gain, DC estimate, etc.) so the very last
    `iter_blocks()` chunk isn't a padded partial block with an artificially
    low RMS that would throw off the assertion."""
    return (n_blocks * block_size) / float(sample_rate)


def make_white_noise(duration_s, amplitude=0.1, sample_rate=SAMPLE_RATE, seed=0):
    rng = np.random.default_rng(seed)
    n = int(duration_s * sample_rate)
    return (rng.uniform(-1.0, 1.0, n) * amplitude).astype(np.float64)


def make_chirp(f0, f1, duration_s, sample_rate=SAMPLE_RATE, amplitude=0.5):
    """Linear chirp sweeping f0 -> f1 Hz over duration_s. Built by hand
    (no scipy in this project's requirements.txt) via the standard
    instantaneous-frequency phase integral:
        phase(t) = 2*pi*(f0*t + (f1-f0)*t^2/(2*duration_s))
    """
    n = int(duration_s * sample_rate)
    t = np.arange(n) / sample_rate
    k = (f1 - f0) / duration_s
    phase = 2 * np.pi * (f0 * t + 0.5 * k * t ** 2)
    return (amplitude * np.sin(phase)).astype(np.float64)


def make_noise_bursts(duration_s, burst_s, gap_s, amplitude=0.4, sample_rate=SAMPLE_RATE, seed=0):
    """Alternating on/off white-noise bursts -- a synthetic stand-in for
    percussive hits, used to exercise beat/transient detection without a
    real drum recording."""
    rng = np.random.default_rng(seed)
    n = int(duration_s * sample_rate)
    out = np.zeros(n, dtype=np.float64)
    period_n = max(1, int((burst_s + gap_s) * sample_rate))
    burst_n = max(1, int(burst_s * sample_rate))
    for start in range(0, n, period_n):
        end = min(n, start + burst_n)
        if start < end:
            out[start:end] = rng.uniform(-1.0, 1.0, end - start) * amplitude
    return out


def clip_signal(samples, ceiling=1.0):
    """Hard-clips a signal to +/-ceiling -- simulates ADC/source overload
    (e.g. a tone generated above 0dBFS then clipped, standing in for a real
    clipped microphone signal)."""
    return np.clip(samples, -ceiling, ceiling)


def add_dc_offset(samples, offset):
    return samples + offset


def make_stereo(left, right):
    n = min(len(left), len(right))
    return np.stack([left[:n], right[:n]], axis=1)


# ------------------------------------------------------------- fuzz inputs --
def nan_samples(n=BLOCK_SIZE):
    arr = np.zeros(n, dtype=np.float64)
    arr[::7] = np.nan
    return arr


def all_nan_samples(n=BLOCK_SIZE):
    return np.full(n, np.nan, dtype=np.float64)


def inf_samples(n=BLOCK_SIZE):
    arr = np.zeros(n, dtype=np.float64)
    arr[::11] = np.inf
    arr[5::11] = -np.inf
    return arr


def random_garbage_samples(n=BLOCK_SIZE, seed=0, scale=1e6):
    """Extreme-magnitude random noise -- not physically plausible audio,
    but exactly the kind of thing a corrupted buffer or a misbehaving
    driver could hand the callback."""
    rng = np.random.default_rng(seed)
    return rng.uniform(-scale, scale, n).astype(np.float64)


def empty_samples():
    return np.zeros(0, dtype=np.float64)


def mixed_nan_inf_garbage_samples(n=BLOCK_SIZE, seed=0):
    rng = np.random.default_rng(seed)
    arr = rng.uniform(-2.0, 2.0, n).astype(np.float64)
    arr[::13] = np.nan
    arr[::17] = np.inf
    arr[::19] = -np.inf
    return arr


# ------------------------------------------------------- block iteration ---
def iter_blocks(samples, block_size=BLOCK_SIZE, pad=True):
    """Yields fixed-size blocks the way sounddevice's InputStream callback
    delivers them. If `pad`, a short final block is zero-padded to
    block_size; otherwise short final blocks are yielded as-is (and a
    genuinely empty tail is skipped)."""
    n = len(samples)
    for start in range(0, n, block_size):
        block = samples[start:start + block_size]
        if len(block) < block_size:
            if not pad:
                if len(block) == 0:
                    continue
                yield block
                continue
            block = np.pad(block, (0, block_size - len(block)))
        yield block


def run_mode_over_samples(mode, samples, n_bands=3, sensitivity=1.0, monochrome_hue=280.0,
                           block_size=BLOCK_SIZE, ctx=None):
    """Feeds `samples` through analyze_frame() + _apply_mode() one block at
    a time -- the same per-callback shape `AudioSession._process` uses --
    and returns (final_action, ctx). Reuses a passed-in `ctx` so callers can
    keep converging smoothed state across multiple calls (e.g. a silence
    warm-up phase followed by a real signal)."""
    if ctx is None:
        ctx = ar._new_ctx(sensitivity, monochrome_hue)
    band_edges = ar.log_band_edges(n_bands) if n_bands != 3 else None
    action = None
    for block in iter_blocks(samples, block_size):
        bands = ar.analyze_frame(block, band_edges=band_edges)
        action = ar._apply_mode(mode, bands, ctx)
    return action, ctx


def converge_mode(mode, samples, n_frames=150, n_bands=3, sensitivity=1.0, monochrome_hue=280.0, ctx=None):
    """Repeats ONE block of `samples` (looping analyze_frame + _apply_mode)
    `n_frames` times so smoothed/rolling state (hue smoothing, the rolling-
    bass beat detector, etc.) settles -- mirrors CONVERGE_FRAMES in
    test_audio_modes.py, generalized for reuse."""
    if ctx is None:
        ctx = ar._new_ctx(sensitivity, monochrome_hue)
    band_edges = ar.log_band_edges(n_bands) if n_bands != 3 else None
    block = samples[:BLOCK_SIZE] if len(samples) >= BLOCK_SIZE else np.pad(samples, (0, BLOCK_SIZE - len(samples)))
    action = None
    for _ in range(n_frames):
        bands = ar.analyze_frame(block, band_edges=band_edges)
        action = ar._apply_mode(mode, bands, ctx)
    return action, ctx


# --------------------------------------------------------- latency harness --
def time_pipeline(samples, mode="band_fixed", n_bands=3, block_size=BLOCK_SIZE, repeats=1):
    """Real wall-clock timing harness: runs the ACTUAL analyze_frame() +
    _apply_mode() pipeline against synthetic input and returns a list of
    real per-block latencies in milliseconds (Section 10's
    "latency-measurement harness" -- genuine timings, not an estimate)."""
    band_edges = ar.log_band_edges(n_bands) if n_bands != 3 else None
    ctx = ar._new_ctx(1.0, 280.0)
    blocks = list(iter_blocks(samples, block_size))
    durations_ms = []
    for _ in range(repeats):
        for block in blocks:
            t0 = time.perf_counter()
            bands = ar.analyze_frame(block, band_edges=band_edges)
            ar._apply_mode(mode, bands, ctx)
            durations_ms.append((time.perf_counter() - t0) * 1000.0)
    return durations_ms


def assert_valid_hsv_action(action):
    assert action[0] == "hsv", f"expected an hsv action, got {action[0]!r}"
    _, hue, sat, brightness = action
    assert np.isfinite(hue) and 0 <= hue <= 360, f"hue out of range/non-finite: {hue}"
    assert np.isfinite(sat) and 0 <= sat <= 100, f"sat out of range/non-finite: {sat}"
    assert np.isfinite(brightness) and 0 <= brightness <= 100, f"brightness out of range/non-finite: {brightness}"


def assert_valid_action(action):
    """Like assert_valid_hsv_action but accepts either action shape
    ("hsv", ...) or ("rgb_brightness", ...), used by the fuzz tests which
    run every mode (including palette_cycle, which returns rgb_brightness)."""
    assert action[0] in ("hsv", "rgb_brightness"), f"unexpected action kind: {action[0]!r}"
    if action[0] == "hsv":
        assert_valid_hsv_action(action)
    else:
        _, r, g, b, brightness = action
        for name, v in (("r", r), ("g", g), ("b", b)):
            assert np.isfinite(v) and 0 <= v <= 255, f"{name} out of range/non-finite: {v}"
        assert np.isfinite(brightness) and 0 <= brightness <= 100, f"brightness out of range/non-finite: {brightness}"
