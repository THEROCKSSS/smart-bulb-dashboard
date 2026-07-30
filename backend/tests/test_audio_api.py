"""HTTP-level tests for the new Section 4 signal-conditioning endpoints
(/api/audio/calibrate, /api/audio/calibration) and the validation path of
the existing audio-reactive start endpoint.

Deliberately does NOT call `/api/devices/{id}/audio-reactive/start` with a
real device_index for a successful start: that call spins up a real
background thread that opens an actual `sounddevice.InputStream` against
real hardware (see AudioSession._run in backend/audio_reactive.py) -- out
of scope per this project's "synthetic/loopback signals only" testing rule,
and there's no existing precedent for it in this test suite either (see
test_audio_modes.py, which tests `_apply_mode` directly instead). The
calibrate endpoint IS tested end-to-end, but with `audio_signal`'s capture
function monkeypatched to a synthetic one -- exercising the real endpoint
-> compute -> save path without touching a real microphone.

Run with:
    pytest backend/tests/test_audio_api.py -v
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest  # noqa: E402

import audio_fixtures as af  # noqa: E402
import audio_signal as asig  # noqa: E402
import main as main_module  # noqa: E402


@pytest.fixture
def cal_reset(tmp_path, monkeypatch):
    fake_path = tmp_path / "audio_calibration.json"
    monkeypatch.setattr(asig, "CALIBRATION_PATH", str(fake_path))
    yield


@pytest.fixture
def synthetic_capture(monkeypatch):
    """Replaces audio_signal's real (sounddevice) capture function with a
    synthetic one so /api/audio/calibrate can be exercised end-to-end
    without a real microphone."""
    def fake_capture(device_index, duration_s, sample_rate):
        return af.make_white_noise(duration_s, amplitude=0.001, sample_rate=sample_rate, seed=1)
    monkeypatch.setattr(asig, "_default_capture_fn", fake_capture)
    yield


# -------------------------------------------------------- /api/audio/devices
def test_audio_devices_lists_modes_and_role_modes(client):
    resp = client.get("/api/audio/devices")
    assert resp.status_code == 200
    data = resp.json()
    assert "devices" in data
    assert "band_fixed" in data["modes"]
    assert "unison" in data["role_modes"]
    assert data["default_min_dwell_ms"] > 0


# ------------------------------------------------------- /api/audio/calibrate
def test_calibrate_end_to_end_with_synthetic_capture(client, cal_reset, synthetic_capture):
    resp = client.post("/api/audio/calibrate", json={"device_index": 3, "duration_s": 1.5, "save": True})
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["result"]["noise_gate_floor"] > 0
    assert data["saved"] is not None

    # Should now show up in the calibration list.
    listing = client.get("/api/audio/calibration").json()
    assert data["device_key"] in listing["devices"]


def test_calibrate_without_save_does_not_persist(client, cal_reset, synthetic_capture):
    resp = client.post("/api/audio/calibrate", json={"device_index": 3, "duration_s": 1.0, "save": False})
    assert resp.status_code == 200
    data = resp.json()
    assert data["saved"] is None
    listing = client.get("/api/audio/calibration").json()
    assert listing["devices"] == {}


def test_calibrate_rejects_absurd_duration(client, cal_reset, synthetic_capture):
    resp = client.post("/api/audio/calibrate", json={"device_index": 3, "duration_s": 60})
    assert resp.status_code == 400


def test_calibrate_capture_failure_returns_500(client, cal_reset, monkeypatch):
    def broken_capture(device_index, duration_s, sample_rate):
        raise RuntimeError("no such device")
    monkeypatch.setattr(asig, "_default_capture_fn", broken_capture)
    resp = client.post("/api/audio/calibrate", json={"device_index": 999, "duration_s": 1.0})
    assert resp.status_code == 500


# ----------------------------------------------------- /api/audio/calibration
def test_calibration_list_and_delete(client, cal_reset):
    asig.save_device_calibration("Some Mic", 0.002, sample_rms=0.001)
    listing = client.get("/api/audio/calibration").json()
    assert "Some Mic" in listing["devices"]

    resp = client.delete("/api/audio/calibration/Some Mic")
    assert resp.status_code == 200
    assert client.get("/api/audio/calibration").json()["devices"] == {}


def test_delete_nonexistent_calibration_404s(client, cal_reset):
    resp = client.delete("/api/audio/calibration/nope")
    assert resp.status_code == 404


# ------------------------------------------ audio-reactive/start validation
# These exercise ONLY the validation path (bad mode / bad dwell), which
# returns before audio_reactive.start_session ever touches real hardware.
def test_audio_reactive_start_rejects_unknown_mode(client):
    resp = client.post("/api/devices/bulb-1/audio-reactive/start", json={
        "device_index": 0, "mode": "not_a_real_mode",
    })
    assert resp.status_code == 400
    assert "unknown mode" in resp.json()["detail"]


def test_audio_reactive_start_rejects_dwell_below_floor(client):
    import audio_reactive as ar
    resp = client.post("/api/devices/bulb-1/audio-reactive/start", json={
        "device_index": 0, "mode": "band_fixed", "min_dwell_ms": ar.MIN_DWELL_FLOOR_MS - 1,
    })
    assert resp.status_code == 400
    assert "safety floor" in resp.json()["detail"]


def test_audio_reactive_start_404s_for_unknown_device(client):
    resp = client.post("/api/devices/does-not-exist/audio-reactive/start", json={
        "device_index": 0, "mode": "band_fixed",
    })
    assert resp.status_code == 404


def test_audio_reactive_start_body_accepts_new_signal_fields_schema():
    # Confirms the new optional signal-conditioning fields exist with the
    # documented defaults, without invoking the endpoint (no hardware
    # touched) -- a pure Pydantic-model schema check.
    body = main_module.AudioReactiveStartBody(device_index=0)
    assert body.agc_enabled is False
    assert body.noise_gate_enabled is True
    assert body.dc_removal_enabled is True
    assert body.noise_gate_floor is None
    assert body.band_gains is None
    assert body.use_saved_calibration is True


# ---------------------------------------------------- device_key resolution
def test_device_key_for_index_resolves_real_name_or_falls_back():
    devices = main_module.audio_reactive.list_input_devices()
    if devices:
        key = main_module._device_key_for_index(devices[0]["index"])
        assert key == devices[0]["name"]
    # An index that (almost certainly) doesn't exist falls back to the
    # stringified index rather than raising.
    fallback_key = main_module._device_key_for_index(999999)
    assert fallback_key == "999999"
