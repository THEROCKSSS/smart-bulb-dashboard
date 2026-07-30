"""Week 1 Phase D, section 8: session preset save/load round-trip, and
last-known-good session persistence ('resume last session after a
restart')."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import audio_presets  # noqa: E402


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(audio_presets, "SESSION_PRESETS_PATH", str(tmp_path / "audio_session_presets.json"))
    monkeypatch.setattr(audio_presets, "LAST_SESSION_PATH", str(tmp_path / "audio_last_session.json"))


def test_save_and_list_preset_round_trips(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    config = {
        "device_index": 3, "mode": "spectrum_gradient", "sensitivity": 1.4,
        "monochrome_hue": 210.0, "n_bands": 6, "min_dwell_ms": 120,
        "max_duration_s": 3600, "warmup_s": 5.0,
    }
    saved = audio_presets.save_preset("Party Mode", "bulb-1", config)
    assert saved["name"] == "Party Mode"
    assert saved["device_id"] == "bulb-1"
    assert saved["config"]["mode"] == "spectrum_gradient"
    assert saved["config"]["n_bands"] == 6

    listed = audio_presets.list_presets()
    assert len(listed) == 1
    assert listed[0]["id"] == saved["id"]

    fetched = audio_presets.get_preset(saved["id"])
    assert fetched == saved

    scoped = audio_presets.list_presets(device_id="bulb-1")
    assert len(scoped) == 1
    scoped_other = audio_presets.list_presets(device_id="bulb-2")
    assert scoped_other == []


def test_save_preset_drops_unknown_fields_and_keeps_known_ones(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    config = {"mode": "vu_meter", "n_bands": 3, "min_dwell_ms": 90, "totally_unknown_field": "nope"}
    saved = audio_presets.save_preset("Simple", "bulb-1", config)
    assert "totally_unknown_field" not in saved["config"]
    assert saved["config"]["mode"] == "vu_meter"


def test_delete_preset(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    saved = audio_presets.save_preset("Temp", "bulb-1", {"mode": "vu_meter", "n_bands": 3, "min_dwell_ms": 90})
    assert audio_presets.delete_preset(saved["id"]) is True
    assert audio_presets.get_preset(saved["id"]) is None
    assert audio_presets.delete_preset(saved["id"]) is False  # already gone


def test_last_session_round_trip_for_resume_after_restart(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    assert audio_presets.load_last_session("bulb-1") is None

    config = {"device_index": 2, "mode": "band_fixed", "n_bands": 3, "min_dwell_ms": 90}
    audio_presets.save_last_session("bulb-1", config)

    loaded = audio_presets.load_last_session("bulb-1")
    assert loaded is not None
    assert loaded["config"]["mode"] == "band_fixed"
    assert loaded["device_id"] == "bulb-1"

    # Scoping to a different device_id must not return another device's record.
    assert audio_presets.load_last_session("bulb-2") is None

    # A newer save overwrites the previous last-known-good record (only one
    # kept, matching the "last" session, not full history).
    audio_presets.save_last_session("bulb-1", {"mode": "vu_meter", "n_bands": 3, "min_dwell_ms": 90})
    assert audio_presets.load_last_session("bulb-1")["config"]["mode"] == "vu_meter"

    audio_presets.clear_last_session()
    assert audio_presets.load_last_session("bulb-1") is None
