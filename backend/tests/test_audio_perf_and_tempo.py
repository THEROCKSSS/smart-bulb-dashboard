"""Regression tests for the audio hot-path caching and bass-band beat
detection (see docs/audio-modes.md).

The caching tests exist because the whole justification for that change was
"identical output, less work" -- a cache that quietly changed a value would
be worse than no cache at all, since every mode's color output depends on
these numbers.
"""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import audio_reactive as ar  # noqa: E402


def _reference_analyze(samples, n_bands):
    """The pre-cache implementation, recomputed inline. If the cached path
    ever diverges from this by even one ULP, the caching change broke
    something."""
    s = np.asarray(samples, dtype=np.float64)
    windowed = s * np.hanning(len(s))
    spectrum = np.abs(np.fft.rfft(windowed, n=ar.FFT_SIZE))
    freqs = np.fft.rfftfreq(ar.FFT_SIZE, 1.0 / ar.SAMPLE_RATE)
    edges = list(np.logspace(np.log10(20.0), np.log10(20000.0), n_bands + 1))
    energies = [ar._band_energy(spectrum, freqs, edges[i], edges[i + 1])
                for i in range(len(edges) - 1)]
    total = sum(energies) + 1e-9
    return energies, [e / total for e in energies]


@pytest.mark.parametrize("n_bands", [3, 5, 6, 8])
def test_cached_hot_path_is_bit_identical_to_the_original(n_bands):
    rs = np.random.RandomState(7)
    cases = [rs.rand(ar.BLOCK_SIZE) * 2 - 1 for _ in range(12)]
    cases += [np.zeros(ar.BLOCK_SIZE), np.ones(ar.BLOCK_SIZE)]
    for samples in cases:
        want_e, want_f = _reference_analyze(samples, n_bands)
        got = ar.analyze_frame(samples, n_bands=n_bands)
        # Exact equality, not approx: the caches must not perturb the maths.
        assert got["energies"] == want_e
        assert got["fractions"] == want_f


def test_a_caller_mutating_band_edges_cannot_poison_the_cache():
    """log_band_edges hands back a list; callers are free to modify it. If
    the cache handed out its own object, one such caller would silently
    corrupt every future session's band split."""
    reference = ar.log_band_edges(3)
    borrowed = ar.log_band_edges(3)
    borrowed[0] = -999.0
    assert ar.log_band_edges(3) == reference


def test_band_slices_reproduce_the_mask_they_replaced():
    """The slice bounds must match the old boolean mask exactly -- freqs >= lo
    (inclusive) and freqs < hi (exclusive)."""
    freqs = ar._rfft_freqs(ar.FFT_SIZE, ar.SAMPLE_RATE)
    edges = ar.log_band_edges(6)
    for (lo_i, hi_i), i in zip(ar._band_slices(freqs, edges), range(len(edges) - 1)):
        mask = (freqs >= edges[i]) & (freqs < edges[i + 1])
        assert np.array_equal(np.arange(len(freqs))[mask], np.arange(lo_i, hi_i))


# ------------------------------------------------------------ beat detection


def _beat_track(bpm, seconds=14.0, density=0, seed=0):
    """A kick at an exact tempo, optionally buried under a dense mix.
    `density=2` is the case that motivated bass-band onset detection."""
    sr = ar.SAMPLE_RATE
    n = int(sr * seconds)
    x = np.zeros(n)
    rs = np.random.RandomState(seed)
    period = sr * 60.0 / bpm
    for i in range(int(seconds * bpm / 60)):
        start = int(i * period)
        length = min(int(sr * 0.09), n - start)
        if length <= 0:
            break
        t = np.arange(length) / sr
        env = np.exp(-t * 38)
        x[start:start + length] += np.sin(2 * np.pi * 60 * t) * env + 0.35 * rs.randn(length) * env
    t = np.arange(n) / sr
    if density >= 1:
        x += 0.35 * np.sin(2 * np.pi * 220 * t) + 0.25 * np.sin(2 * np.pi * 330 * t)
    if density >= 2:
        x += 0.45 * np.sin(2 * np.pi * 440 * t) * (1 + 0.3 * np.sin(2 * np.pi * 0.7 * t))
        x += 0.30 * np.sin(2 * np.pi * 880 * t) + 0.12 * rs.randn(n)
    return np.clip(x, -1, 1)


def _detect(signal, use_bass):
    tracker = ar.TempoTracker()
    for i in range(0, len(signal) - ar.BLOCK_SIZE, ar.BLOCK_SIZE):
        bands = ar.analyze_frame(signal[i:i + ar.BLOCK_SIZE], n_bands=3)
        if use_bass:
            tracker.update(bands["rms"], bass_energy=bands["energies"][0])
        else:
            tracker.update(bands["rms"])
    return tracker.bpm


@pytest.mark.parametrize("bpm", [70, 100, 120, 128, 140, 174])
def test_bass_band_onset_tracks_tempo_through_a_dense_mix(bpm):
    """The reason bass_energy exists. Broadband rms sums the whole mix, so a
    loud sustained pad plus vocal-range content raises the floor the kick has
    to clear; measured, broadband drops to roughly 1-in-9 correct on this
    signal while the low band stays locked on."""
    got = _detect(_beat_track(bpm, density=2), use_bass=True)
    assert got is not None
    assert abs(got - bpm) <= 3, f"expected ~{bpm} BPM, got {got}"


def test_switching_onset_source_mid_session_does_not_fake_a_beat():
    """rms and band energy are on different scales. Diffing across a source
    change would manufacture one large bogus onset; update() rebases instead."""
    tracker = ar.TempoTracker()
    for _ in range(5):
        tracker.update(0.5)
    tracker.update(0.5, bass_energy=40.0)   # much larger scale
    assert tracker.last_onset == 0.0
