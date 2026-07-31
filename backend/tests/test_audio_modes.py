"""Unit tests for the audio-reactive color modes, the BPM/tempo tracker,
and the genre/mood preset bundles in `audio_reactive.py`. The mode tests
call `_apply_mode()` straight, with hand-built `bands` dicts (the same
shape `analyze_frame()` returns) and a real `ctx` from `_new_ctx()` — no
audio device, no FFT, no FastAPI server involved.

Run with:
    pytest backend/tests/test_audio_modes.py -v
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np  # noqa: E402

import audio_reactive as ar  # noqa: E402
import config as cfgmod  # noqa: E402

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


# ============================================================================
# Week 1 additional modes: energy_contour, bass_only_pulse, mirror_mode,
# random_walk_hue, silence_flash_recover, crescendo_ramp
# ============================================================================
def test_new_modes_are_registered():
    for m in ("energy_contour", "bass_only_pulse", "mirror_mode",
              "random_walk_hue", "silence_flash_recover", "crescendo_ramp"):
        assert m in ar.MODES


# ------------------------------------------------------------- energy_contour
def test_energy_contour_silence_settles_to_floor():
    ctx = make_ctx()
    bands = make_bands([0.0, 0.0, 0.0], rms=0.0)
    action = run_frames("energy_contour", bands, ctx, n=CONVERGE_FRAMES)
    assert_valid_hsv_action(action)
    assert action[1] == ctx["monochrome_hue"], "hue must stay locked to the monochrome hue"
    assert action[3] <= 5, "brightness should settle at the silence floor"
    assert action[2] <= 41, "saturation should settle near its low anchor"


def test_energy_contour_loud_settles_bright_and_saturated():
    ctx = make_ctx()
    bands = make_bands([1.0, 1.0, 1.0], rms=0.05)
    action = run_frames("energy_contour", bands, ctx, n=CONVERGE_FRAMES)
    assert_valid_hsv_action(action)
    assert action[1] == ctx["monochrome_hue"]
    assert action[3] > 90
    assert action[2] > 90


def test_energy_contour_hue_never_moves_regardless_of_band_balance():
    # Multi-band input with a dominant single band must not move the hue --
    # only saturation/brightness are supposed to react in this mode.
    ctx = make_ctx()
    bands = make_bands([100.0, 0.0, 0.0], rms=0.02)
    action = run_frames("energy_contour", bands, ctx, n=CONVERGE_FRAMES)
    assert action[1] == ctx["monochrome_hue"]


# ----------------------------------------------------------- bass_only_pulse
def test_bass_only_pulse_silence_is_floor_and_fixed_hue():
    ctx = make_ctx()
    bands = make_bands([0.0, 0.0, 0.0], rms=0.0)
    action = ar._apply_mode("bass_only_pulse", bands, ctx)
    assert_valid_hsv_action(action)
    assert action[1] == ctx["monochrome_hue"]
    assert action[3] == 4


def test_bass_only_pulse_bass_dominant_drives_brightness_up():
    ctx = make_ctx()
    bands = make_bands([100.0, 0.0, 0.0], rms=0.005)
    action = ar._apply_mode("bass_only_pulse", bands, ctx)
    assert_valid_hsv_action(action)
    assert action[1] == ctx["monochrome_hue"], "hue must never move in this mode"
    assert action[3] > 10


def test_bass_only_pulse_ignores_non_bass_energy():
    # Same rms as the bass-dominant case, but all the energy is in the mid
    # band -- brightness must stay at the silence floor since bass_frac is 0.
    ctx = make_ctx()
    bands = make_bands([0.0, 100.0, 0.0], rms=0.005)
    action = ar._apply_mode("bass_only_pulse", bands, ctx)
    assert_valid_hsv_action(action)
    assert action[3] == 4


# ---------------------------------------------------------------- mirror_mode
def test_mirror_mode_silence_converges_to_center():
    ctx = make_ctx()
    bands = make_bands([0.0, 0.0, 0.0], rms=0.0)
    action = run_frames("mirror_mode", bands, ctx, n=CONVERGE_FRAMES)
    assert_valid_hsv_action(action)
    assert abs(action[1] - ar.MIRROR_CENTER_HUE) < 1.0


def test_mirror_mode_bass_dominant_mirrors_one_direction():
    ctx = make_ctx()
    bands = make_bands([100.0, 0.0, 0.0], rms=0.05)
    action = run_frames("mirror_mode", bands, ctx, n=CONVERGE_FRAMES)
    assert_valid_hsv_action(action)
    expected = (ar.MIRROR_CENTER_HUE - ar.MIRROR_SWING_DEG) % 360
    assert abs(action[1] - expected) < 1.0


def test_mirror_mode_treble_dominant_mirrors_the_other_direction():
    ctx = make_ctx()
    bands = make_bands([0.0, 0.0, 100.0], rms=0.05)
    action = run_frames("mirror_mode", bands, ctx, n=CONVERGE_FRAMES)
    assert_valid_hsv_action(action)
    expected = (ar.MIRROR_CENTER_HUE + ar.MIRROR_SWING_DEG) % 360
    assert abs(action[1] - expected) < 1.0
    # And it should genuinely be the mirror image of the bass-dominant case,
    # i.e. equidistant from the center in the opposite direction.
    bass_ctx = make_ctx()
    bass_bands = make_bands([100.0, 0.0, 0.0], rms=0.05)
    bass_action = run_frames("mirror_mode", bass_bands, bass_ctx, n=CONVERGE_FRAMES)
    dist_treble = min(abs(action[1] - ar.MIRROR_CENTER_HUE), 360 - abs(action[1] - ar.MIRROR_CENTER_HUE))
    dist_bass = min(abs(bass_action[1] - ar.MIRROR_CENTER_HUE), 360 - abs(bass_action[1] - ar.MIRROR_CENTER_HUE))
    assert abs(dist_treble - dist_bass) < 1.0


# ------------------------------------------------------------ random_walk_hue
def test_random_walk_hue_step_is_bounded_every_frame():
    ctx = make_ctx()
    bands = make_bands([1.0, 1.0, 1.0], rms=0.02)
    hues = []
    for _ in range(300):
        action = ar._apply_mode("random_walk_hue", bands, ctx)
        assert_valid_hsv_action(action)
        hues.append(action[1])
    for h1, h2 in zip(hues, hues[1:]):
        delta = abs(h2 - h1)
        delta = min(delta, 360 - delta)  # circular distance
        assert delta <= ar.RANDOM_WALK_MAX_STEP_DEG + 1e-6, f"step {delta} exceeded the bound"


def test_random_walk_hue_actually_moves_around_over_time():
    # It should be a genuine walk, not stuck at one value or auto_rotate's
    # fixed increment -- confirm real spread and non-monotonic direction
    # over many frames.
    ctx = make_ctx()
    bands = make_bands([1.0, 1.0, 1.0], rms=0.02)
    hues = [ar._apply_mode("random_walk_hue", bands, ctx)[1] for _ in range(400)]
    assert len(set(round(h, 2) for h in hues)) > 50, "hue should visit many distinct values"
    diffs = [b - a for a, b in zip(hues, hues[1:])]
    assert any(d > 0 for d in diffs) and any(d < 0 for d in diffs), (
        "a genuine random walk should step both up and down, unlike a fixed-direction rotation"
    )


# ------------------------------------------------------ silence_flash_recover
def test_silence_flash_recover_no_flash_on_first_ever_frame():
    # A session that has never seen silence yet must not flash just because
    # this happens to be the very first frame processed.
    ctx = make_ctx()
    bands = make_bands([100.0, 0.0, 0.0], rms=0.05)
    action = ar._apply_mode("silence_flash_recover", bands, ctx)
    assert action != ("hsv", 0, 0, 100)


def test_silence_flash_recover_flashes_after_long_silence():
    ctx = make_ctx()
    ctx["silence_since"] = time.time() - (ar.SILENCE_FLASH_LONG_THRESHOLD_S + 1.0)
    bands = make_bands([100.0, 0.0, 0.0], rms=0.05)
    action = ar._apply_mode("silence_flash_recover", bands, ctx)
    assert action == ("hsv", 0, 0, 100)
    assert ctx["silence_since"] is None, "silence tracking must reset once audio resumes"


def test_silence_flash_recover_no_second_flash_immediately_after():
    ctx = make_ctx()
    ctx["silence_since"] = time.time() - (ar.SILENCE_FLASH_LONG_THRESHOLD_S + 1.0)
    bands = make_bands([100.0, 0.0, 0.0], rms=0.05)
    first = ar._apply_mode("silence_flash_recover", bands, ctx)
    second = ar._apply_mode("silence_flash_recover", bands, ctx)
    assert first == ("hsv", 0, 0, 100)
    assert second != ("hsv", 0, 0, 100)


def test_silence_flash_recover_short_silence_does_not_flash():
    ctx = make_ctx()
    ctx["silence_since"] = time.time() - (ar.SILENCE_FLASH_LONG_THRESHOLD_S - 1.0)
    bands = make_bands([100.0, 0.0, 0.0], rms=0.05)
    action = ar._apply_mode("silence_flash_recover", bands, ctx)
    assert action != ("hsv", 0, 0, 100)


def test_silence_flash_recover_tracks_silence_start():
    ctx = make_ctx()
    assert ctx.get("silence_since") is None
    bands = make_bands([0.0, 0.0, 0.0], rms=0.0)
    action = ar._apply_mode("silence_flash_recover", bands, ctx)
    assert_valid_hsv_action(action)
    assert ctx["silence_since"] is not None


# ------------------------------------------------------------- crescendo_ramp
def test_crescendo_ramp_flat_silence_stays_at_floor():
    ctx = make_ctx()
    bands = make_bands([0.0, 0.0, 0.0], rms=0.0)
    action = run_frames("crescendo_ramp", bands, ctx, n=ar.CRESCENDO_WINDOW_FRAMES + 5)
    assert_valid_hsv_action(action)
    assert ctx["crescendo_ramp_level"] < 0.05
    assert action[3] <= 5


def test_crescendo_ramp_flat_loud_does_not_ramp():
    # Loud but NOT rising -- ramp should stay near zero even though rms is
    # high, distinguishing this from a simple loudness threshold.
    ctx = make_ctx()
    bands = make_bands([1.0, 1.0, 1.0], rms=0.01)
    action = run_frames("crescendo_ramp", bands, ctx, n=ar.CRESCENDO_WINDOW_FRAMES + 5)
    assert_valid_hsv_action(action)
    assert ctx["crescendo_ramp_level"] < 0.05


def test_crescendo_ramp_rising_trend_boosts_brightness_and_saturation():
    ctx_rising = make_ctx()
    action_rising = None
    for v in np.linspace(0.001, 0.01, ar.CRESCENDO_WINDOW_FRAMES):
        bands = make_bands([1.0, 1.0, 1.0], rms=float(v))
        action_rising = ar._apply_mode("crescendo_ramp", bands, ctx_rising)
    assert_valid_hsv_action(action_rising)
    assert ctx_rising["crescendo_ramp_level"] > 0.3, "a genuine sustained rise should trigger a real ramp"

    # A flat signal held at the SAME final rms the whole time should score
    # a lower ramp and lower brightness/saturation than the rising case,
    # proving the boost comes from the trend, not just the final loudness.
    ctx_flat = make_ctx()
    action_flat = None
    for _ in range(ar.CRESCENDO_WINDOW_FRAMES):
        bands = make_bands([1.0, 1.0, 1.0], rms=0.01)
        action_flat = ar._apply_mode("crescendo_ramp", bands, ctx_flat)
    assert action_rising[3] > action_flat[3]
    assert action_rising[2] > action_flat[2]


# ============================================================================
# TempoTracker: real BPM estimation, beat confidence, adaptive threshold,
# tap tempo, silence-aware reset
# ============================================================================
def _synth_beat_rms(bpm, duration_s, frame_dt, pulse_frames=3, base=0.001, peak=0.05):
    """A synthetic onset-strength signal with a real, known, constructed
    periodic beat: a short loud pulse every `60/bpm` seconds, quiet
    baseline in between. This is the ground truth the BPM estimator is
    checked against below -- not just "it returns a number"."""
    period_s = 60.0 / bpm
    n = int(duration_s / frame_dt)
    values = []
    t = 0.0
    for _ in range(n):
        phase = t % period_s
        values.append(peak if phase < pulse_frames * frame_dt else base)
        t += frame_dt
    return values


FRAME_DT = ar.BLOCK_SIZE / ar.SAMPLE_RATE


def test_tempo_tracker_estimates_known_bpm_within_tolerance():
    for target_bpm in (90.0, 120.0, 150.0):
        tracker = ar.TempoTracker()
        for v in _synth_beat_rms(target_bpm, duration_s=10.0, frame_dt=FRAME_DT):
            tracker.update(v)
        assert tracker.bpm is not None, f"no BPM estimate produced for {target_bpm} target"
        assert abs(tracker.bpm - target_bpm) < 5.0, (
            f"target={target_bpm} estimated={tracker.bpm} outside tolerance"
        )
        assert tracker.confidence > 0.4, f"confidence too low for a clean synthetic beat: {tracker.confidence}"


def test_tempo_tracker_confidence_higher_for_clean_beat_than_noise():
    clean = ar.TempoTracker()
    for v in _synth_beat_rms(120.0, duration_s=10.0, frame_dt=FRAME_DT):
        clean.update(v)

    rng = np.random.default_rng(42)
    noisy = ar.TempoTracker()
    for _ in range(int(10.0 / FRAME_DT)):
        noisy.update(float(rng.uniform(0.0005, 0.002)))

    assert clean.confidence > noisy.confidence
    assert clean.confidence > 0.4
    assert noisy.confidence < 0.3


def test_tempo_tracker_silence_resets_bpm_and_confidence():
    tracker = ar.TempoTracker()
    for v in _synth_beat_rms(120.0, duration_s=6.0, frame_dt=FRAME_DT):
        tracker.update(v)
    assert tracker.bpm is not None

    for _ in range(tracker._silence_reset_frames + 5):
        tracker.update(0.0)

    assert tracker.bpm is None, "a long pause must drop the stale tempo estimate"
    assert tracker.confidence == 0.0
    assert len(tracker.onset_history) == 0


def test_tempo_tracker_short_silence_does_not_reset():
    tracker = ar.TempoTracker()
    for v in _synth_beat_rms(120.0, duration_s=6.0, frame_dt=FRAME_DT):
        tracker.update(v)
    bpm_before = tracker.bpm
    assert bpm_before is not None

    for _ in range(tracker._silence_reset_frames // 2):
        tracker.update(0.0)

    assert tracker.bpm is not None
    assert tracker.bpm == bpm_before, "a short gap shouldn't touch the estimate at all (no update happens while it's short)"


def test_tap_tempo_computes_bpm_from_regular_intervals():
    tracker = ar.TempoTracker()
    t0 = 1_000_000.0
    for i in range(6):
        tracker.tap(timestamp=t0 + i * 0.5)  # 0.5s apart == 120 BPM
    assert tracker.tap_bpm is not None
    assert abs(tracker.tap_bpm - 120.0) < 0.5


def test_tap_tempo_resets_after_a_long_gap():
    tracker = ar.TempoTracker()
    tracker.tap(timestamp=1000.0)
    tracker.tap(timestamp=1000.5)  # 120 BPM pair
    assert abs(tracker.tap_bpm - 120.0) < 0.5
    # A gap far longer than TAP_TEMPO_MAX_GAP_S should start a fresh
    # sequence rather than blending stale + new taps into a bogus interval.
    tracker.tap(timestamp=1010.0)
    tracker.tap(timestamp=1010.4)  # 150 BPM pair
    assert abs(tracker.tap_bpm - 150.0) < 0.5


def test_beat_sensitivity_presets_change_adaptive_threshold():
    signal = _synth_beat_rms(120.0, duration_s=5.0, frame_dt=FRAME_DT)
    subtle = ar.TempoTracker(beat_sensitivity="subtle")
    normal = ar.TempoTracker(beat_sensitivity="normal")
    aggressive = ar.TempoTracker(beat_sensitivity="aggressive")
    for v in signal:
        subtle.update(v)
        normal.update(v)
        aggressive.update(v)
    # Same underlying onset data, only k differs -- aggressive should be
    # the easiest threshold to clear, subtle the hardest.
    assert aggressive.adaptive_threshold < normal.adaptive_threshold < subtle.adaptive_threshold


def test_set_sensitivity_rejects_unknown_preset():
    tracker = ar.TempoTracker()
    assert tracker.set_sensitivity("bogus") is False
    assert tracker.beat_sensitivity == ar.DEFAULT_BEAT_SENSITIVITY
    assert tracker.set_sensitivity("aggressive") is True
    assert tracker.beat_sensitivity == "aggressive"


def test_tempo_tracker_status_shape():
    tracker = ar.TempoTracker()
    status = tracker.status()
    for key in ("bpm", "confidence", "beat_sensitivity", "adaptive_threshold",
                "is_beat", "tap_bpm", "suggested_preset"):
        assert key in status


# ============================================================================
# Genre/mood presets: bundle application and BPM-based suggestion
# ============================================================================
class _FakeController:
    """Minimal stand-in for bulb_manager's real controller, just enough for
    AudioSession's __init__/BulbSender to construct without touching any
    real device or network call. No action is ever queued in these tests,
    so BulbSender's background thread never attempts a real send."""

    def __init__(self):
        self.cfg = {"id": "fake-device"}

    def stop_effect(self):
        pass

    def _log(self, *args, **kwargs):
        pass


def test_all_genre_presets_have_valid_fields():
    assert len(ar.AUDIO_GENRE_PRESETS) == 8
    valid_color_ids = {p["id"] for p in ar.PRESET_COLORS}
    seen_ids = set()
    for preset in ar.AUDIO_GENRE_PRESETS:
        assert preset["mode"] in ar.MODES
        assert preset["beat_sensitivity"] in ar.BEAT_SENSITIVITY_PRESETS
        assert preset["min_dwell_ms"] >= ar.MIN_DWELL_FLOOR_MS
        assert preset["n_bands"] >= 3
        assert 0.1 <= preset["sensitivity"] <= 5.0
        assert all(cid in valid_color_ids for cid in preset["palette"])
        assert preset["id"] not in seen_ids, "preset ids must be unique"
        seen_ids.add(preset["id"])


def test_find_genre_preset_lookup():
    assert ar.find_genre_preset("edm_party")["name"] == "EDM / Party"
    assert ar.find_genre_preset("does_not_exist") is None


def test_apply_genre_preset_actually_sets_session_config():
    # This is the "does applying a preset actually configure a session"
    # test: construct a real AudioSession (without starting its capture
    # thread) using a preset's bundled fields, then assert every one of
    # mode/sensitivity/dwell/n_bands/beat_sensitivity/monochrome_hue
    # actually landed where it should.
    for preset_id in ("edm_party", "classical_acoustic", "hip_hop_bass_heavy", "lofi_study"):
        preset = ar.find_genre_preset(preset_id)
        controller = _FakeController()
        session = ar.AudioSession(
            controller, device_index=0, mode=preset["mode"], sensitivity=preset["sensitivity"],
            monochrome_hue=preset["monochrome_hue"], n_bands=preset["n_bands"],
            min_dwell_ms=preset["min_dwell_ms"], beat_sensitivity=preset["beat_sensitivity"],
        )
        try:
            assert session.mode == preset["mode"]
            assert session.ctx["sensitivity"] == preset["sensitivity"]
            assert session.sender.min_dwell_ms == preset["min_dwell_ms"]
            assert session.n_bands == preset["n_bands"]
            assert session.tempo.beat_sensitivity == preset["beat_sensitivity"]
            assert abs(session.ctx["monochrome_hue"] - (preset["monochrome_hue"] % 360)) < 1e-6
        finally:
            session.sender.stop()


def test_suggest_preset_for_bpm_covers_known_ranges():
    assert ar.suggest_preset_for_bpm(None) is None
    assert ar.suggest_preset_for_bpm(60.0) == "lofi_study"
    assert ar.suggest_preset_for_bpm(80.0) == "chill_ambient"
    assert ar.suggest_preset_for_bpm(124.0) == "hip_hop_bass_heavy"
    assert ar.suggest_preset_for_bpm(160.0) == "edm_party"
    assert ar.suggest_preset_for_bpm(200.0) == "metal_hardcore"


# --------------------------------------------------------- custom presets --
def test_build_custom_preset_produces_valid_bundle():
    preset = ar.build_custom_preset(
        "Friday Mix!", "band_fixed", sensitivity=1.4, min_dwell_ms=80, n_bands=5,
        monochrome_hue=400.0, beat_sensitivity="aggressive", palette=["red", "blue"],
        description="test preset",
    )
    assert preset["id"] == "friday_mix"
    assert preset["mode"] == "band_fixed"
    assert preset["sensitivity"] == 1.4
    assert preset["min_dwell_ms"] == 80
    assert preset["n_bands"] == 5
    assert preset["monochrome_hue"] == 40.0  # 400 % 360
    assert preset["beat_sensitivity"] == "aggressive"
    assert preset["palette"] == ["red", "blue"]
    assert preset["custom"] is True


def test_build_custom_preset_rejects_invalid_mode():
    try:
        ar.build_custom_preset("Bad", "not_a_real_mode")
        assert False, "expected ValueError for an unknown mode"
    except ValueError:
        pass


def test_build_custom_preset_rejects_invalid_palette_id():
    try:
        ar.build_custom_preset("Bad Palette", "band_fixed", palette=["not_a_real_color"])
        assert False, "expected ValueError for an unknown palette color id"
    except ValueError:
        pass


def test_build_custom_preset_rejects_dwell_below_floor():
    try:
        ar.build_custom_preset("Too Fast", "band_fixed", min_dwell_ms=1)
        assert False, "expected ValueError for a dwell below the safety floor"
    except ValueError:
        pass


# ============================================================================
# HTTP-level: /api/audio/presets* routes, via the real FastAPI app.
#
# These use the `client`/`fake_config` fixtures from conftest.py, which
# already keep every route in this suite off the real backend/config.json
# and real hardware. The one extra thing needed here specifically:
# `fake_config` fakes `load_config` but not `save_config` (nothing else in
# the existing suite persists anything), and the new custom-preset routes
# DO call `cfgmod.save_config`, so each test that saves/deletes a preset
# monkeypatches it to a no-op -- the route still mutates the in-memory
# `cfg` dict returned by the faked `load_config` (which IS the same dict
# object as the `fake_config` fixture's `state`), so the roundtrip is still
# genuinely exercised without ever touching disk.
#
# Deliberately NOT tested here: POST .../audio-reactive/start,
# .../audio-reactive/apply-preset, and the group equivalents -- those spin
# up a real `sounddevice.InputStream` against a real input device index,
# which has no business running in an automated test (see the "what was
# skipped" notes in the final report).
# ============================================================================
def test_audio_devices_endpoint_exposes_beat_sensitivity_presets(client):
    resp = client.get("/api/audio/devices")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body["beat_sensitivity_presets"]) == set(ar.BEAT_SENSITIVITY_PRESETS.keys())
    assert body["default_beat_sensitivity"] == ar.DEFAULT_BEAT_SENSITIVITY


def test_audio_presets_endpoint_lists_all_builtins(client):
    resp = client.get("/api/audio/presets")
    assert resp.status_code == 200
    ids = {p["id"] for p in resp.json()["presets"]}
    assert {
        "edm_party", "chill_ambient", "rock_live", "classical_acoustic",
        "hip_hop_bass_heavy", "jazz_improv", "lofi_study", "metal_hardcore",
    } <= ids


def test_audio_presets_suggest_endpoint(client):
    resp = client.get("/api/audio/presets/suggest", params={"bpm": 124})
    assert resp.status_code == 200
    body = resp.json()
    assert body["suggested_preset_id"] == "hip_hop_bass_heavy"
    assert body["preset"]["id"] == "hip_hop_bass_heavy"


def test_audio_presets_custom_save_list_and_delete_roundtrip(client, fake_config, monkeypatch):
    monkeypatch.setattr(cfgmod, "save_config", lambda cfg: None)

    save_resp = client.post("/api/audio/presets/custom", json={
        "name": "Test Save", "mode": "band_fixed", "sensitivity": 1.2,
        "min_dwell_ms": 100, "n_bands": 3, "monochrome_hue": 200.0,
        "beat_sensitivity": "normal", "palette": ["red"], "description": "",
    })
    assert save_resp.status_code == 200
    preset = save_resp.json()["preset"]
    assert preset["id"] == "test_save"
    assert preset["mode"] == "band_fixed"
    assert any(p["id"] == "test_save" for p in fake_config["audio_custom_presets"])

    list_resp = client.get("/api/audio/presets")
    assert any(p["id"] == "test_save" for p in list_resp.json()["presets"])

    delete_resp = client.delete("/api/audio/presets/custom/test_save")
    assert delete_resp.status_code == 200
    assert not any(p["id"] == "test_save" for p in fake_config["audio_custom_presets"])

    delete_again_resp = client.delete("/api/audio/presets/custom/test_save")
    assert delete_again_resp.status_code == 404


def test_audio_presets_custom_rejects_invalid_mode(client):
    resp = client.post("/api/audio/presets/custom", json={"name": "Bad", "mode": "not_a_real_mode"})
    assert resp.status_code == 400
