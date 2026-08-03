"""Golden-value regression tests (Section 10, W1-176-190): lock down each
EXISTING audio-reactive mode's exact hue/brightness output for a fixed
synthetic input, so a silent change to any mode's math gets caught even
though nothing about its public behavior (still a valid hsv/rgb action)
changed.

Golden values below were captured by actually running
`audio_fixtures.converge_mode()` against the fixed multi-tone input defined
here (see the module docstring in each block for how). If you deliberately
change a mode's formula, regenerate the expected values the same way and
update them here -- don't just loosen the tolerance.

Run with:
    pytest backend/tests/test_audio_golden.py -v
"""
import os
import random
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import audio_fixtures as af  # noqa: E402
import audio_reactive as ar  # noqa: E402

TOL = 1e-3  # tight but not brittle-to-the-last-float-bit across platforms

# Fixed synthetic input: a bass tone (100Hz) louder than a mid tone (1kHz)
# louder than a treble tone (8kHz) -- deliberately bass-dominant so
# dominant_band/band_fixed/etc. have an unambiguous "loudest band" to lock
# down, while still exercising all three legacy bands.
MULTI_TONE = af.make_multi_tone([(100, 0.6), (1000, 0.3), (8000, 0.15)], duration_s=0.05)
CONVERGE_FRAMES = 150  # matches test_audio_modes.py's convention


def _run(mode, n_bands=3):
    action, _ = af.converge_mode(mode, MULTI_TONE, n_frames=CONVERGE_FRAMES, n_bands=n_bands)
    return action


# mode -> (n_bands, expected action tuple)
GOLDEN_3BAND_MODES = {
    "band_fixed": ("hsv", 81.9393, 100, 100),
    "dominant_band": ("hsv", 10.0, 100, 100),
    "weighted_blend": ("hsv", 72.8098, 100, 100),
    "vu_meter": ("hsv", 280.0, 100, 100),
    "auto_rotate_hue": ("hsv", 180.0, 100, 100),
    "monochrome_pulse": ("hsv", 280.0, 100, 100),
    "strobe_on_drop": ("hsv", 10.0, 100, 40.0),
    "palette_cycle": ("rgb_brightness", 255, 0, 0, 100),
    "kick_snare_split": ("hsv", 100.5715, 100, 100),
    # Added when Week 1 Phase A's new modes were merged alongside this
    # phase's golden-test harness -- captured by actually running each mode
    # against the same MULTI_TONE fixture, same as every value above.
    "energy_contour": ("hsv", 280.0, 100.0, 100.0),
    "bass_only_pulse": ("hsv", 280.0, 100, 100),
    "crescendo_ramp": ("hsv", 280.0, 55.0, 100),
    "silence_flash_recover": ("hsv", 81.9393, 100, 100),
}

GOLDEN_6BAND_MODES = {
    "spectrum_gradient": ("hsv", 132.2162, 100, 100),
    "band_flash_overlay": ("hsv", 132.2162, 90, 50.0),
    "harmonic_pairs": ("hsv", 326.4163, 100, 100),
}


def _assert_golden(mode, expected, n_bands):
    action = _run(mode, n_bands=n_bands)
    assert action[0] == expected[0], f"action type {action[0]!r} != {expected[0]!r}"
    for i, (actual, exp) in enumerate(zip(action[1:], expected[1:]), start=1):
        assert actual == pytest.approx(exp, abs=TOL), (
            f"field {i}: {actual!r} != {exp!r} (tolerance {TOL})"
        )


# One test per band count rather than one per mode. A golden-value change is
# almost never limited to a single mode -- retuning a shared smoothing
# constant shifts most of them at once -- so the useful failure output is
# every drifted mode with its actual-vs-expected, in one report.
def test_golden_3band_mode(check_all):
    check_all(sorted(GOLDEN_3BAND_MODES.items()),
              lambda case: _assert_golden(case[0], case[1], n_bands=3),
              label="mode", name=lambda c: c[0])


def test_golden_6band_mode(check_all):
    check_all(sorted(GOLDEN_6BAND_MODES.items()),
              lambda case: _assert_golden(case[0], case[1], n_bands=6),
              label="mode", name=lambda c: c[0])


def test_golden_stereo_split():
    # stereo_split reads ctx["left_rms"]/["right_rms"] directly (set by
    # AudioSession._process from the real 2-channel capture) rather than
    # from `bands`, so its golden test sets them explicitly instead of
    # going through converge_mode.
    ctx = ar._new_ctx(1.0, 280.0)
    ctx["left_rms"] = 0.02
    ctx["right_rms"] = 0.08
    block = MULTI_TONE[:ar.BLOCK_SIZE]
    action = None
    for _ in range(CONVERGE_FRAMES):
        bands = ar.analyze_frame(block)
        action = ar._apply_mode("stereo_split", bands, ctx)
    assert action[0] == "hsv"
    assert action[1] == pytest.approx(344.0, abs=TOL)
    assert action[2] == pytest.approx(100, abs=TOL)
    assert action[3] == pytest.approx(100, abs=TOL)


def test_golden_breathing_silence(monkeypatch):
    # breathing_silence's brightness is a function of wall-clock time
    # (`now % period_s` drives its sine breathing), so its golden value
    # freezes time.time() rather than hardcoding a moving-target number.
    FIXED_TS = 1_000_000.0
    monkeypatch.setattr(ar.time, "time", lambda: FIXED_TS)
    silence = af.make_silence(0.05)
    action, _ = af.converge_mode("breathing_silence", silence, n_frames=CONVERGE_FRAMES, n_bands=3)
    assert action[0] == "hsv"
    assert action[1] == pytest.approx(22.5, abs=TOL)
    assert action[2] == pytest.approx(70, abs=TOL)
    assert action[3] == pytest.approx(16.0, abs=TOL)


def test_golden_random_walk_hue():
    # random_walk_hue's step is drawn from random.uniform() each frame, so
    # its golden value freezes the RNG with a fixed seed rather than
    # hardcoding a moving-target number the way breathing_silence freezes
    # time.time(). Any change to the step formula (not just the RNG calls
    # themselves) will still move this value, same guarantee as every
    # other golden test here.
    random.seed(42)
    action, _ = af.converge_mode("random_walk_hue", MULTI_TONE, n_frames=CONVERGE_FRAMES, n_bands=3)
    assert action[0] == "hsv"
    assert action[1] == pytest.approx(337.3053, abs=TOL)
    assert action[2] == pytest.approx(100, abs=TOL)
    assert action[3] == pytest.approx(100, abs=TOL)


def test_golden_mirror_mode(monkeypatch):
    # mirror_mode's brightness breathes via time.time() % period_s, the same
    # wall-clock dependency as breathing_silence -- freeze it the same way
    # rather than hardcoding a moving-target number.
    FIXED_TS = 1_000_000.0
    monkeypatch.setattr(ar.time, "time", lambda: FIXED_TS)
    action, _ = af.converge_mode("mirror_mode", MULTI_TONE, n_frames=CONVERGE_FRAMES, n_bands=3)
    assert action[0] == "hsv"
    assert action[1] == pytest.approx(240.0146, abs=TOL)
    assert action[2] == pytest.approx(100, abs=TOL)
    assert action[3] == pytest.approx(98.6603, abs=TOL)


def test_all_modes_covered_by_golden_tests():
    covered = (set(GOLDEN_3BAND_MODES) | set(GOLDEN_6BAND_MODES)
               | {"stereo_split", "breathing_silence", "random_walk_hue", "mirror_mode"})
    assert covered == set(ar.MODES), f"missing golden coverage for: {set(ar.MODES) - covered}"
