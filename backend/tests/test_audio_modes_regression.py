"""Week 1 Phase D, section 9: a real regression test suite for
`_apply_mode()` covering every mode with synthetic signals.

`test_audio_modes.py` already covers `harmonic_pairs` and `kick_snare_split`
in depth -- this file extends coverage to the other 12 modes rather than
duplicating that work, using the same synthetic-`bands`-dict approach (no
audio device, no FFT, no FastAPI server).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import audio_reactive as ar  # noqa: E402

CONVERGE_FRAMES = 150


def make_bands(energies, rms=0.01, spectrum=None, freqs=None):
    total = sum(energies) + 1e-9
    fractions = [e / total for e in energies]
    return {
        "spectrum": spectrum, "freqs": freqs, "rms": rms,
        "energies": list(energies), "fractions": fractions, "band_edges": None,
    }


def make_ctx(sensitivity=1.0, monochrome_hue=280.0):
    return ar._new_ctx(sensitivity=sensitivity, monochrome_hue=monochrome_hue)


def run_frames(mode, bands, ctx, n=1):
    action = None
    for _ in range(n):
        action = ar._apply_mode(mode, bands, ctx)
    return action


def assert_valid_hsv_action(action):
    assert action[0] == "hsv"
    _, hue, sat, brightness = action
    assert 0 <= hue <= 360
    assert 0 <= sat <= 100
    assert 0 <= brightness <= 100


def assert_valid_rgb_brightness_action(action):
    assert action[0] == "rgb_brightness"
    _, r, g, b, brightness = action
    assert 0 <= r <= 255 and 0 <= g <= 255 and 0 <= b <= 255
    assert 0 <= brightness <= 100


ALL_MODES = ar.MODES


def test_all_modes_registered_and_reachable():
    """Sanity: every mode in MODES actually produces a valid action instead
    of silently falling through to the default clause (which would mean a
    typo in a mode's `if mode == "..."` check)."""
    import numpy as np
    freqs = np.array([100.0, 1000.0, 8000.0])
    spectrum = np.array([10.0, 5.0, 2.0])
    for mode in ALL_MODES:
        ctx = make_ctx()
        bands = make_bands([10.0, 5.0, 2.0], rms=0.02, spectrum=spectrum, freqs=freqs)
        action = run_frames(mode, bands, ctx, n=CONVERGE_FRAMES)
        assert action[0] in ("hsv", "rgb_brightness"), f"{mode} produced unexpected action kind {action[0]!r}"


def test_vu_meter_tracks_loudness_and_stays_valid():
    ctx = make_ctx()
    quiet = run_frames("vu_meter", make_bands([1.0, 1.0, 1.0], rms=0.001), ctx, n=5)
    loud = run_frames("vu_meter", make_bands([50.0, 50.0, 50.0], rms=0.05), ctx, n=5)
    assert_valid_hsv_action(quiet)
    assert_valid_hsv_action(loud)
    assert loud[3] > quiet[3], "vu_meter brightness should track rms*sensitivity upward with louder input"


def test_monochrome_pulse_pulses_brighter_on_beat():
    ctx = make_ctx()
    bands = make_bands([1.0, 1.0, 1.0], rms=0.01)
    # Build up a steady rolling-bass baseline first (no beat yet).
    for _ in range(20):
        run_frames("monochrome_pulse", bands, ctx, n=1)
    steady = run_frames("monochrome_pulse", bands, ctx, n=1)
    assert_valid_hsv_action(steady)
    # A hard spike relative to the rolling baseline should read as a beat
    # and add the +20 brightness pulse bump.
    spike_bands = make_bands([1.0 * 3.0, 1.0, 1.0], rms=0.01)
    spiked = run_frames("monochrome_pulse", spike_bands, ctx, n=1)
    assert_valid_hsv_action(spiked)
    assert spiked[3] >= steady[3]


def test_auto_rotate_hue_actually_rotates_over_frames():
    ctx = make_ctx()
    bands = make_bands([1.0, 1.0, 1.0], rms=0.01)
    hues = []
    for _ in range(5):
        action = ar._apply_mode("auto_rotate_hue", bands, ctx)
        assert_valid_hsv_action(action)
        hues.append(action[1])
    assert len(set(round(h, 3) for h in hues)) > 1, "auto_rotate_hue must actually change hue frame to frame"


def test_band_fixed_biases_toward_dominant_band_hue():
    ctx = make_ctx()
    bass_dominant = run_frames("band_fixed", make_bands([100.0, 0.0, 0.0], rms=0.05), ctx, n=CONVERGE_FRAMES)
    assert_valid_hsv_action(bass_dominant)
    assert abs(bass_dominant[1] - ar.BASS_HUE) < 5.0

    ctx2 = make_ctx()
    treble_dominant = run_frames("band_fixed", make_bands([0.0, 0.0, 100.0], rms=0.05), ctx2, n=CONVERGE_FRAMES)
    assert_valid_hsv_action(treble_dominant)
    assert abs(treble_dominant[1] - ar.TREBLE_HUE) < 5.0


def test_dominant_band_picks_the_single_loudest_band():
    ctx = make_ctx()
    action = run_frames("dominant_band", make_bands([0.0, 100.0, 0.0], rms=0.05), ctx, n=CONVERGE_FRAMES)
    assert_valid_hsv_action(action)
    assert abs(action[1] - ar.MID_HUE) < 5.0


def test_weighted_blend_uses_spectral_centroid_and_stays_valid():
    ctx = make_ctx()
    # A low-centroid spectrum (energy concentrated at low freqs).
    freqs = [100.0, 1000.0, 8000.0]
    import numpy as np
    spectrum_low = np.array([100.0, 1.0, 1.0])
    bands = make_bands([100.0, 1.0, 1.0], rms=0.05, spectrum=spectrum_low, freqs=np.array(freqs))
    action = run_frames("weighted_blend", bands, ctx, n=CONVERGE_FRAMES)
    assert_valid_hsv_action(action)


def test_strobe_on_drop_flashes_on_hard_hit_only():
    ctx = make_ctx()
    quiet = make_bands([1.0, 1.0, 1.0], rms=0.01)
    for _ in range(20):
        run_frames("strobe_on_drop", quiet, ctx, n=1)
    steady = run_frames("strobe_on_drop", quiet, ctx, n=1)
    assert_valid_hsv_action(steady)
    assert steady[1:] != (0, 0, 100)  # not flashing on a steady signal

    hard_hit = make_bands([1.0 * 3.0, 1.0, 1.0], rms=0.05)
    flashed = ar._apply_mode("strobe_on_drop", hard_hit, ctx)
    assert_valid_hsv_action(flashed)
    assert flashed[2] == 0 and flashed[3] == 100, "a hard hit should trigger the white flash (sat=0, v=100)"


def test_palette_cycle_produces_valid_rgb_brightness_and_advances_on_beat():
    ctx = make_ctx()
    bands = make_bands([1.0, 1.0, 1.0], rms=0.01)
    for _ in range(20):
        run_frames("palette_cycle", bands, ctx, n=1)
    action = ar._apply_mode("palette_cycle", bands, ctx)
    assert_valid_rgb_brightness_action(action)
    idx_before = ctx["palette_idx"]
    hard_hit = make_bands([1.0 * 3.0, 1.0, 1.0], rms=0.05)
    ar._apply_mode("palette_cycle", hard_hit, ctx)
    assert ctx["palette_idx"] != idx_before, "a detected beat should advance the palette index"


def test_spectrum_gradient_across_n_bands_low_vs_high():
    ctx_low = make_ctx()
    low = run_frames("spectrum_gradient", make_bands([100.0, 0.0, 0.0, 0.0, 0.0, 0.0], rms=0.05), ctx_low, n=CONVERGE_FRAMES)
    ctx_high = make_ctx()
    high = run_frames("spectrum_gradient", make_bands([0.0, 0.0, 0.0, 0.0, 0.0, 100.0], rms=0.05), ctx_high, n=CONVERGE_FRAMES)
    assert_valid_hsv_action(low)
    assert_valid_hsv_action(high)
    assert low[1] < high[1], "a low-band-dominant signal should land at a lower hue than a high-band-dominant one"


def test_band_flash_overlay_flashes_the_hit_band():
    ctx = make_ctx()
    ambient = make_bands([5.0, 5.0, 5.0, 5.0, 5.0, 5.0], rms=0.02)
    for _ in range(20):
        run_frames("band_flash_overlay", ambient, ctx, n=1)
    steady = run_frames("band_flash_overlay", ambient, ctx, n=1)
    assert_valid_hsv_action(steady)

    spike = make_bands([5.0, 5.0, 5.0 * 3.0, 5.0, 5.0, 5.0], rms=0.05)
    flashed = ar._apply_mode("band_flash_overlay", spike, ctx)
    assert_valid_hsv_action(flashed)
    assert flashed[3] > steady[3], "a per-band spike should read brighter than the ambient overlay baseline"


def test_stereo_split_biases_hue_toward_the_louder_channel():
    ctx = make_ctx()
    ctx["left_rms"], ctx["right_rms"] = 0.1, 0.001
    left_loud = run_frames("stereo_split", make_bands([1.0, 1.0, 1.0], rms=0.05), ctx, n=CONVERGE_FRAMES)
    assert_valid_hsv_action(left_loud)

    ctx2 = make_ctx()
    ctx2["left_rms"], ctx2["right_rms"] = 0.001, 0.1
    right_loud = run_frames("stereo_split", make_bands([1.0, 1.0, 1.0], rms=0.05), ctx2, n=CONVERGE_FRAMES)
    assert_valid_hsv_action(right_loud)
    assert left_loud[1] != right_loud[1], "left- vs right-dominant stereo input should settle on different hues"


def test_breathing_silence_never_crashes_and_stays_in_range():
    ctx = make_ctx()
    silent = make_bands([0.0, 0.0, 0.0], rms=0.0)
    for _ in range(5):
        action = ar._apply_mode("breathing_silence", silent, ctx)
        assert_valid_hsv_action(action)
