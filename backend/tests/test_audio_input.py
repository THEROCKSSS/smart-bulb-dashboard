"""Audio input source expansion (Week 1 Phase C, Section 11):
  - per-input-device-index sensitivity calibration, persisted in
    config.json and auto-applied when that device_index is used to start a
    session (unless the caller passes an explicit sensitivity),
  - a device health-check endpoint that tests basic capture without
    starting a full session,
  - graceful, actionable errors when a saved device_index no longer
    matches any connected device (instead of silently starting a session
    that can never produce audio).

`sounddevice.query_devices` / `sounddevice.InputStream` are monkeypatched
throughout -- this dev box's real input device list shouldn't determine
whether these tests pass, and a background InputStream is not something
a unit test should actually open.
"""
import numpy as np
import pytest

import audio_reactive as ar


class FakeDeviceList:
    """Mimics sd.query_devices()'s return shape (a list-like of per-device
    dicts) closely enough for validate_device_index/device_health_check."""

    def __init__(self, devices):
        self._devices = devices

    def __len__(self):
        return len(self._devices)

    def __getitem__(self, i):
        return self._devices[i]


FAKE_DEVICES = [
    {"name": "Built-in Mic", "max_input_channels": 1, "default_samplerate": 44100.0},
    {"name": "USB Line-In", "max_input_channels": 2, "default_samplerate": 44100.0},
    {"name": "Speakers (output only)", "max_input_channels": 0, "default_samplerate": 44100.0},
]


@pytest.fixture
def fake_sd_devices(monkeypatch):
    def query_devices(index=None):
        if index is None:
            return FakeDeviceList(FAKE_DEVICES)
        return FAKE_DEVICES[index]
    monkeypatch.setattr(ar.sd, "query_devices", query_devices)
    return FAKE_DEVICES


# --------------------------------------------------------- validate_device_index
def test_validate_device_index_ok_for_real_input_device(fake_sd_devices):
    ok, err = ar.validate_device_index(0)
    assert ok is True
    assert err is None


def test_validate_device_index_rejects_out_of_range_index(fake_sd_devices):
    ok, err = ar.validate_device_index(99)
    assert ok is False
    assert "99" in err
    assert "not found" in err


def test_validate_device_index_rejects_negative_index(fake_sd_devices):
    ok, err = ar.validate_device_index(-1)
    assert ok is False


def test_validate_device_index_rejects_output_only_device(fake_sd_devices):
    ok, err = ar.validate_device_index(2)  # "Speakers (output only)"
    assert ok is False
    assert "no input channels" in err


def test_validate_device_index_gives_actionable_message_when_query_fails(monkeypatch):
    def broken_query_devices(index=None):
        raise OSError("PortAudio not initialized")
    monkeypatch.setattr(ar.sd, "query_devices", broken_query_devices)
    ok, err = ar.validate_device_index(0)
    assert ok is False
    assert "could not query audio devices" in err


# ------------------------------------------------------------- health check
def test_device_health_check_reports_ok_when_capture_produces_frames(fake_sd_devices, monkeypatch):
    class FakeStream:
        def __init__(self, device, channels, samplerate, blocksize, callback):
            self.callback = callback

        def __enter__(self):
            # Simulate one real callback firing with actual audio data.
            samples = (np.random.rand(ar.BLOCK_SIZE, 2).astype(np.float64) - 0.5) * 0.2
            self.callback(samples, ar.BLOCK_SIZE, None, None)
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(ar.sd, "InputStream", FakeStream)
    monkeypatch.setattr(ar.time, "sleep", lambda s: None)

    result = ar.device_health_check(1, duration_s=0.01)
    assert result["ok"] is True
    assert result["device_index"] == 1
    assert result["frames_captured"] == ar.BLOCK_SIZE
    assert result["channels_tested"] == 2
    assert result["peak_amplitude"] > 0
    assert result["silent"] is False


def test_device_health_check_reports_silent_when_capture_is_near_zero(fake_sd_devices, monkeypatch):
    class SilentStream:
        def __init__(self, device, channels, samplerate, blocksize, callback):
            self.callback = callback

        def __enter__(self):
            self.callback(np.zeros((ar.BLOCK_SIZE, 1), dtype=np.float64), ar.BLOCK_SIZE, None, None)
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(ar.sd, "InputStream", SilentStream)
    monkeypatch.setattr(ar.time, "sleep", lambda s: None)

    result = ar.device_health_check(0, duration_s=0.01)
    assert result["ok"] is True  # frames WERE captured
    assert result["silent"] is True  # but they're all ~zero amplitude


def test_device_health_check_fails_fast_for_unknown_device_without_opening_a_stream(fake_sd_devices, monkeypatch):
    opened = {"called": False}

    class ShouldNotOpen:
        def __init__(self, *a, **kw):
            opened["called"] = True

    monkeypatch.setattr(ar.sd, "InputStream", ShouldNotOpen)
    result = ar.device_health_check(99)
    assert result["ok"] is False
    assert "not found" in result["error"]
    assert opened["called"] is False, "must not attempt to open a stream against an invalid device index"


def test_device_health_check_reports_stream_open_errors_cleanly(fake_sd_devices, monkeypatch):
    class ExplodingStream:
        def __init__(self, *a, **kw):
            raise OSError("device is busy")

    monkeypatch.setattr(ar.sd, "InputStream", ExplodingStream)
    result = ar.device_health_check(0, duration_s=0.01)
    assert result["ok"] is False
    assert "device is busy" in result["error"]


# ------------------------------------------------------- API-level routes
def test_health_endpoint_returns_result_from_device_health_check(client, monkeypatch):
    import audio_reactive as ar_module

    monkeypatch.setattr(ar_module, "device_health_check",
                         lambda idx: {"ok": True, "device_index": idx, "frames_captured": 512})
    resp = client.get("/api/audio/devices/3/health")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "device_index": 3, "frames_captured": 512}


def test_audio_reactive_start_rejects_invalid_device_index_with_400(client, fake_tuya, monkeypatch):
    import audio_reactive as ar_module
    monkeypatch.setattr(ar_module, "validate_device_index", lambda idx: (False, f"device {idx} not found"))
    resp = client.post("/api/devices/bulb-1/audio-reactive/start", json={"device_index": 42})
    assert resp.status_code == 400
    assert "not found" in resp.json()["detail"]


def test_group_audio_reactive_start_rejects_invalid_device_index_with_400(client, fake_config, monkeypatch):
    import audio_reactive as ar_module
    monkeypatch.setattr(ar_module, "validate_device_index", lambda idx: (False, "unplugged since reboot"))
    resp = client.post("/api/groups/all/audio-reactive/start", json={"device_index": 7})
    assert resp.status_code == 400
    assert "unplugged" in resp.json()["detail"]


# ------------------------------------------------------- calibration CRUD
def test_calibration_starts_empty(client):
    assert client.get("/api/audio/calibrations").json() == []


def test_set_and_list_calibration(client):
    resp = client.put("/api/audio/devices/2/calibration", json={"sensitivity": 1.7, "name": "USB Mic"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["device_index"] == 2
    assert body["sensitivity"] == 1.7
    assert body["name"] == "USB Mic"

    listed = client.get("/api/audio/calibrations").json()
    assert len(listed) == 1
    assert listed[0]["sensitivity"] == 1.7


def test_set_calibration_rejects_out_of_range_sensitivity(client):
    resp = client.put("/api/audio/devices/2/calibration", json={"sensitivity": 9.9})
    assert resp.status_code == 400


def test_delete_calibration(client):
    client.put("/api/audio/devices/2/calibration", json={"sensitivity": 1.7})
    del_resp = client.delete("/api/audio/devices/2/calibration")
    assert del_resp.status_code == 200
    assert client.get("/api/audio/calibrations").json() == []


def test_audio_reactive_start_auto_applies_saved_calibration(client, fake_tuya, monkeypatch):
    import audio_reactive as ar_module
    monkeypatch.setattr(ar_module, "validate_device_index", lambda idx: (True, None))

    captured = {}

    def fake_start_session(controller, device_index, mode, sensitivity, monochrome_hue, n_bands, min_dwell_ms):
        captured["sensitivity"] = sensitivity
        return object()

    monkeypatch.setattr(ar_module, "start_session", fake_start_session)

    client.put("/api/audio/devices/5/calibration", json={"sensitivity": 2.25})
    resp = client.post("/api/devices/bulb-1/audio-reactive/start", json={"device_index": 5})
    assert resp.status_code == 200
    assert resp.json()["sensitivity"] == 2.25
    assert captured["sensitivity"] == 2.25


def test_audio_reactive_start_explicit_sensitivity_overrides_calibration(client, fake_tuya, monkeypatch):
    import audio_reactive as ar_module
    monkeypatch.setattr(ar_module, "validate_device_index", lambda idx: (True, None))

    captured = {}

    def fake_start_session(controller, device_index, mode, sensitivity, monochrome_hue, n_bands, min_dwell_ms):
        captured["sensitivity"] = sensitivity
        return object()

    monkeypatch.setattr(ar_module, "start_session", fake_start_session)

    client.put("/api/audio/devices/5/calibration", json={"sensitivity": 2.25})
    resp = client.post("/api/devices/bulb-1/audio-reactive/start",
                        json={"device_index": 5, "sensitivity": 1.0})
    assert resp.status_code == 200
    assert captured["sensitivity"] == 1.0, "an explicit sensitivity (even 1.0) must win over saved calibration"


def test_audio_reactive_start_defaults_to_1_0_when_no_calibration_saved(client, fake_tuya, monkeypatch):
    import audio_reactive as ar_module
    monkeypatch.setattr(ar_module, "validate_device_index", lambda idx: (True, None))

    captured = {}

    def fake_start_session(controller, device_index, mode, sensitivity, monochrome_hue, n_bands, min_dwell_ms):
        captured["sensitivity"] = sensitivity
        return object()

    monkeypatch.setattr(ar_module, "start_session", fake_start_session)

    resp = client.post("/api/devices/bulb-1/audio-reactive/start", json={"device_index": 9})
    assert resp.status_code == 200
    assert captured["sensitivity"] == 1.0
