"""Saved input identity survives renumbering and never selects an unrelated mic."""
import importlib.util
from pathlib import Path
import pytest

spec = importlib.util.spec_from_file_location("bridge_tool", Path(__file__).resolve().parents[2] / "tools" / "sbd-audio-bridge.py")
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


def test_remembered_input_survives_index_change(tmp_path, monkeypatch):
    path = tmp_path / "bridge.json"
    path.write_text('{"name":"CABLE Output (VB-Audio Virtual Cable)","hostapi":"Windows WASAPI"}')
    monkeypatch.setattr(bridge, "DEVICE_STATE", path)
    devices = [{"index": 105, "name": "CABLE Output (VB-Audio Virtual Cable)", "hostapi": "Windows WASAPI"}]
    assert bridge.preferred_device(devices) == 105
    with pytest.raises(OSError, match="unavailable"):
        bridge.preferred_device([{"index": 86, "name": "Microphone", "hostapi": "Windows WASAPI"}])


def test_first_run_prefers_cable_even_when_mic_is_first(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "DEVICE_STATE", tmp_path / "absent.json")
    assert bridge.preferred_device([
        {"index": 1, "name": "Microphone", "hostapi": "Windows WASAPI"},
        {"index": 9, "name": "CABLE Output (VB-Audio Virtual Cable)", "hostapi": "Windows WASAPI"},
    ]) == 9


def test_raw_nonpreferred_api_identity_is_not_lost_to_deduplication(tmp_path, monkeypatch):
    path = tmp_path / 'bridge.json'
    path.write_text('{"name":"CABLE Output short","hostapi":"MME"}')
    monkeypatch.setattr(bridge, 'DEVICE_STATE', path)
    monkeypatch.setattr(bridge, '_hostapi_names', lambda: {0: 'MME', 1: 'Windows WASAPI'})
    monkeypatch.setattr(bridge.sd, 'query_devices', lambda: [
        {'name': 'CABLE Output short', 'hostapi': 0, 'max_input_channels': 2},
        {'name': 'CABLE Output longer', 'hostapi': 1, 'max_input_channels': 2},
    ])
    assert bridge.preferred_device() == 0


def test_channel_capacity_recovers_after_switching_back_to_stereo(monkeypatch):
    monkeypatch.setattr(bridge, 'native_rate', lambda device: 48000)
    channels = {'value': 1}
    monkeypatch.setattr(bridge.sd, 'query_devices', lambda device: {'name': 'Test', 'max_input_channels': channels['value']})
    opened = []
    class Opened(Exception): pass
    def capture(**kwargs):
        opened.append(kwargs['channels'])
        raise Opened()
    monkeypatch.setattr(bridge.sd, 'InputStream', capture)
    streamer = bridge.Streamer(1, '127.0.0.1', 8503, 2)
    for count in (1, 2):
        channels['value'] = count
        with pytest.raises(Opened): streamer.run_once()
    assert opened == [1, 2]
