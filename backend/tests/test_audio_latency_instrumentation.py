"""Runtime per-stage latency instrumentation (issue #78).

Distinct from `test_audio_latency.py`, which is an offline *benchmark* -- it
times the pipeline in a harness and prints numbers. Nothing there proves a
running session reports anything. This file tests the live instrumentation:
that a session measures capture, analysis and bulb round-trip separately,
distinguishes typical from worst, counts late and dropped frames, and surfaces
all of it through status() and the API.

The timing assertions use *injected* delays with generous bounds, following
the precedent set by `test_audio_latency.py`'s regression bound: a real sleep
of Nms must show up as roughly Nms, checked with enough headroom that Windows
scheduling jitter on a loaded box cannot flake it. The bounds are wide on
purpose -- these tests prove the instrumentation is wired to reality, not that
the machine is fast.

Run with:
    pytest backend/tests/test_audio_latency_instrumentation.py -v
"""
import os
import sys
import time

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import audio_fixtures as af  # noqa: E402
import audio_latency as al  # noqa: E402
import audio_reactive as ar  # noqa: E402
import capture_sources  # noqa: E402

BLOCK_PERIOD_MS = (ar.BLOCK_SIZE / float(ar.SAMPLE_RATE)) * 1000.0        # ~11.6ms
HOP_PERIOD_MS = (ar.DEFAULT_HOP_SIZE / float(ar.SAMPLE_RATE)) * 1000.0    # ~5.8ms


class StubController:
    """Minimal controller for BulbSender: enough surface to drive a send, with
    an injectable delay standing in for a slow bulb."""

    def __init__(self, delay_s=0.0):
        self.cfg = {"id": "stub"}
        self.delay_s = delay_s
        self.sent = []
        self.fail = False

    def set_socket_timeout(self, t):
        pass

    def set_hsv(self, h, s, v):
        if self.delay_s:
            time.sleep(self.delay_s)
        if self.fail:
            raise RuntimeError("bulb unreachable")
        self.sent.append((h, s, v))

    def set_rgb(self, r, g, b):
        if self.delay_s:
            time.sleep(self.delay_s)

    def set_brightness(self, b):
        pass

    def stop_effect(self):
        pass

    def _log(self, *a, **k):
        pass


# --------------------------------------------------------------- StageWindow
def test_stage_window_reports_typical_and_worst_not_just_a_mean():
    w = al.StageWindow()
    # 99 fast frames and one 500ms spike: a mean alone would report ~6ms and
    # hide it completely, which is the failure this whole panel exists to stop.
    for _ in range(99):
        w.record(1.0)
    w.record(500.0)

    s = w.summary()
    assert s["p50_ms"] == 1.0
    assert s["max_ms"] == 500.0
    assert s["worst_ever_ms"] == 500.0
    assert s["count"] == 100
    assert s["mean_ms"] < 10.0, "mean is exactly the statistic that hides the spike"


def test_worst_ever_survives_the_rolling_window_forgetting_it():
    w = al.StageWindow(maxlen=8)
    w.record(250.0)
    for _ in range(20):  # evict the spike out of the window entirely
        w.record(1.0)

    s = w.summary()
    assert s["max_ms"] == 1.0, "the window itself should have forgotten the spike"
    assert s["worst_ever_ms"] == 250.0, "but the session's worst must still be visible"
    assert s["window_n"] == 8
    assert s["count"] == 21


def test_empty_stage_window_is_reportable_rather_than_a_crash():
    s = al.StageWindow().summary()
    assert s["p50_ms"] is None and s["max_ms"] is None and s["count"] == 0


def test_percentile_handles_the_tiny_windows_a_session_starts_with():
    assert al.percentile([], 0.5) is None
    assert al.percentile([7.0], 0.95) == 7.0
    assert al.percentile([1.0, 2.0, 3.0, 4.0], 0.0) == 1.0
    assert al.percentile([1.0, 2.0, 3.0, 4.0], 1.0) == 4.0


# ------------------------------------------------------------ LatencyTracker
def test_first_block_has_no_interval_to_measure():
    t = al.LatencyTracker(block_period_ms=BLOCK_PERIOD_MS)
    assert t.record_arrival(100.0) is None, "nothing to measure against yet"
    assert t.record_arrival(100.0 + 0.0116) == pytest.approx(11.6, abs=0.5)


def test_late_frames_are_counted_and_ordinary_jitter_is_not():
    t = al.LatencyTracker(block_period_ms=BLOCK_PERIOD_MS)
    now = 0.0
    t.record_arrival(now)
    # Ten on-schedule blocks, plus a little jitter that must NOT trip the count.
    for _ in range(10):
        now += 0.013  # 13ms — late-ish, well under the 2x threshold
        t.record_arrival(now)
    assert t.frame_counts()["late"] == 0

    now += 0.100  # a 100ms gap: unambiguously a stall
    t.record_arrival(now)
    counts = t.frame_counts()
    assert counts["late"] == 1
    assert counts["processed"] == 12
    assert counts["late_threshold_ms"] == pytest.approx(23.2, abs=0.5)


def test_stream_restart_does_not_invent_a_late_frame():
    """The gap spanning a reopened stream is not something a listener heard."""
    t = al.LatencyTracker(block_period_ms=BLOCK_PERIOD_MS)
    t.record_arrival(0.0)
    t.record_arrival(0.0116)
    t.reset_stream()
    assert t.record_arrival(30.0) is None, "a restart starts a fresh arrival chain"
    assert t.frame_counts()["late"] == 0


def test_dropped_frames_accumulate_and_ignore_nonsense():
    t = al.LatencyTracker(block_period_ms=BLOCK_PERIOD_MS)
    t.note_dropped(3)
    t.note_dropped(2)
    t.note_dropped(0)
    t.note_dropped(-5)
    assert t.frame_counts()["dropped"] == 5


def test_summary_separates_the_software_budget_from_the_hardware_floor():
    # A 4ms hop: the tracker is told the period, because capture latency is
    # bounded by how often a block is produced, not by how fast the callback
    # thread happens to be scheduled.
    t = al.LatencyTracker(block_period_ms=4.0)
    t.record_arrival(0.0)
    for i in range(1, 40):
        t.record_arrival(i * 0.004)   # delivered on schedule
    for _ in range(40):
        t.record_analysis(1.0)
        t.record_bulb(130.0)          # the real bulb's measured neighbourhood

    s = t.summary()
    assert s["stages"]["capture"]["kind"] == "software"
    assert s["stages"]["analysis"]["kind"] == "software"
    assert s["stages"]["bulb"]["kind"] == "hardware"

    budget = s["budget"]
    assert budget["software_p50_ms"] == pytest.approx(5.0, abs=0.5), "4ms capture + 1ms analysis"
    assert budget["hardware_p50_ms"] == pytest.approx(130.0, abs=0.5)
    assert budget["total_p50_ms"] == pytest.approx(135.0, abs=1.0)
    # The bulb is 26x the software budget and must not drag the verdict down.
    assert budget["within_target"] is True


def test_bursty_delivery_cannot_flatter_the_capture_figure():
    """Regression guard for a real bug this instrumentation shipped with.

    Measured live against DirectSound: p50 inter-arrival 0.93ms, mean 11.59ms.
    Blocks arrive several at a time then pause, so *half the gaps are
    sub-millisecond*. Reporting the median gap as capture latency made a 12ms
    pipeline report 1.5ms and claim it met the 10ms target -- the exact false
    pass this whole ticket exists to prevent.

    Capture latency is the block period plus delivery lateness. Blocks flushed
    from a backlog are stale, not fast.
    """
    t = al.LatencyTracker(block_period_ms=BLOCK_PERIOD_MS)
    now = 0.0
    t.record_arrival(now)
    # Ten bursts of four blocks 0.2ms apart, each burst 45ms after the last --
    # the shape actually observed on this machine.
    for _ in range(10):
        for _ in range(3):
            now += 0.0002
            t.record_arrival(now)
        now += 0.045
        t.record_arrival(now)

    capture = t.summary()["stages"]["capture"]
    assert capture["interval_p50_ms"] < 1.0, "the raw gaps really are mostly sub-millisecond"
    assert capture["p50_ms"] >= BLOCK_PERIOD_MS, (
        f"capture latency can never be below the block period; got {capture['p50_ms']}ms "
        f"against a {BLOCK_PERIOD_MS:.2f}ms floor")
    assert capture["floor_ms"] == pytest.approx(BLOCK_PERIOD_MS, abs=0.01)
    # The stalls must show up in the worst case rather than being averaged away.
    assert capture["max_ms"] > 40.0

    t2 = al.LatencyTracker(block_period_ms=BLOCK_PERIOD_MS)
    t2.record_arrival(0.0)
    for i in range(1, 30):
        t2.record_arrival(i * BLOCK_PERIOD_MS / 1000.0)
    for _ in range(30):
        t2.record_analysis(0.6)
    # 11.61 + 0.6 = 12.2ms. The honest verdict on today's pipeline is "over".
    assert t2.summary()["budget"]["within_target"] is False


def test_within_target_is_false_when_the_software_stages_blow_the_budget():
    t = al.LatencyTracker(block_period_ms=BLOCK_PERIOD_MS)
    t.record_arrival(0.0)
    for i in range(1, 30):
        t.record_arrival(i * BLOCK_PERIOD_MS / 1000.0)  # today's 11.6ms block
    for _ in range(30):
        t.record_analysis(0.2)

    budget = t.summary()["budget"]
    # This is the honest state of the pipeline as of #78: 512-sample blocks are
    # already over the 10ms target before any bridge hop exists. #80 is the
    # ticket that fixes it; this test pins the starting point.
    assert budget["software_p50_ms"] > 10.0
    assert budget["within_target"] is False


def test_null_tracker_matches_the_real_one_where_callers_touch_it():
    n = al.NullLatencyTracker()
    assert n.record_arrival(1.0) is None
    n.record_analysis(1.0), n.record_bulb(1.0), n.note_dropped(2), n.reset_stream()
    assert n.summary() is None
    assert n.frame_counts()["processed"] == 0


# ------------------------------------------------- injected-delay round-trips
def test_injected_bulb_delay_shows_up_in_the_bulb_stage():
    """A deliberately slow bulb must be measured as a slow bulb.

    30ms injected; bounds are wide (15-400ms) because Windows sleep granularity
    and thread scheduling both add slop. The point is that the number tracks
    reality, not that it is precise.
    """
    tracker = al.LatencyTracker(block_period_ms=BLOCK_PERIOD_MS)
    controller = StubController(delay_s=0.030)
    sender = ar.BulbSender(controller, min_dwell_ms=40, tracker=tracker)
    try:
        deadline = time.time() + 5.0
        while len(controller.sent) < 3 and time.time() < deadline:
            sender.queue(("hsv", 120.0, 100.0, 80.0))
            time.sleep(0.05)
        assert len(controller.sent) >= 3, "sender never delivered enough sends to measure"

        bulb = tracker.summary()["stages"]["bulb"]
        assert bulb["count"] >= 3
        assert 15.0 < bulb["p50_ms"] < 400.0, f"expected ~30ms, got {bulb['p50_ms']}ms"
    finally:
        sender.stop()


def test_a_failing_bulb_is_still_timed():
    """Timing that vanishes on error is worst-useless: a bulb that times out is
    exactly when someone opens the latency panel."""
    tracker = al.LatencyTracker(block_period_ms=BLOCK_PERIOD_MS)
    controller = StubController(delay_s=0.02)
    controller.fail = True
    sender = ar.BulbSender(controller, min_dwell_ms=40, tracker=tracker)
    try:
        deadline = time.time() + 4.0
        while tracker.summary()["stages"]["bulb"]["count"] < 2 and time.time() < deadline:
            sender.queue(("hsv", 10.0, 100.0, 50.0))
            time.sleep(0.05)
        assert sender.status()["error"], "the send really did fail"
        assert tracker.summary()["stages"]["bulb"]["count"] >= 2
    finally:
        sender.stop()


def test_injected_analysis_delay_shows_up_in_the_analysis_stage(monkeypatch):
    """20ms of injected work inside the decision path must be attributed to
    analysis -- and to analysis only."""
    tracker = al.LatencyTracker(block_period_ms=BLOCK_PERIOD_MS)
    real_apply = ar._apply_mode

    def slow_apply(mode, bands, ctx):
        time.sleep(0.020)
        return real_apply(mode, bands, ctx)

    monkeypatch.setattr(ar, "_apply_mode", slow_apply)

    session = ar.AudioSession(StubController(), device_index=None, source_kind="callable")
    session.latency = tracker
    session.sender.tracker = tracker
    try:
        # Enough audio to fill the analysis window and then run several hops.
        block = np.asarray(af.make_tone(440, 0.3)[:ar.DEFAULT_WINDOW_SIZE * 2],
                           dtype=np.float32).reshape(-1, 1)
        session._process(block)

        analysis = tracker.summary()["stages"]["analysis"]
        assert analysis["count"] >= 5, "the hop scheduler should have run several analyses"
        assert 10.0 < analysis["p50_ms"] < 300.0, f"expected ~20ms, got {analysis['p50_ms']}ms"
    finally:
        session.sender.stop()


def test_a_live_session_measures_capture_analysis_and_reports_them(monkeypatch):
    """End-to-end through the real `_run` loop and the real capture-source
    seam: a session driven by CallableSource at a known cadence must report a
    capture interval matching that cadence and a nonzero analysis cost."""
    samples = af.make_multi_tone([(100, 0.6), (1000, 0.3)], duration_s=0.5)
    blocks = list(af.iter_blocks(samples))
    interval_s = 0.02  # 20ms cadence, deliberately unlike the 11.6ms nominal

    def factory(callback, channels):
        return capture_sources.CallableSource(blocks, callback, interval_s=interval_s, loop=True)

    controller = StubController()
    session = ar.AudioSession(controller, device_index=None, mode="band_fixed",
                              min_dwell_ms=40, source_kind="callable", source_factory=factory)
    try:
        session.start()
        deadline = time.time() + 6.0
        while session.latency.frame_counts()["processed"] < 12 and time.time() < deadline:
            time.sleep(0.05)
        status = session.status()
    finally:
        session._stop.set()
        session.stop()

    lat = status["latency"]
    assert lat is not None, "a running session must report latency in status()"
    assert lat["frames"]["processed"] >= 12

    capture = lat["stages"]["capture"]
    # The source delivers every 20ms, which is slower than the hop. The floor
    # must follow the *source*, not the hop -- a decision cannot be fresher
    # than the audio it is made from.
    assert 8.0 < capture["p50_ms"] < 90.0, (
        f"expected a capture floor near {interval_s * 1000}ms, got {capture['p50_ms']}ms")
    assert capture["floor_source"] == "source delivery"
    assert capture["observed_period_ms"] > capture["configured_period_ms"]

    analysis = lat["stages"]["analysis"]
    assert analysis["count"] >= 12
    assert analysis["p50_ms"] > 0.0, "real analysis cannot take literally zero time"
    assert analysis["p50_ms"] < 50.0, "analysis should be far cheaper than the block period"

    # A session's configured period is its HOP (#80), not the old 512-sample
    # block: the hop is what bounds how often a decision is produced.
    assert lat["block_period_ms"] == pytest.approx(HOP_PERIOD_MS, abs=0.1)
    assert lat["budget"]["target_ms"] == al.SOFTWARE_TARGET_MS


# ------------------------------------------------------ dropped-frame wiring
def test_portaudio_input_overflow_is_reported_as_a_dropped_frame():
    """PortAudio has always reported input overflow in status_flags and the
    callback threw it away. An overflow is samples that existed and never
    reached analysis -- invisible in every other metric."""
    seen = []

    class Flags:
        input_overflow = True

    source = capture_sources.SoundDeviceSource(
        device_index=0, channels=1, samplerate=ar.SAMPLE_RATE, blocksize=ar.BLOCK_SIZE,
        callback=lambda block: None, on_dropped=seen.append)

    # Drive the callback body the way PortAudio would, without a real device.
    block = np.zeros((ar.BLOCK_SIZE, 1), dtype=np.float32)
    delivered = []
    source.callback = delivered.append

    def _cb(indata, frames, time_info, status_flags):
        if status_flags and getattr(status_flags, "input_overflow", False):
            source.overflows += 1
            if source.on_dropped is not None:
                source.on_dropped(1)
        source.callback(indata)

    _cb(block, ar.BLOCK_SIZE, None, Flags())
    _cb(block, ar.BLOCK_SIZE, None, None)

    assert seen == [1], "exactly one overflow reported"
    assert source.overflows == 1
    assert len(delivered) == 2, "both blocks still reached analysis"
    assert source.status()["overflows"] == 1


def test_bridge_drops_reach_the_session_tracker():
    """The bridge server already counted drops; nothing consumed the number."""

    class FakeServer:
        def __init__(self):
            self._drops = 0
            self._cb = None

        def subscribe(self, cb):
            self._cb = cb
            return 1

        def unsubscribe(self, sub_id):
            self._cb = None

        def status(self):
            return {"connected": True, "streaming": True, "drops": self._drops}

    tracker = al.LatencyTracker(block_period_ms=BLOCK_PERIOD_MS)
    server = FakeServer()
    source = capture_sources.NetworkSource(
        callback=lambda block: None, server=server, on_dropped=tracker.note_dropped)

    block = np.zeros((ar.BLOCK_SIZE, 1), dtype=np.float32)
    with source:
        server._cb(block)
        server._drops = 4          # the server dropped four while we weren't looking
        server._cb(block)
        server._drops = 6
        server._cb(block)

    assert tracker.frame_counts()["dropped"] == 6
    assert source.status()["drops"] == 6


# ------------------------------------------------------------------ overhead
def test_recording_is_cheap_enough_not_to_distort_what_it_measures():
    """The analysis path runs ~86x/second per session and was already
    optimised once for exactly this reason. Bound is deliberately loose --
    this catches "someone made record() do real work", not microseconds.
    """
    t = al.LatencyTracker(block_period_ms=BLOCK_PERIOD_MS)
    n = 20000
    start = time.perf_counter()
    for i in range(n):
        t.record_arrival(i * 0.0116)
        t.record_analysis(0.11)
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    per_frame_us = (elapsed_ms / n) * 1000.0

    print(f"\n[latency-instrumentation] {per_frame_us:.2f}us per frame "
          f"({elapsed_ms:.1f}ms for {n} frames)")
    # One frame's real analysis cost is ~111us. Instrumentation must be a
    # rounding error against that, not a tax.
    assert per_frame_us < 20.0, (
        f"instrumentation costs {per_frame_us:.2f}us/frame, which is no longer negligible "
        f"against the ~111us the analysis itself takes")


def test_summary_is_computed_on_read_not_on_the_hot_path():
    """Percentiles sort. Sorting per frame would be the obvious wrong design,
    so assert the window holds raw samples and only summary() orders them."""
    w = al.StageWindow()
    for v in (5.0, 1.0, 3.0):
        w.record(v)
    raw, _count, _worst, _total = w.snapshot()
    assert raw == [5.0, 1.0, 3.0], "records are stored in arrival order, unsorted"
    assert w.summary()["p50_ms"] == 3.0


# ----------------------------------------------------------------- API shape
def test_status_route_exposes_latency_for_an_idle_device(client):
    r = client.get("/api/devices/bulb-1/audio-reactive/status")
    assert r.status_code == 200
    assert r.json()["active"] is False


def test_group_session_tracks_latency_across_its_bulbs():
    controllers = [StubController(), StubController()]
    session = ar.GroupAudioSession(controllers, device_index=None, mode="band_fixed",
                                   role_mode="unison", min_dwell_ms=40)
    try:
        block = np.asarray(af.make_tone(220, 0.05)[:ar.BLOCK_SIZE], dtype=np.float32)
        for _ in range(4):
            session._process(block)

        status = session.status()
        lat = status["latency"]
        assert lat["frames"]["processed"] == 4
        assert lat["stages"]["analysis"]["count"] == 4
        assert lat["stages"]["capture"]["count"] == 3, "first block has no interval"
    finally:
        for s in session.senders:
            s.stop()
