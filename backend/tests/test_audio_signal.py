"""Tests for Section 4's signal-conditioning module (backend/audio_signal.py):
AGC with attack/release time constants, a noise gate, clipping/overload
detection, DC offset removal, multi-band independent gain, the peak-hold
input level meter, and per-device saved calibration (the "sample a few
seconds of room silence" flow).

Run with:
    pytest backend/tests/test_audio_signal.py -v
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


@pytest.fixture
def cal_reset(tmp_path, monkeypatch):
    """Points the calibration store at a tmp file so tests never touch the
    real backend/data/audio_calibration.json."""
    fake_path = tmp_path / "audio_calibration.json"
    monkeypatch.setattr(asig, "CALIBRATION_PATH", str(fake_path))
    yield


FRAME_DT = ar.BLOCK_SIZE / float(ar.SAMPLE_RATE)  # ~11.6ms, matches real capture cadence


# --------------------------------------------------------------- DC offset --
def test_dc_offset_removal_converges_and_zeros_the_bias():
    # Exactly 300 full blocks (no short trailing block) so the final meter
    # reflects true steady state, not an artificially low-RMS padded tail.
    tone = af.make_tone(440, af.duration_for_n_blocks(300), amplitude=0.3)
    biased = af.add_dc_offset(tone, 0.25)
    conditioner = asig.SignalConditioner(dc_removal_enabled=True, noise_gate_enabled=False)
    now = 0.0
    output_mean = None
    for block in af.iter_blocks(biased):
        now += FRAME_DT
        conditioned, meter = conditioner.process(block, frame_dt=FRAME_DT, now=now)
        output_mean = float(np.mean(conditioned))
    assert meter["dc_offset"] == pytest.approx(0.25, abs=0.03)
    assert abs(output_mean) < 0.03, f"DC bias should be mostly removed by the end, got mean={output_mean}"


def test_dc_offset_removal_disabled_leaves_bias_untouched():
    tone = af.make_tone(440, 0.05, amplitude=0.1)
    biased = af.add_dc_offset(tone, 0.3)
    conditioner = asig.SignalConditioner(dc_removal_enabled=False, noise_gate_enabled=False)
    conditioned, meter = conditioner.process(biased, frame_dt=FRAME_DT, now=0.0)
    assert meter["dc_offset"] == 0.0
    assert float(np.mean(conditioned)) == pytest.approx(0.3, abs=0.02)


# ---------------------------------------------------------------------- AGC --
def test_agc_boosts_quiet_signal_toward_target():
    # amplitude 0.05 -> rms ~0.0354; desired_gain = 0.15/0.0354 ~= 4.24,
    # comfortably inside the default [0.1, 8.0] gain clamp so the test
    # actually exercises convergence rather than the clamp ceiling.
    quiet_tone = af.make_tone(440, af.duration_for_n_blocks(300), amplitude=0.05)
    conditioner = asig.SignalConditioner(agc_enabled=True, noise_gate_enabled=False,
                                          dc_removal_enabled=False, target_rms=0.15)
    now = 0.0
    last_meter = None
    for block in af.iter_blocks(quiet_tone):
        now += FRAME_DT
        _, last_meter = conditioner.process(block, frame_dt=FRAME_DT, now=now)
    assert last_meter["gain"] > 1.0, "AGC should have increased gain for a quiet source"
    assert last_meter["output_rms"] == pytest.approx(0.15, rel=0.1)


def test_agc_attenuates_loud_signal_toward_target():
    # amplitude 0.9 -> rms ~0.636; desired_gain = 0.15/0.636 ~= 0.236,
    # comfortably inside the clamp.
    loud_tone = af.make_tone(440, af.duration_for_n_blocks(300), amplitude=0.9)
    conditioner = asig.SignalConditioner(agc_enabled=True, noise_gate_enabled=False,
                                          dc_removal_enabled=False, target_rms=0.15)
    now = 0.0
    last_meter = None
    for block in af.iter_blocks(loud_tone):
        now += FRAME_DT
        _, last_meter = conditioner.process(block, frame_dt=FRAME_DT, now=now)
    assert last_meter["gain"] < 1.0, "AGC should have decreased gain for a loud source"
    assert last_meter["output_rms"] == pytest.approx(0.15, rel=0.1)


def test_agc_respects_min_max_gain_clamps():
    silent = af.make_tone(440, 1.0, amplitude=0.001)  # would want a huge gain
    conditioner = asig.SignalConditioner(agc_enabled=True, noise_gate_enabled=False,
                                          dc_removal_enabled=False, target_rms=0.5,
                                          min_gain=0.1, max_gain=3.0)
    now = 0.0
    for block in af.iter_blocks(silent):
        now += FRAME_DT
        _, meter = conditioner.process(block, frame_dt=FRAME_DT, now=now)
    assert meter["gain"] <= 3.0 + 1e-9


def test_agc_attack_is_faster_than_release():
    # Attack (gain going DOWN for a loud signal) should reach its target in
    # FEWER frames than release (gain going UP for a quiet signal) does for
    # a comparable-magnitude gain change, given attack_ms << release_ms.
    # Both tones are picked so their steady-state desired_gain sits well
    # inside the [0.1, 8.0] clamp (~0.236 and ~4.24 respectively).
    conditioner = asig.SignalConditioner(agc_enabled=True, noise_gate_enabled=False,
                                          dc_removal_enabled=False, target_rms=0.15,
                                          attack_ms=20.0, release_ms=800.0)
    loud_rms = 0.9 / (2 ** 0.5)
    loud_target_gain = 0.15 / loud_rms
    loud = af.make_tone(440, af.duration_for_n_blocks(500), amplitude=0.9)
    now = 0.0
    frames_to_attack_target = None
    for i, block in enumerate(af.iter_blocks(loud)):
        now += FRAME_DT
        _, meter = conditioner.process(block, frame_dt=FRAME_DT, now=now)
        if frames_to_attack_target is None and meter["gain"] <= loud_target_gain * 1.1:
            frames_to_attack_target = i

    conditioner2 = asig.SignalConditioner(agc_enabled=True, noise_gate_enabled=False,
                                           dc_removal_enabled=False, target_rms=0.15,
                                           attack_ms=20.0, release_ms=800.0)
    quiet_rms = 0.05 / (2 ** 0.5)
    quiet_target_gain = 0.15 / quiet_rms
    quiet = af.make_tone(440, af.duration_for_n_blocks(500), amplitude=0.05)
    now = 0.0
    frames_to_release_target = None
    for i, block in enumerate(af.iter_blocks(quiet)):
        now += FRAME_DT
        _, meter = conditioner2.process(block, frame_dt=FRAME_DT, now=now)
        if frames_to_release_target is None and meter["gain"] >= quiet_target_gain * 0.9:
            frames_to_release_target = i

    assert frames_to_attack_target is not None
    assert frames_to_release_target is not None
    assert frames_to_attack_target < frames_to_release_target, (
        f"attack ({frames_to_attack_target} frames) should converge faster than "
        f"release ({frames_to_release_target} frames)"
    )


def test_agc_disabled_leaves_gain_at_unity():
    tone = af.make_tone(440, 0.1, amplitude=0.02)
    conditioner = asig.SignalConditioner(agc_enabled=False, noise_gate_enabled=False, dc_removal_enabled=False)
    conditioned, meter = conditioner.process(tone, frame_dt=FRAME_DT, now=0.0)
    assert meter["gain"] == 1.0
    assert np.allclose(conditioned, tone)


# --------------------------------------------------------------- noise gate --
def test_noise_gate_zeros_signal_below_floor():
    quiet_hum = af.make_white_noise(0.05, amplitude=0.0005, seed=1)  # rms well under the floor
    conditioner = asig.SignalConditioner(noise_gate_enabled=True, noise_gate_floor=0.002,
                                          dc_removal_enabled=False, agc_enabled=False)
    conditioned, meter = conditioner.process(quiet_hum, frame_dt=FRAME_DT, now=0.0)
    assert meter["gated"] is True
    assert np.all(conditioned == 0.0)


def test_noise_gate_passes_signal_above_floor():
    tone = af.make_tone(440, 0.05, amplitude=0.3)
    conditioner = asig.SignalConditioner(noise_gate_enabled=True, noise_gate_floor=0.002,
                                          dc_removal_enabled=False, agc_enabled=False)
    conditioned, meter = conditioner.process(tone, frame_dt=FRAME_DT, now=0.0)
    assert meter["gated"] is False
    assert np.allclose(conditioned, tone)


def test_noise_gate_disabled_never_gates():
    silence = af.make_silence(0.05)
    conditioner = asig.SignalConditioner(noise_gate_enabled=False)
    _, meter = conditioner.process(silence, frame_dt=FRAME_DT, now=0.0)
    assert meter["gated"] is False


def test_noise_gate_configurable_floor_changes_behavior():
    mid_level = af.make_tone(440, 0.05, amplitude=0.01)  # rms ~0.007
    strict = asig.SignalConditioner(noise_gate_enabled=True, noise_gate_floor=0.02, dc_removal_enabled=False)
    lenient = asig.SignalConditioner(noise_gate_enabled=True, noise_gate_floor=0.001, dc_removal_enabled=False)
    _, strict_meter = strict.process(mid_level, frame_dt=FRAME_DT, now=0.0)
    _, lenient_meter = lenient.process(mid_level, frame_dt=FRAME_DT, now=0.0)
    assert strict_meter["gated"] is True
    assert lenient_meter["gated"] is False


# -------------------------------------------------------- clipping/overload --
def test_clip_detection_flags_overloaded_signal():
    hot_tone = af.make_tone(440, 0.05, amplitude=1.0)  # sine at full scale -> rides the rails
    conditioner = asig.SignalConditioner(dc_removal_enabled=False, noise_gate_enabled=False)
    _, meter = conditioner.process(hot_tone, frame_dt=FRAME_DT, now=0.0)
    assert meter["clipping"] is True


def test_clip_detection_does_not_flag_normal_signal():
    normal_tone = af.make_tone(440, 0.05, amplitude=0.5)
    conditioner = asig.SignalConditioner(dc_removal_enabled=False, noise_gate_enabled=False)
    _, meter = conditioner.process(normal_tone, frame_dt=FRAME_DT, now=0.0)
    assert meter["clipping"] is False


def test_clip_detection_on_hard_clipped_signal():
    overdriven = af.clip_signal(af.make_tone(440, 0.05, amplitude=1.8))  # clipped source, like a hot mic input
    conditioner = asig.SignalConditioner(dc_removal_enabled=False, noise_gate_enabled=False)
    _, meter = conditioner.process(overdriven, frame_dt=FRAME_DT, now=0.0)
    assert meter["clipping"] is True


# ------------------------------------------------------------ peak-hold ----
def test_peak_hold_captures_and_holds_after_transient():
    conditioner = asig.SignalConditioner(dc_removal_enabled=False, noise_gate_enabled=False)
    loud_block = af.make_tone(440, FRAME_DT, amplitude=0.9)
    quiet_block = af.make_tone(440, FRAME_DT, amplitude=0.05)

    _, meter1 = conditioner.process(loud_block, frame_dt=FRAME_DT, now=0.0)
    assert meter1["peak_hold"] == pytest.approx(0.9, abs=0.02)

    # Immediately after, a quiet block should NOT drop the hold value yet
    # (barely any time has passed).
    _, meter2 = conditioner.process(quiet_block, frame_dt=FRAME_DT, now=0.001)
    assert meter2["peak_hold"] == pytest.approx(0.9, abs=0.05)

    # After enough elapsed time, the hold should have decayed well below
    # the original peak.
    _, meter3 = conditioner.process(quiet_block, frame_dt=FRAME_DT, now=5.0)
    assert meter3["peak_hold"] < 0.5


def test_peak_hold_tracks_new_higher_peak_immediately():
    conditioner = asig.SignalConditioner(dc_removal_enabled=False, noise_gate_enabled=False)
    conditioner.process(af.make_tone(440, FRAME_DT, amplitude=0.2), frame_dt=FRAME_DT, now=0.0)
    _, meter = conditioner.process(af.make_tone(440, FRAME_DT, amplitude=0.95), frame_dt=FRAME_DT, now=1.0)
    assert meter["peak_hold"] == pytest.approx(0.95, abs=0.02)


# --------------------------------------------------- multi-band independent gain
def test_apply_band_gains_boosts_only_targeted_band():
    conditioner = asig.SignalConditioner(band_gains=[1.0, 1.0, 5.0])  # boost the treble band hard
    bands = {"energies": [10.0, 10.0, 10.0], "fractions": [1 / 3, 1 / 3, 1 / 3]}
    out = conditioner.apply_band_gains(bands)
    assert out["energies"] == [10.0, 10.0, 50.0]
    assert out["fractions"][2] > out["fractions"][0]
    assert sum(out["fractions"]) == pytest.approx(1.0, abs=1e-6)


def test_apply_band_gains_noop_when_length_mismatches():
    conditioner = asig.SignalConditioner(band_gains=[1.0, 2.0])  # 2 gains, but bands has 3 energies
    bands = {"energies": [10.0, 10.0, 10.0], "fractions": [1 / 3, 1 / 3, 1 / 3]}
    out = conditioner.apply_band_gains(bands)
    assert out["energies"] == bands["energies"]


def test_apply_band_gains_noop_when_not_configured():
    conditioner = asig.SignalConditioner(band_gains=None)
    bands = {"energies": [10.0, 20.0], "fractions": [1 / 3, 2 / 3]}
    out = conditioner.apply_band_gains(bands)
    assert out is bands


# ------------------------------------------------------- calibrate-from-silence
def test_compute_noise_floor_from_real_room_silence_sample():
    room_hum = af.make_white_noise(3.0, amplitude=0.0008, seed=7)
    result = asig.compute_noise_floor(room_hum)
    assert result["sample_rms"] > 0
    assert result["noise_gate_floor"] > result["sample_rms"], "floor should include a safety margin above measured rms"


def test_compute_noise_floor_true_silence_uses_min_floor():
    result = asig.compute_noise_floor(af.make_silence(2.0))
    assert result["noise_gate_floor"] == asig.CALIBRATION_MIN_FLOOR
    assert result["sample_rms"] == 0.0


def test_calibrate_from_device_uses_injected_capture_fn():
    captured_args = {}

    def fake_capture(device_index, duration_s, sample_rate):
        captured_args["device_index"] = device_index
        captured_args["duration_s"] = duration_s
        return af.make_white_noise(duration_s, amplitude=0.001, sample_rate=sample_rate, seed=3)

    result = asig.calibrate_from_device(5, duration_s=2.0, capture_fn=fake_capture)
    assert captured_args == {"device_index": 5, "duration_s": 2.0}
    assert result["noise_gate_floor"] > 0


# -------------------------------------------------- per-device saved calibration
def test_save_and_get_device_calibration_roundtrip(cal_reset):
    assert asig.get_device_calibration("Mic (USB)") is None
    saved = asig.save_device_calibration("Mic (USB)", 0.0025, sample_rms=0.0016)
    assert saved["noise_gate_floor"] == 0.0025
    fetched = asig.get_device_calibration("Mic (USB)")
    assert fetched["noise_gate_floor"] == 0.0025
    assert fetched["sample_rms"] == 0.0016


def test_get_device_calibration_unknown_device_returns_none(cal_reset):
    assert asig.get_device_calibration("nonexistent device") is None
    assert asig.get_device_calibration(None) is None


def test_list_calibrations_includes_all_saved_devices(cal_reset):
    asig.save_device_calibration("Device A", 0.001)
    asig.save_device_calibration("Device B", 0.003)
    devices = asig.list_calibrations()
    assert set(devices) == {"Device A", "Device B"}


def test_delete_calibration(cal_reset):
    asig.save_device_calibration("Device A", 0.001)
    assert asig.delete_calibration("Device A") is True
    assert asig.get_device_calibration("Device A") is None
    assert asig.delete_calibration("Device A") is False  # already gone


def test_calibration_persists_across_module_reload_of_state(cal_reset):
    # Simulates a server restart: calibration is read fresh from disk, not
    # just cached in memory.
    asig.save_device_calibration("Persistent Mic", 0.004, sample_rms=0.0025)
    reloaded = asig._load_calibration_state()
    assert reloaded["devices"]["Persistent Mic"]["noise_gate_floor"] == 0.004
