"""Week 1 Phase D, section 8/9/14: unit-level tests for session-management
helpers that don't need a live audio stream -- config validation, warmup
ramp, applause/cheer detection, device-fallback resolution, conflict
checks, and manual-command auto-pause/resume. These construct `AudioSession`
objects directly (never calling `.start()`, so no real sounddevice stream
is opened) to exercise pause/resume/confirmation/status logic cheaply.
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import audio_reactive as ar  # noqa: E402


class FakeController:
    def __init__(self, device_id):
        self.cfg = {"id": device_id}
        self.calls = []

    def set_socket_timeout(self, seconds):
        pass

    def set_hsv(self, h, s, v):
        self.calls.append(("hsv", h, s, v))

    def set_rgb(self, r, g, b):
        self.calls.append(("rgb", r, g, b))

    def set_brightness(self, pct):
        self.calls.append(("brightness", pct))

    def stop_effect(self):
        pass

    def power(self, on):
        self.calls.append(("power", on))

    def _log(self, action, params=None, ok=True, error=None):
        pass


# --------------------------------------------------- config validation ---
def test_validate_start_config_rejects_out_of_range_n_bands():
    try:
        ar.validate_start_config(n_bands=2, min_dwell_ms=90)
        assert False, "n_bands below the floor should raise"
    except ar.AudioConfigError as e:
        assert "n_bands" in str(e)

    try:
        ar.validate_start_config(n_bands=17, min_dwell_ms=90)
        assert False, "n_bands above the ceiling should raise"
    except ar.AudioConfigError:
        pass

    ar.validate_start_config(n_bands=3, min_dwell_ms=90)  # must not raise
    ar.validate_start_config(n_bands=16, min_dwell_ms=90)  # must not raise


def test_validate_start_config_rejects_out_of_range_min_dwell_ms():
    try:
        ar.validate_start_config(n_bands=3, min_dwell_ms=1)
        assert False, "below the safety floor should raise"
    except ar.AudioConfigError as e:
        assert "min_dwell_ms" in str(e)

    try:
        ar.validate_start_config(n_bands=3, min_dwell_ms=999999)
        assert False, "above the sane ceiling should raise"
    except ar.AudioConfigError:
        pass


def test_validate_start_config_rejects_bad_max_duration_and_warmup():
    try:
        ar.validate_start_config(n_bands=3, min_dwell_ms=90, max_duration_s=-5)
        assert False
    except ar.AudioConfigError as e:
        assert "max_duration_s" in str(e)

    try:
        ar.validate_start_config(n_bands=3, min_dwell_ms=90, warmup_s=99999)
        assert False
    except ar.AudioConfigError as e:
        assert "warmup_s" in str(e)


def test_validate_start_config_rejects_flash_heavy_mode_when_disabled():
    try:
        ar.validate_start_config(n_bands=3, min_dwell_ms=90, mode="strobe_on_drop", disable_flash_heavy=True)
        assert False, "a flash-heavy mode combined with disable_flash_heavy should raise"
    except ar.AudioConfigError as e:
        assert "flash-heavy" in str(e)
    # An ambient mode is fine even with the toggle on.
    ar.validate_start_config(n_bands=3, min_dwell_ms=90, mode="breathing_silence", disable_flash_heavy=True)


# --------------------------------------------------------- warmup ramp ---
def test_apply_warmup_scales_brightness_during_window_and_passes_through_after():
    action = ("hsv", 100, 100, 80)
    at_start = ar.apply_warmup(action, elapsed_s=0.0, warmup_s=10.0)
    assert at_start[3] == 0.0
    halfway = ar.apply_warmup(action, elapsed_s=5.0, warmup_s=10.0)
    assert abs(halfway[3] - 40.0) < 0.01
    after = ar.apply_warmup(action, elapsed_s=15.0, warmup_s=10.0)
    assert after == action  # unchanged once warmup window has elapsed
    disabled = ar.apply_warmup(action, elapsed_s=2.0, warmup_s=0)
    assert disabled == action  # warmup_s=0 means no ramp at all

    rgb_action = ("rgb_brightness", 255, 0, 0, 80)
    scaled = ar.apply_warmup(rgb_action, elapsed_s=5.0, warmup_s=10.0)
    assert abs(scaled[4] - 40.0) < 0.01
    assert scaled[1:4] == (255, 0, 0)  # color untouched, only brightness ramps


# ------------------------------------------------- applause detection ---
def test_detect_applause_fires_on_broadband_loud_burst_not_bass_kick():
    ctx = ar._new_ctx(sensitivity=1.0, monochrome_hue=280.0)
    now = time.time()
    # Warm up the rolling rms baseline with quiet, broadband-ish frames.
    for i in range(25):
        quiet_bands = {"rms": 0.01, "fractions": [0.34, 0.33, 0.33]}
        assert ar.detect_applause(quiet_bands, ctx, now + i * 0.01) is False

    # A broadband loud burst (energy spread roughly evenly across bands).
    loud_broadband = {"rms": 0.2, "fractions": [0.35, 0.33, 0.32]}
    assert ar.detect_applause(loud_broadband, ctx, now + 1.0) is True

    # Immediately after, the cooldown must suppress a second trigger.
    assert ar.detect_applause(loud_broadband, ctx, now + 1.05) is False


def test_detect_applause_does_not_fire_on_a_bass_dominant_kick():
    ctx = ar._new_ctx(sensitivity=1.0, monochrome_hue=280.0)
    now = time.time()
    for i in range(25):
        quiet_bands = {"rms": 0.01, "fractions": [0.34, 0.33, 0.33]}
        ar.detect_applause(quiet_bands, ctx, now + i * 0.01)

    # A loud but bass-DOMINATED hit (one band carries almost everything) --
    # this is what a kick drum looks like, not applause, and must not
    # trigger the one-shot flash.
    kick = {"rms": 0.2, "fractions": [0.95, 0.03, 0.02]}
    assert ar.detect_applause(kick, ctx, now + 1.0) is False


# ------------------------------------------------- device resolution -----
def test_resolve_device_index_falls_back_on_invalid_index():
    # An absurd index no real machine will have -> must fall back.
    index, used_fallback = ar.resolve_device_index(999999, fallback_index=None)
    assert used_fallback is True
    assert index is None  # None tells sounddevice to use the system default


# ------------------------------------------------- pause / resume --------
def test_pause_for_manual_suppresses_then_auto_resumes(monkeypatch):
    controller = FakeController("bulb-1")
    session = ar.AudioSession(controller, device_index=None, mode="vu_meter")
    assert session._check_paused() is False

    session.pause_for_manual(grace_s=0.15)
    assert session._check_paused() is True
    time.sleep(0.2)
    assert session._check_paused() is False  # grace period elapsed -> auto-resumed

    status_before_pause = session.status()
    assert status_before_pause["paused"] is False


def test_confirmation_reports_exactly_what_was_applied():
    controller = FakeController("bulb-1")
    session = ar.AudioSession(controller, device_index=None, mode="band_flash_overlay",
                               n_bands=6, min_dwell_ms=120, max_duration_s=600, warmup_s=2.0)
    confirmation = session.confirmation()
    assert confirmation["device_id"] == "bulb-1"
    assert confirmation["mode"] == "band_flash_overlay"
    assert confirmation["n_bands"] == 6
    assert confirmation["min_dwell_ms"] == 120
    assert confirmation["max_duration_s"] == 600
    assert confirmation["warmup_s"] == 2.0


def test_invalid_mode_falls_back_to_band_fixed_not_a_crash():
    controller = FakeController("bulb-1")
    session = ar.AudioSession(controller, device_index=None, mode="not_a_real_mode")
    assert session.mode == "band_fixed"


# --------------------------------------------------------- conflicts -----
def test_group_and_solo_conflict_checks(monkeypatch):
    class FakeAliveSession:
        def is_alive(self):
            return True

    ar._sessions.clear()
    ar._group_sessions.clear()
    try:
        ar._sessions["bulb-1"] = FakeAliveSession()

        class FakeGroupSession:
            def __init__(self, controllers):
                self.controllers = controllers

            def is_alive(self):
                return True

        ar._group_sessions["all"] = FakeGroupSession([FakeController("bulb-2"), FakeController("bulb-3")])

        assert ar.check_group_conflict(["bulb-1", "bulb-2"]) == ["bulb-1"]
        assert ar.check_solo_conflict("bulb-2") == ["all"]
        assert ar.check_solo_conflict("bulb-1") == []
    finally:
        ar._sessions.clear()
        ar._group_sessions.clear()


# ------------------------------------------------------ rate limiting ----
def test_check_rate_limit_blocks_after_max_calls_then_recovers():
    key = "test-rate-limit-key-unique"
    ar._rate_limit_hits.pop(key, None)
    for _ in range(ar.RATE_LIMIT_MAX_CALLS):
        assert ar.check_rate_limit(key, max_calls=ar.RATE_LIMIT_MAX_CALLS, window_s=1.0) is True
    assert ar.check_rate_limit(key, max_calls=ar.RATE_LIMIT_MAX_CALLS, window_s=1.0) is False
    time.sleep(1.05)
    assert ar.check_rate_limit(key, max_calls=ar.RATE_LIMIT_MAX_CALLS, window_s=1.0) is True
