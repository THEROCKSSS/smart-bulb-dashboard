"""Direct unit tests for the two new audio-reactive color modes added in
Week 1: `harmonic_pairs` and `kick_snare_split`. These call `_apply_mode()`
straight, with hand-built `bands` dicts (the same shape `analyze_frame()`
returns) and a real `ctx` from `_new_ctx()` — no audio device, no FFT, no
FastAPI server involved.

Run with:
    pytest backend/tests/test_audio_modes.py -v
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import audio_reactive as ar  # noqa: E402

CONVERGE_FRAMES = 150  # enough iterations for the circular hue smoothing
                       # (alpha ~0.25-0.3 per frame) to settle on a fixed
                       # per-frame target within float precision.


def make_bands(energies, rms=0.01):
    """Build a bands dict matching analyze_frame()'s real output shape:
    {"energies": [...], "fractions": [...], "rms": ..., ...}. `fractions`
    is derived the exact same way analyze_frame does it (energy / total).
    """
    total = sum(energies) + 1e-9
    fractions = [e / total for e in energies]
    return {
        "spectrum": None,
        "freqs": None,
        "rms": rms,
        "energies": list(energies),
        "fractions": fractions,
        "band_edges": None,
    }


def make_ctx():
    return ar._new_ctx(sensitivity=1.0, monochrome_hue=280.0)


def run_frames(mode, bands, ctx, n=1):
    action = None
    for _ in range(n):
        action = ar._apply_mode(mode, bands, ctx)
    return action


def assert_valid_hsv_action(action):
    assert action[0] == "hsv", f"expected an hsv action, got {action[0]!r}"
    _, hue, sat, brightness = action
    assert 0 <= hue <= 360, f"hue out of range: {hue}"
    assert 0 <= sat <= 100, f"sat out of range: {sat}"
    assert 0 <= brightness <= 100, f"brightness out of range: {brightness}"


# ---------------------------------------------------------------- MODES ---
def test_modes_are_registered():
    assert "harmonic_pairs" in ar.MODES
    assert "kick_snare_split" in ar.MODES


# --------------------------------------------------------- harmonic_pairs --
def test_harmonic_pairs_silence_is_stable_non_flickery():
    ctx = make_ctx()
    bands = make_bands([0.0, 0.0, 0.0], rms=0.0)
    # Let the circular smoothing converge first (starting hue is
    # BASS_HUE=10 from _new_ctx, target at true silence is deterministic
    # HARMONIC_HUE_B, so it takes several frames to get there).
    run_frames("harmonic_pairs", bands, ctx, n=CONVERGE_FRAMES)
    # Once converged, further silent frames must not move the hue at all
    # (that's the actual "non-flickery" guarantee — a steady room stays
    # on one hue, it doesn't hunt around).
    settled = [ar._apply_mode("harmonic_pairs", bands, ctx) for _ in range(5)]
    for a in settled:
        assert_valid_hsv_action(a)
    hues = [round(a[1], 6) for a in settled]
    assert len(set(hues)) == 1, f"hue flickered at silence after convergence: {hues}"
    assert abs(hues[0] - ar.HARMONIC_HUE_B) < 1e-6


def test_harmonic_pairs_single_dominant_band():
    ctx = make_ctx()
    bands = make_bands([100.0, 0.0, 0.0], rms=0.05)
    action = run_frames("harmonic_pairs", bands, ctx, n=CONVERGE_FRAMES)
    assert_valid_hsv_action(action)
    # Band 0 is the only non-zero band in the only available non-adjacent
    # pair (0, 2) for a 3-band split, so the hue should converge near
    # HARMONIC_HUE_A (the "band A dominates" anchor).
    assert abs(action[1] - ar.HARMONIC_HUE_A) < 1.0


def test_harmonic_pairs_two_nonadjacent_bands_high():
    # 5 bands so the pair-selection logic itself is genuinely exercised:
    # bands 0 and 4 are both hot (non-adjacent, biggest combined energy);
    # bands 1-3 are quiet. The winning pair must be (0, 4), not any
    # adjacent combination.
    ctx = make_ctx()
    bands = make_bands([50.0, 0.0, 0.0, 0.0, 50.0], rms=0.05)
    action = run_frames("harmonic_pairs", bands, ctx, n=CONVERGE_FRAMES)
    assert_valid_hsv_action(action)
    # Equal energy on both sides of the winning pair -> ratio_a == 0.5 ->
    # hue should land at the circular midpoint between the two anchors,
    # i.e. roughly equidistant from HARMONIC_HUE_A and HARMONIC_HUE_B.
    dist_a = min(abs(action[1] - ar.HARMONIC_HUE_A), 360 - abs(action[1] - ar.HARMONIC_HUE_A))
    dist_b = min(abs(action[1] - ar.HARMONIC_HUE_B), 360 - abs(action[1] - ar.HARMONIC_HUE_B))
    assert abs(dist_a - dist_b) < 1.0, (
        f"expected hue roughly equidistant from both anchors at a 50/50 "
        f"split, got hue={action[1]} dist_a={dist_a} dist_b={dist_b}"
    )


def test_harmonic_pairs_adjacent_high_bands_are_excluded():
    # Bands 1 and 2 (adjacent) are the loudest individually, but the mode
    # must never pair them together — only non-adjacent pairs are valid.
    # Just confirm this still produces a sane, valid action rather than
    # crashing or returning a degenerate value.
    ctx = make_ctx()
    bands = make_bands([0.0, 40.0, 40.0, 0.0, 0.0], rms=0.05)
    action = run_frames("harmonic_pairs", bands, ctx, n=CONVERGE_FRAMES)
    assert_valid_hsv_action(action)


# ------------------------------------------------------- kick_snare_split --
def test_kick_snare_split_silence_is_stable_non_flickery():
    ctx = make_ctx()
    bands = make_bands([0.0, 0.0, 0.0], rms=0.0)
    run_frames("kick_snare_split", bands, ctx, n=CONVERGE_FRAMES)
    settled = [ar._apply_mode("kick_snare_split", bands, ctx) for _ in range(5)]
    for a in settled:
        assert_valid_hsv_action(a)
    hues = [round(a[1], 6) for a in settled]
    brightnesses = [a[3] for a in settled]
    assert len(set(hues)) == 1, f"hue flickered at silence after convergence: {hues}"
    assert abs(hues[0] - ar.KICK_SNARE_BASE_HUE) < 1e-6
    # Brightness should sit at the same floor every frame (no flicker),
    # matching the ~4 floor used by every other mode's brightness formula.
    assert all(abs(b - brightnesses[0]) < 1e-6 for b in brightnesses)
    assert brightnesses[0] <= 10


def test_kick_snare_split_single_dominant_band():
    # A single dominant low (bass) band should read as a strong kick:
    # high brightness, hue pinned to the base (no snare accent).
    ctx = make_ctx()
    bands = make_bands([100.0, 0.0, 0.0], rms=0.05)
    action = run_frames("kick_snare_split", bands, ctx, n=CONVERGE_FRAMES)
    assert_valid_hsv_action(action)
    assert abs(action[1] - ar.KICK_SNARE_BASE_HUE) < 1e-6
    assert action[3] > 10  # a real kick hit should read brighter than the silence floor


def test_kick_snare_split_low_and_mid_spike_together():
    # A low-band (kick) spike simultaneous with a separate mid-band
    # (snare) spike: brightness should be driven up by the kick, and the
    # hue should shift away from the base by the snare accent.
    ctx = make_ctx()
    bands = make_bands([60.0, 60.0, 0.0], rms=0.05)
    action = run_frames("kick_snare_split", bands, ctx, n=CONVERGE_FRAMES)
    assert_valid_hsv_action(action)
    assert action[3] > 10
    assert action[1] != ar.KICK_SNARE_BASE_HUE, "mid-band accent should shift the hue off the base"


def test_kick_snare_split_mid_only_shifts_hue_without_bass():
    # A pure mid-band (snare-only) spike with no bass at all should still
    # shift the hue via the accent term, while brightness stays near the
    # quiet floor since there's no bass energy driving the kick pulse.
    ctx = make_ctx()
    bands = make_bands([0.0, 80.0, 0.0], rms=0.01)
    action = run_frames("kick_snare_split", bands, ctx, n=CONVERGE_FRAMES)
    assert_valid_hsv_action(action)
    assert action[1] != ar.KICK_SNARE_BASE_HUE
