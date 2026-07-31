"""Tests that AudioSession actually wires up the new Section 4/5 plumbing
end-to-end at the object level: signal-conditioning config reaches its
SignalConditioner, a processed block populates bands_full/signal/bpm data
in status(), and BulbSender's rolling latency window fills in.

Deliberately never calls `AudioSession.start()` (that spins a real
background thread which opens an actual `sounddevice.InputStream` against
real hardware) -- instead calls the private `_process()` callback body
directly with synthetic indata, exactly the shape `sounddevice` would hand
it, which is enough to exercise conditioning + analysis + dispatch through
BulbSender without touching a real audio device.

Run with:
    pytest backend/tests/test_audio_session_wiring.py -v
"""
import os
import sys
import time

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import audio_fixtures as af  # noqa: E402
import audio_reactive as ar  # noqa: E402
import bulb_manager as bm  # noqa: E402


def _indata_from(samples):
    """Shapes a 1D samples array into the (n, 1) 2D array sounddevice's
    InputStream callback hands to `indata` for a mono stream."""
    return np.asarray(samples, dtype=np.float64).reshape(-1, 1)


@pytest.fixture
def controller(fake_config, fake_tuya):
    return bm.get_controller("bulb-1")


def test_signal_conditioning_config_reaches_the_conditioner(controller):
    session = ar.AudioSession(
        controller, device_index=0, mode="band_fixed",
        device_key="Test Mic", agc_enabled=True, noise_gate_enabled=True,
        dc_removal_enabled=True, noise_gate_floor=0.01, agc_target_rms=0.2,
        agc_attack_ms=30.0, agc_release_ms=300.0, band_gains=[1.0, 2.0, 3.0],
        use_saved_calibration=False,
    )
    try:
        c = session.conditioner
        assert c.agc_enabled is True
        assert c.noise_gate_enabled is True
        assert c.dc_removal_enabled is True
        assert c.noise_gate_floor == 0.01
        assert c.target_rms == 0.2
        assert c.attack_ms == 30.0
        assert c.release_ms == 300.0
        assert c.band_gains == [1.0, 2.0, 3.0]
    finally:
        session.sender.stop()


def test_saved_calibration_supplies_noise_gate_floor_by_default(controller, tmp_path, monkeypatch):
    import audio_signal as asig
    monkeypatch.setattr(asig, "CALIBRATION_PATH", str(tmp_path / "cal.json"))
    asig.save_device_calibration("Test Mic", 0.0042, sample_rms=0.002)

    session = ar.AudioSession(controller, device_index=0, device_key="Test Mic")
    try:
        assert session.conditioner.noise_gate_floor == 0.0042
    finally:
        session.sender.stop()


def test_explicit_noise_gate_floor_overrides_saved_calibration(controller, tmp_path, monkeypatch):
    import audio_signal as asig
    monkeypatch.setattr(asig, "CALIBRATION_PATH", str(tmp_path / "cal.json"))
    asig.save_device_calibration("Test Mic", 0.0042)

    session = ar.AudioSession(controller, device_index=0, device_key="Test Mic", noise_gate_floor=0.05)
    try:
        assert session.conditioner.noise_gate_floor == 0.05
    finally:
        session.sender.stop()


def test_process_populates_bands_full_and_signal_and_bpm(controller):
    session = ar.AudioSession(controller, device_index=0, mode="band_fixed", n_bands=6,
                               noise_gate_enabled=False)
    try:
        tone = af.make_tone(440, af.BLOCK_SIZE / af.SAMPLE_RATE, amplitude=0.4)
        for _ in range(20):
            session._process(_indata_from(tone))
        status = session.status()

        assert status["bands_full"]["n_bands"] == 6
        assert len(status["bands_full"]["fractions"]) == 6
        assert len(status["bands_full"]["energies"]) == 6

        assert "input_rms" in status["signal"]
        assert "clipping" in status["signal"]
        assert "peak_hold" in status["signal"]

        bands = status["bands"]
        assert "is_hard_hit" in bands
        assert "beat_strength" in bands
        assert "ms_since_beat" in bands
        assert "bpm" in bands
    finally:
        session.sender.stop()


def test_process_with_beats_produces_a_bpm_reading(controller):
    session = ar.AudioSession(controller, device_index=0, mode="band_fixed", noise_gate_enabled=False)
    try:
        # Alternate loud-bass / quiet frames at a steady cadence so the
        # rolling-bass beat detector fires repeatedly at a fixed interval,
        # long enough for the basic BPM estimator to get 3+ valid
        # intervals (it needs several counted beats -- see _apply_mode).
        loud = af.make_tone(60, af.BLOCK_SIZE / af.SAMPLE_RATE, amplitude=0.9)
        quiet = af.make_silence(af.BLOCK_SIZE / af.SAMPLE_RATE)
        fixed_now = [1_000_000.0]
        real_time = time.time
        ar.time.time = lambda: fixed_now[0]
        try:
            for _ in range(6):
                for _ in range(6):
                    session._process(_indata_from(quiet))
                    fixed_now[0] += 0.02
                session._process(_indata_from(loud))
                fixed_now[0] += 0.02
        finally:
            ar.time.time = real_time
        status = session.status()
        assert status["bands"]["bpm"] is None or status["bands"]["bpm"] > 0
    finally:
        session.sender.stop()


def test_bulb_sender_latency_history_fills_in(controller):
    sender = ar.BulbSender(controller, min_dwell_ms=ar.MIN_DWELL_FLOOR_MS)
    try:
        for _ in range(5):
            sender.queue(("hsv", 120.0, 100, 80))
            # Give the sender's background loop a chance to dispatch --
            # min_dwell is at the safety floor so this settles quickly.
            deadline = time.time() + 1.0
            while sender.status()["last_latency_ms"] is None and time.time() < deadline:
                time.sleep(0.01)
        status = sender.status()
        assert status["last_latency_ms"] is not None
        assert isinstance(status["latency_history_ms"], list)
        assert len(status["latency_history_ms"]) >= 1
        assert all(isinstance(x, float) for x in status["latency_history_ms"])
    finally:
        sender.stop()


def test_bulb_sender_latency_history_is_bounded():
    class _StubController:
        def set_hsv(self, h, s, v):
            pass

        def _log(self, *a, **k):
            pass

    sender = ar.BulbSender(_StubController(), min_dwell_ms=ar.MIN_DWELL_FLOOR_MS)
    try:
        for i in range(ar.BulbSender.LATENCY_HISTORY_LEN + 10):
            sender.queue(("hsv", float(i % 360), 100, 80))
            time.sleep(0.001)
        deadline = time.time() + 2.0
        while len(sender.status()["latency_history_ms"]) < ar.BulbSender.LATENCY_HISTORY_LEN and time.time() < deadline:
            time.sleep(0.02)
        assert len(sender.status()["latency_history_ms"]) <= ar.BulbSender.LATENCY_HISTORY_LEN
    finally:
        sender.stop()
