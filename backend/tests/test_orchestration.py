"""Direct unit tests for the Week 1 Phase C orchestration refinements added
to `GroupAudioSession` (Section 6): the "wave" and "mirror" role modes, the
per-bulb hue_offsets/brightness_scales/band_assignments override lists, and
failover behavior when one bulb in a group starts failing mid-session.

None of this touches real hardware -- `GroupAudioSession` is fed synthetic
numpy sample arrays directly via `_process()` (the same technique
test_audio_modes.py uses for `_apply_mode`), and multi-bulb orchestration is
exercised against lightweight FakeController stand-ins (mirroring
conftest.py's FakeTuyaBulbDevice pattern) rather than 3+ real physical
bulbs, which this dev box does not have.

Run with:
    pytest backend/tests/test_orchestration.py -v
"""
import math
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import audio_reactive as ar  # noqa: E402


class FakeController:
    """Minimal stand-in for BulbController -- just enough surface for
    GroupAudioSession/BulbSender to drive it (stop_effect, _log, set_hsv,
    set_rgb, set_brightness) while recording every call so tests can
    assert on what actually reached "the device". `fail=True` makes every
    network call raise, simulating a bulb that has gone offline."""

    def __init__(self, name, fail=False):
        self.cfg = {"id": name}
        self.name = name
        self.fail = fail
        self.calls = []

    def stop_effect(self):
        pass

    def _log(self, *args, **kwargs):
        pass

    def set_hsv(self, h, s, v):
        if self.fail:
            raise RuntimeError(f"{self.name} is unreachable")
        self.calls.append(("hsv", h, s, v))

    def set_rgb(self, r, g, b):
        if self.fail:
            raise RuntimeError(f"{self.name} is unreachable")
        self.calls.append(("rgb", r, g, b))

    def set_brightness(self, pct):
        if self.fail:
            raise RuntimeError(f"{self.name} is unreachable")
        self.calls.append(("brightness", pct))


class RecordingSender:
    """Stand-in for BulbSender used when testing pure orchestration
    *computation* -- records every queued action synchronously (no real
    thread, no real dwell timing), so tests can assert exact per-bulb
    values without waiting on background threads at all."""

    def __init__(self):
        self.queued = []

    def queue(self, action):
        self.queued.append(action)

    def stop(self):
        pass


def make_group_session(n, mode="band_fixed", role_mode="unison", **kwargs):
    """Builds a real GroupAudioSession against FakeControllers, then swaps
    its real BulbSenders (which spin real background dwell threads) for
    RecordingSenders -- stopping the real ones cleanly first so no thread
    is left running past the test."""
    controllers = [FakeController(f"bulb-{i}") for i in range(n)]
    session = ar.GroupAudioSession(controllers, device_index=0, mode=mode, role_mode=role_mode,
                                    min_dwell_ms=ar.MIN_DWELL_FLOOR_MS, **kwargs)
    for s in session.senders:
        s.stop()
    session.senders = [RecordingSender() for _ in controllers]
    return session, controllers


def synthetic_samples(rms=0.05, n=512):
    """A flat-amplitude synthetic capture block. Real `_apply_mode` output
    depends on rms/energies, not on the samples being musical -- constant
    amplitude keeps every bulb's base color/brightness identical before
    role_mode/override logic is applied, isolating exactly what the
    orchestration layer contributes."""
    return np.full(n, rms, dtype=np.float64)


# ------------------------------------------------------------- wave mode --
def test_wave_brightness_scale_is_pure_and_deterministic():
    # Crest starts at bulb 0 (tick=0, position=0) -- bulb 0 should be at
    # or near peak, and scale should fall off with distance.
    n, period = 6, 60
    scales_t0 = [ar._wave_brightness_scale(0, i, n, period) for i in range(n)]
    assert scales_t0[0] == max(scales_t0), f"expected bulb 0 at the crest at tick 0: {scales_t0}"
    assert scales_t0[0] > scales_t0[3], "far bulb should be dimmer than the crest bulb"
    for s in scales_t0:
        assert 0.15 <= s <= 1.0

    # A quarter period later the crest should have moved forward along the
    # bulb list -- bulb further along should now be relatively brighter
    # than it was at tick 0.
    quarter_tick = period // 4
    scales_q = [ar._wave_brightness_scale(quarter_tick, i, n, period) for i in range(n)]
    moved_bulb = round(n * 0.25)
    assert scales_q[moved_bulb] > scales_t0[moved_bulb], (
        f"crest should have traveled toward bulb {moved_bulb}: t0={scales_t0} q={scales_q}"
    )


def test_wave_brightness_scale_single_bulb_is_always_full():
    assert ar._wave_brightness_scale(0, 0, 1) == 1.0
    assert ar._wave_brightness_scale(37, 0, 1) == 1.0


def test_wave_role_mode_scales_brightness_identically_in_hue_across_bulbs():
    n = 5
    session, controllers = make_group_session(n, mode="band_fixed", role_mode="wave", wave_period_ticks=n * 10)
    session._process(synthetic_samples(rms=0.05))

    actions = [s.queued[-1] for s in session.senders]
    hues = [round(a[1], 3) for a in actions]
    # Every bulb gets the SAME base color in wave mode -- only brightness
    # (and only brightness) is modulated per-bulb.
    assert len(set(hues)) == 1, f"wave mode must keep hue identical across bulbs, got {hues}"

    brightnesses = [a[3] for a in actions]
    assert len(set(round(b, 6) for b in brightnesses)) > 1, (
        f"wave mode must vary brightness across bulbs at a single tick, got {brightnesses}"
    )
    # Bulb 0 is at the wave crest on the very first processed frame
    # (tick starts at 0), so it should be the brightest of the group.
    assert brightnesses[0] == max(brightnesses), f"bulb 0 should be at the crest first: {brightnesses}"


def test_wave_tick_advances_once_per_processed_frame():
    session, controllers = make_group_session(3, role_mode="wave", wave_period_ticks=30)
    assert session._wave_tick == 0
    session._process(synthetic_samples())
    assert session._wave_tick == 1
    session._process(synthetic_samples())
    assert session._wave_tick == 2


# ----------------------------------------------------------- mirror mode --
def test_mirror_hue_pure_function_reflects_around_center():
    # 4 bulbs: pairs are (0,3) and (1,2). Leaders (0,1) pass through
    # unchanged; followers (2,3) mirror around center_hue.
    assert ar._mirror_hue(30.0, 0, 4, center_hue=0.0) == 30.0  # leader
    assert ar._mirror_hue(30.0, 3, 4, center_hue=0.0) == (2 * 0.0 - 30.0) % 360  # follower -> 330
    assert ar._mirror_hue(200.0, 1, 4, center_hue=90.0) == 200.0  # leader
    expected_follower = (2 * 90.0 - 200.0) % 360  # = -20 % 360 = 340
    assert ar._mirror_hue(200.0, 2, 4, center_hue=90.0) == expected_follower


def test_mirror_hue_unpaired_middle_bulb_is_unchanged():
    # 3 bulbs (odd): middle bulb (index 1) has no partner (partner == self).
    assert ar._mirror_hue(77.0, 1, 3, center_hue=0.0) == 77.0
    assert ar._mirror_hue(77.0, 0, 3, center_hue=0.0) == 77.0  # leader of (0, 2)
    assert ar._mirror_hue(77.0, 2, 3, center_hue=0.0) == (2 * 0.0 - 77.0) % 360  # follower


def test_mirror_role_mode_pairs_reflect_in_a_real_group_session():
    n = 4
    session, controllers = make_group_session(n, mode="band_fixed", role_mode="mirror", mirror_center_hue=0.0)
    session._process(synthetic_samples(rms=0.05))
    actions = [s.queued[-1] for s in session.senders]
    hues = [a[1] for a in actions]

    # Leaders (0, 1) keep the shared base hue.
    assert hues[0] == hues[1]
    # Followers (3 mirrors 0; 2 mirrors 1) reflect around center_hue=0,
    # i.e. mirrored = (-base_hue) % 360.
    expected_follower_hue = (2 * 0.0 - hues[0]) % 360
    assert abs(hues[3] - expected_follower_hue) < 1e-9
    assert abs(hues[2] - expected_follower_hue) < 1e-9
    # And the pairing is genuinely different hues (unless base_hue happens
    # to be exactly on the center, which band_fixed at rms=0.05 is not).
    assert hues[0] != hues[3]


# -------------------------------------------- per-bulb hue_offsets override
def test_phase_offset_uses_explicit_hue_offsets_over_even_spacing():
    n = 3
    custom_offsets = [0.0, 45.0, 200.0]
    session, controllers = make_group_session(n, mode="band_fixed", role_mode="phase_offset",
                                               hue_offsets=custom_offsets)
    session._process(synthetic_samples(rms=0.05))
    actions = [s.queued[-1] for s in session.senders]
    base_hue = actions[0][1]  # bulb 0's offset is 0.0, so it shows the raw base hue
    for i, offset in enumerate(custom_offsets):
        expected = (base_hue + offset) % 360
        assert abs(actions[i][1] - expected) < 1e-6, (
            f"bulb {i} expected hue {expected} (offset {offset}), got {actions[i][1]}"
        )


def test_phase_offset_falls_back_to_even_spacing_when_no_override_given():
    n = 4
    session, controllers = make_group_session(n, mode="band_fixed", role_mode="phase_offset")
    session._process(synthetic_samples(rms=0.05))
    actions = [s.queued[-1] for s in session.senders]
    base_hue = (actions[0][1] - 0.0) % 360  # bulb 0's offset is always 0 in even spacing
    for i in range(n):
        expected = (base_hue + (360.0 / n) * i) % 360
        assert abs(actions[i][1] - expected) < 1e-6


# ----------------------------------------------- per-bulb brightness scale
def test_brightness_scales_apply_as_final_multiplier_in_unison_mode():
    n = 3
    scales = [1.0, 0.5, 2.0]  # bulb 2's scale should clamp at 100
    session, controllers = make_group_session(n, mode="band_fixed", role_mode="unison",
                                               brightness_scales=scales)
    session._process(synthetic_samples(rms=0.05))
    actions = [s.queued[-1] for s in session.senders]
    base_brightness = actions[0][3]  # scale 1.0 -> unchanged
    assert abs(actions[1][3] - min(100, base_brightness * 0.5)) < 1e-6
    assert abs(actions[2][3] - min(100, base_brightness * 2.0)) < 1e-6
    assert actions[2][3] <= 100


def test_brightness_scales_compose_with_wave_role_mode():
    n = 3
    session, controllers = make_group_session(n, mode="band_fixed", role_mode="wave",
                                               brightness_scales=[1.0, 1.0, 0.1],
                                               wave_period_ticks=30)
    session._process(synthetic_samples(rms=0.05))
    actions = [s.queued[-1] for s in session.senders]
    # Bulb 2 should be dramatically dimmer than bulb 0 or 1 due to its
    # 0.1x scale, regardless of where the wave crest currently sits.
    assert actions[2][3] < actions[0][3]
    assert actions[2][3] < actions[1][3]


# --------------------------------------------- per-bulb band_assignments
def test_band_split_default_assignment_is_bulb_index():
    # 3 bulbs, one band each hot in turn -- default assignment means bulb i
    # is fed band i as its "primary" (rolled-to-front) band.
    n = 3
    session, controllers = make_group_session(n, mode="dominant_band", role_mode="band_split")
    samples = synthetic_samples(rms=0.05)
    session._process(samples)
    # Just confirm it runs without crashing and produces valid hsv actions
    # for every bulb -- the real per-band assertion is in the override test
    # below, which is easier to make airtight with hand-picked energies.
    for s in session.senders:
        assert s.queued[-1][0] == "hsv"


def test_band_split_manual_override_changes_which_band_a_bulb_sees_as_primary():
    """Force a 3-band split where band 2 (treble) is the only hot band.
    Bulb 0's DEFAULT assignment is its own index (band 0), so the hot
    energy gets rolled to the LAST position of its view -- dominant_band
    reads that as the TREBLE anchor. Overriding bulb 0's assignment to
    band 2 instead rolls that same hot energy to the FIRST position --
    dominant_band now reads it as the BASS anchor. Different, and
    predictable, output proves the override genuinely changes which band
    a bulb is fed rather than just cosmetically relabeling it.
    """
    import audio_reactive as ar_module

    def fake_analyze_frame(samples, band_edges=None):
        return {
            "spectrum": None, "freqs": None, "rms": 0.05,
            "energies": [0.0, 0.0, 100.0], "fractions": [0.0, 0.0, 1.0],
            "band_edges": band_edges,
        }

    original = ar_module.analyze_frame

    def run(band_assignments):
        session, controllers = make_group_session(3, mode="dominant_band", role_mode="band_split",
                                                    band_assignments=band_assignments)
        ar_module.analyze_frame = fake_analyze_frame
        try:
            # dominant_band smooths toward its target at alpha=0.2 per
            # frame (see _apply_mode) -- run enough frames for the circular
            # smoothing to actually converge before comparing to the exact
            # anchor, same convergence pattern test_audio_modes.py uses.
            action = None
            for _ in range(150):
                session._process(np.zeros(8, dtype=np.float64))
                action = session.senders[0].queued[-1]
        finally:
            ar_module.analyze_frame = original
        return action

    default_action = run(None)
    override_action = run([2, None, None])

    assert abs(default_action[1] - ar.TREBLE_HUE) < 5.0, (
        f"bulb 0's default assignment (band 0) should read treble-hot energy "
        f"as TREBLE_HUE once rolled to the last position, got {default_action[1]}"
    )
    assert abs(override_action[1] - ar.BASS_HUE) < 5.0, (
        f"bulb 0 overridden to band 2 should read the same hot energy as "
        f"BASS_HUE once rolled to the first position, got {override_action[1]}"
    )


# ------------------------------------------------------- failover handling
def test_group_session_continues_orchestrating_other_bulbs_when_one_fails():
    """Simulates one bulb going offline mid-group-session: its BulbSender
    keeps raising on every send, while the other bulbs' real BulbSenders
    (genuine background threads, genuine dwell pacing) keep receiving and
    successfully dispatching actions. No restart of the group session ever
    happens -- this is the same session object, same senders, throughout."""
    good_a = FakeController("good-a")
    bad = FakeController("bad", fail=True)
    good_b = FakeController("good-b")
    controllers = [good_a, bad, good_b]

    session = ar.GroupAudioSession(controllers, device_index=0, mode="band_fixed", role_mode="unison",
                                    min_dwell_ms=ar.MIN_DWELL_FLOOR_MS)
    try:
        # Drive several frames directly (bypassing real audio capture,
        # exactly like test_audio_modes.py bypasses real FFT setup).
        for _ in range(5):
            session._process(synthetic_samples(rms=0.05))
            time.sleep(0.06)  # let each frame's queued action clear the real dwell window

        deadline = time.time() + 2.0
        while time.time() < deadline:
            if good_a.calls and good_b.calls:
                break
            time.sleep(0.05)

        # The two healthy bulbs actually received real dispatched calls.
        assert good_a.calls, "healthy bulb A should have received dispatched actions"
        assert good_b.calls, "healthy bulb B should have received dispatched actions"
        # The failing bulb never got a *successful* recorded call (it
        # always raises before appending to .calls).
        assert bad.calls == []

        # The failing bulb's sender reports itself offline after enough
        # consecutive failures, without the group session's thread or the
        # other senders being affected at all.
        bad_index = controllers.index(bad)
        bad_status = session.senders[bad_index].status()
        assert bad_status["consecutive_failures"] >= ar.FAILOVER_THRESHOLD
        assert bad_status["offline"] is True
        assert bad_status["error"] is not None

        good_index = controllers.index(good_a)
        good_status = session.senders[good_index].status()
        assert good_status["offline"] is False
        assert good_status["error"] is None

        # Live per-bulb status field (Section 6): status() reports a
        # distinct state per bulb -- the failing one "offline", the
        # healthy ones "active".
        full_status = session.status()
        bulbs_by_index = {b["index"]: b for b in full_status["bulbs"]}
        assert bulbs_by_index[bad_index]["state"] == "offline"
        assert bulbs_by_index[good_index]["state"] == "active"

        # Push more frames AFTER the failure is established -- the good
        # bulbs must keep being fed without any restart.
        calls_before = len(good_a.calls)
        for _ in range(3):
            session._process(synthetic_samples(rms=0.05))
            time.sleep(0.06)
        deadline = time.time() + 1.0
        while time.time() < deadline and len(good_a.calls) <= calls_before:
            time.sleep(0.05)
        assert len(good_a.calls) > calls_before, "healthy bulb should keep receiving new dispatches after failover"
    finally:
        for s in session.senders:
            s.stop()
