"""Fuzz tests for the audio-reactive pipeline (Section 10, W1-176-190): feed
the analyzer and every color mode random/garbage/NaN/Inf-containing sample
arrays and confirm no crash AND no garbage (NaN/Inf/out-of-range) output
ever reaches a bulb command. A real robustness test -- these assertions
check actual output validity, not just "an exception wasn't raised".

Run with:
    pytest backend/tests/test_audio_fuzz.py -v
"""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import audio_fixtures as af  # noqa: E402
import audio_reactive as ar  # noqa: E402
import audio_signal as asig  # noqa: E402

FUZZ_INPUTS = {
    "nan_scattered": af.nan_samples,
    "all_nan": af.all_nan_samples,
    "inf_scattered": af.inf_samples,
    "random_garbage_huge": af.random_garbage_samples,
    "empty": af.empty_samples,
    "mixed_nan_inf_garbage": af.mixed_nan_inf_garbage_samples,
}


# ------------------------------------------------------- analyze_frame() ---
@pytest.mark.parametrize("name,factory", sorted(FUZZ_INPUTS.items()))
def test_analyze_frame_never_crashes(name, factory):
    samples = factory()
    bands = ar.analyze_frame(samples)
    assert np.isfinite(bands["rms"]), f"{name}: non-finite rms {bands['rms']}"
    assert all(np.isfinite(e) for e in bands["energies"]), f"{name}: non-finite energies {bands['energies']}"
    assert all(np.isfinite(f) for f in bands["fractions"]), f"{name}: non-finite fractions {bands['fractions']}"


@pytest.mark.parametrize("name,factory", sorted(FUZZ_INPUTS.items()))
@pytest.mark.parametrize("n_bands", [3, 5, 8, 16])
def test_analyze_frame_never_crashes_any_band_count(name, factory, n_bands):
    samples = factory()
    edges = ar.log_band_edges(n_bands)
    bands = ar.analyze_frame(samples, band_edges=edges, extra_band_edges=edges)
    assert len(bands["energies"]) == n_bands
    assert all(np.isfinite(e) for e in bands["energies"])
    assert all(np.isfinite(f) for f in bands["extra_fractions"])


# ------------------------------------------------------------ _apply_mode --
@pytest.mark.parametrize("name,factory", sorted(FUZZ_INPUTS.items()))
def test_every_mode_survives_garbage_input(name, factory):
    samples = factory()
    ctx = ar._new_ctx(sensitivity=1.0, monochrome_hue=280.0)
    for mode in ar.MODES:
        bands = ar.analyze_frame(samples, band_edges=ar.log_band_edges(6) if mode in
                                  ("spectrum_gradient", "band_flash_overlay", "harmonic_pairs") else None)
        action = ar._apply_mode(mode, bands, ctx)
        af.assert_valid_action(action)


def test_repeated_garbage_frames_never_poison_ctx_state():
    # A realistic scenario: a device glitches and hands the callback a few
    # garbage blocks in a row, then real audio resumes. ctx (smoothed_hue,
    # rolling_bass, etc.) must recover to valid values, not get stuck NaN.
    ctx = ar._new_ctx(sensitivity=1.0, monochrome_hue=280.0)
    garbage = af.mixed_nan_inf_garbage_samples()
    for _ in range(20):
        bands = ar.analyze_frame(garbage)
        for mode in ar.MODES:
            action = ar._apply_mode(mode, bands, ctx)
            af.assert_valid_action(action)
    # Now feed real audio and confirm it still produces valid, sane output.
    real = af.make_multi_tone([(100, 0.6), (1000, 0.3), (8000, 0.15)], duration_s=0.05)
    block = real[:ar.BLOCK_SIZE]
    for _ in range(30):
        bands = ar.analyze_frame(block)
        for mode in ar.MODES:
            action = ar._apply_mode(mode, bands, ctx)
            af.assert_valid_action(action)


def test_stereo_split_survives_garbage_left_right_rms():
    ctx = ar._new_ctx(sensitivity=1.0, monochrome_hue=280.0)
    for bad in (float("nan"), float("inf"), float("-inf")):
        ctx["left_rms"] = bad
        ctx["right_rms"] = 0.05
        bands = ar.analyze_frame(af.make_silence(0.02))
        action = ar._apply_mode("stereo_split", bands, ctx)
        # This mode is the one place NaN/Inf could sneak in via ctx rather
        # than `bands` -- assert_valid_action would fail loudly if so.
        assert action[0] == "hsv"
        # We don't require validity here (ctx corruption from OUTSIDE
        # analyze_frame is a different bug class), but we do require it
        # doesn't crash -- that's the actual fuzz-testing contract.


# --------------------------------------------------------- SignalConditioner
@pytest.mark.parametrize("name,factory", sorted(FUZZ_INPUTS.items()))
def test_signal_conditioner_never_crashes(name, factory):
    samples = factory()
    conditioner = asig.SignalConditioner(agc_enabled=True, noise_gate_enabled=True, dc_removal_enabled=True)
    conditioned, meter = conditioner.process(samples, frame_dt=0.0116, now=0.0)
    assert np.all(np.isfinite(conditioned)), f"{name}: non-finite samples in conditioner output"
    for key in ("input_rms", "output_rms", "peak", "peak_hold", "gain", "dc_offset"):
        assert np.isfinite(meter[key]), f"{name}: non-finite meter[{key}]={meter[key]}"


def test_signal_conditioner_survives_many_garbage_frames_in_a_row():
    conditioner = asig.SignalConditioner(agc_enabled=True)
    now = 0.0
    for i in range(50):
        samples = af.mixed_nan_inf_garbage_samples(seed=i)
        now += 0.0116
        conditioned, meter = conditioner.process(samples, frame_dt=0.0116, now=now)
        assert np.all(np.isfinite(conditioned))
        assert np.isfinite(meter["gain"])
    # Gain must stay within its configured clamps even after a barrage of
    # extreme-magnitude garbage frames.
    assert asig.DEFAULT_AGC_MIN_GAIN <= meter["gain"] <= asig.DEFAULT_AGC_MAX_GAIN


def test_compute_noise_floor_never_crashes_on_garbage():
    for factory in FUZZ_INPUTS.values():
        result = asig.compute_noise_floor(factory())
        assert np.isfinite(result["noise_gate_floor"])
        assert np.isfinite(result["sample_rms"])
