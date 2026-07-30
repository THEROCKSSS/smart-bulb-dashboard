"""Week 1 Phase D, section 13: the photosensitive-epilepsy safety cap.

These tests exercise the real `audio_safety.apply_flash_cap` /
`clamp_max_flash_rate` functions with real elapsed wall-clock time (short
sleeps, not mocked), because the cap is explicitly a *rate* limit -- the
only convincing way to prove it holds is to actually flash faster than
allowed and measure how many flashes got through.
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import audio_safety  # noqa: E402


def test_clamp_max_flash_rate_never_exceeds_hard_ceiling():
    # However high a caller asks for, the effective ceiling is <= 3.0Hz.
    assert audio_safety.clamp_max_flash_rate(1000) == audio_safety.HARD_MAX_FLASH_RATE_HZ
    assert audio_safety.clamp_max_flash_rate(3.0) == 3.0
    assert audio_safety.clamp_max_flash_rate(1.5) == 1.5
    # A silly low/garbage value is floored, not allowed to go to 0 (which
    # would be misinterpreted as "no limit" by 1/rate math elsewhere).
    assert audio_safety.clamp_max_flash_rate(0) >= audio_safety.MIN_FLASH_RATE_HZ
    assert audio_safety.clamp_max_flash_rate(-5) >= audio_safety.MIN_FLASH_RATE_HZ
    assert audio_safety.clamp_max_flash_rate("garbage") == audio_safety.HARD_MAX_FLASH_RATE_HZ


def test_set_max_flash_rate_persists_but_is_still_clamped(tmp_path, monkeypatch):
    fake_path = tmp_path / "audio_safety.json"
    monkeypatch.setattr(audio_safety, "SAFETY_SETTINGS_PATH", str(fake_path))
    result = audio_safety.set_max_flash_rate(50.0)  # way above the hard ceiling
    assert result["max_flash_rate_hz"] == audio_safety.HARD_MAX_FLASH_RATE_HZ
    reloaded = audio_safety.get_safety_settings()
    assert reloaded["max_flash_rate_hz"] == audio_safety.HARD_MAX_FLASH_RATE_HZ


def test_set_disable_flash_heavy_toggle_persists(tmp_path, monkeypatch):
    fake_path = tmp_path / "audio_safety.json"
    monkeypatch.setattr(audio_safety, "SAFETY_SETTINGS_PATH", str(fake_path))
    assert audio_safety.get_safety_settings()["disable_flash_heavy"] is False
    audio_safety.set_disable_flash_heavy(True)
    assert audio_safety.get_safety_settings()["disable_flash_heavy"] is True


def test_mode_metadata_flags_flash_heavy_modes_correctly():
    modes = ["band_fixed", "strobe_on_drop", "band_flash_overlay", "breathing_silence"]
    meta = {m["mode"]: m for m in audio_safety.mode_metadata(modes)}
    assert meta["strobe_on_drop"]["flash_heavy"] is True
    assert meta["strobe_on_drop"]["category"] == "flash-heavy"
    assert meta["band_flash_overlay"]["flash_heavy"] is True
    assert meta["band_fixed"]["flash_heavy"] is False
    assert meta["band_fixed"]["category"] == "ambient"
    assert meta["breathing_silence"]["flash_heavy"] is False


def test_reduced_motion_profile_disables_flash_heavy_and_caps_rate():
    profile = audio_safety.reduced_motion_profile()
    assert profile["disable_flash_heavy"] is True
    assert profile["mode"] not in audio_safety.FLASH_HEAVY_MODES
    assert profile["max_flash_rate_hz"] <= audio_safety.HARD_MAX_FLASH_RATE_HZ


def _hard_strobe_action():
    """Mimics what strobe_on_drop actually returns on a hit: full-brightness
    white flash from a much dimmer resting state."""
    return ("hsv", 0, 0, 100)


def test_apply_flash_cap_never_exceeds_hard_ceiling_under_rapid_strobing():
    """The core safety guarantee: even if a mode (or a misconfigured
    request) tries to flash far faster than the hard ceiling allows,
    `apply_flash_cap` must suppress enough of them that the *actual* flash
    rate delivered never exceeds HARD_MAX_FLASH_RATE_HZ, measured over real
    wall-clock time.

    Simulates what strobe_on_drop actually does: dip to a dim resting
    brightness between hits, then jump to 100 on each "hit" -- alternating
    every 20ms (~25 hit-attempts/sec), far above the 3/sec ceiling. Letting
    `ctx` carry its own state naturally between calls (as the real send
    loop does) is what makes each jump a genuine new flash for the cap to
    evaluate, not an artifact of the test harness."""
    ctx = {"_safety_prev_brightness": 8.0}
    max_rate_hz = audio_safety.HARD_MAX_FLASH_RATE_HZ  # 3.0/s -> min 1/3s apart
    flash_events = []

    start = time.time()
    duration_s = 1.2
    i = 0
    while time.time() - start < duration_s:
        dim = (i % 2 == 0)
        proposed = ("hsv", 0, 0, 8.0) if dim else ("hsv", 0, 0, 100.0)
        # Try to bypass via an absurd requested rate -- must still be
        # clamped down to the hard ceiling inside apply_flash_cap.
        capped = audio_safety.apply_flash_cap(proposed, ctx, max_rate_hz=max_rate_hz * 100)
        if not dim and capped[3] >= 100:
            flash_events.append(time.time())
        i += 1
        time.sleep(0.02)

    # Over `duration_s` seconds, no more than ceil(duration_s * 3) + 2 real
    # flashes should have gotten through (+2 slack for boundary timing).
    max_allowed = int(duration_s * max_rate_hz) + 2
    assert len(flash_events) <= max_allowed, (
        f"flash cap let {len(flash_events)} flashes through in {duration_s}s "
        f"(max allowed ~{max_allowed}) -- the non-bypassable ceiling was violated"
    )
    # And it must have let *some* through -- a cap that suppresses
    # everything isn't a rate limit, it's silently broken.
    assert len(flash_events) >= 1


def test_apply_flash_cap_requested_rate_cannot_exceed_hard_ceiling():
    """Even asking for an explicit max_rate_hz above the hard ceiling must
    not raise the effective rate above it."""
    ctx = {"_safety_prev_brightness": 8.0}
    a = audio_safety.apply_flash_cap(("hsv", 0, 0, 100), ctx, max_rate_hz=1000.0)
    assert a[3] == 100  # first flash always gets through
    ctx["_safety_prev_brightness"] = 8.0
    # Immediately try again (0s elapsed) -- must be suppressed since the
    # hard ceiling (3/s -> ~333ms min interval) hasn't elapsed, regardless
    # of the requested 1000Hz.
    b = audio_safety.apply_flash_cap(("hsv", 0, 0, 100), ctx, max_rate_hz=1000.0)
    assert b[3] < 100, "requesting an absurd max_rate_hz must not bypass the hard ceiling"


def test_apply_flash_cap_leaves_smooth_ambient_changes_alone():
    """A gradual brightness change (not a flash-sized jump) must pass
    through unmodified -- the cap only targets actual flash-sized jumps."""
    ctx = {"_safety_prev_brightness": 50.0}
    gentle = ("hsv", 100, 100, 55.0)  # a +5 point change, well under FLASH_BRIGHTNESS_JUMP
    result = audio_safety.apply_flash_cap(gentle, ctx, max_rate_hz=3.0)
    assert result == gentle


def test_apply_flash_cap_respects_max_brightness_swing():
    ctx = {"_safety_prev_brightness": 10.0}
    action = ("hsv", 10, 100, 90.0)
    result = audio_safety.apply_flash_cap(action, ctx, max_rate_hz=3.0, max_brightness_swing=20.0)
    assert result[3] <= 30.0  # 10 + 20 max swing
