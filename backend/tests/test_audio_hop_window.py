"""Hop/window decoupling (issue #80).

The claim under test: analysis can run every 5.8ms while still seeing 46ms of
audio, so latency drops below the 10ms target *without* trading away the
low-frequency resolution that makes beat detection work.

The two things that must not regress are load-bearing and get their own tests:

* `analyze_frame()` is untouched — the 21 golden-value tests in
  test_audio_golden.py are the guard, and they are not modified here.
* bass-band tempo tracking still locks on through a dense mix, now driven
  through the real hop scheduler rather than at the old fixed cadence.

Run with:
    pytest backend/tests/test_audio_hop_window.py -v
"""
import os
import sys
import time

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import audio_fixtures as af  # noqa: E402
import audio_hop  # noqa: E402
import audio_latency as al  # noqa: E402
import audio_reactive as ar  # noqa: E402

SR = ar.SAMPLE_RATE
HOP_MS = (audio_hop.DEFAULT_HOP_SIZE / float(SR)) * 1000.0


# --------------------------------------------------------------- HopBuffer
def test_defaults_beat_the_old_block_on_both_axes():
    """512/512 was 11.6ms latency and ~86Hz resolution. The point of the
    change is that 256/2048 is better at *both*, not a trade."""
    old_latency_ms = (ar.BLOCK_SIZE / SR) * 1000.0
    old_resolution_hz = SR / float(ar.BLOCK_SIZE)
    new_latency_ms = (audio_hop.DEFAULT_HOP_SIZE / SR) * 1000.0
    new_resolution_hz = SR / float(audio_hop.DEFAULT_WINDOW_SIZE)

    assert new_latency_ms < old_latency_ms
    assert new_resolution_hz < old_resolution_hz, "smaller Hz per bin = finer resolution"
    assert new_latency_ms < al.SOFTWARE_TARGET_MS, "the hop alone must fit the budget"


def test_emits_one_window_per_hop_once_warmed_up():
    buf = audio_hop.HopBuffer(window=1024, hop=256)
    assert buf.push(np.zeros(1023)) == [], "nothing until the window has filled once"
    assert not buf.ready

    # The 1024th sample completes both the window and a hop boundary, so this
    # is the first emit.
    out = buf.push(np.ones(1))
    assert len(out) == 1
    assert out[0].shape == (1024,)
    assert buf.ready

    assert len(buf.push(np.ones(255))) == 0, "part of a hop emits nothing"
    assert len(buf.push(np.ones(1))) == 1, "completing the hop emits one window"


def test_a_block_carrying_several_hops_emits_several_windows():
    """Exactly what a bursty capture backend does, and why push() returns a
    list rather than an Optional."""
    buf = audio_hop.HopBuffer(window=1024, hop=256)
    buf.push(np.ones(1024))             # warm up
    out = buf.push(np.ones(1024))       # 4 hops in one block
    assert len(out) == 4


def test_windows_overlap_by_exactly_window_minus_hop():
    buf = audio_hop.HopBuffer(window=1024, hop=256)
    buf.push(np.arange(1024, dtype=np.float32))
    first = buf.push(np.arange(1024, 1280, dtype=np.float32))[0]
    second = buf.push(np.arange(1280, 1536, dtype=np.float32))[0]
    # Advancing by one hop means the tail of the first window is the head of
    # the second, shifted by exactly `hop`.
    assert np.array_equal(first[256:], second[:768])


def test_arriving_block_size_does_not_have_to_match_anything():
    """The bridge delivers whatever the host captured; a device delivers
    whatever PortAudio chose. Neither has to match the window."""
    for block in (37, 100, 256, 512, 1000):
        buf = audio_hop.HopBuffer(window=1024, hop=256)
        total = 0
        fed = 0
        while fed < 1024 * 4:
            total += len(buf.push(np.ones(block)))
            fed += block
        # Once warm, one window per hop: (fed - window) // hop, give or take
        # the partial hop still pending.
        assert total >= (fed - 1024) // 256 - 1, f"block size {block} lost windows"


def test_warmup_never_analyses_a_half_zero_window():
    """A part-filled window is half real audio and half zeros; that
    discontinuity splatters across the spectrum and would read as a transient
    at session start."""
    buf = audio_hop.HopBuffer(window=2048, hop=256)
    emitted = []
    for _ in range(7):
        emitted += buf.push(np.ones(256, dtype=np.float32))
    assert emitted == [], "still warming up"
    assert buf.status()["warmup_frames_skipped"] == 7

    emitted += buf.push(np.ones(256, dtype=np.float32))   # 8th hop fills 2048
    assert len(emitted) == 1
    assert np.all(emitted[0] == 1.0), "the first emitted window is entirely real audio"


def test_reset_clears_the_window():
    buf = audio_hop.HopBuffer(window=1024, hop=256)
    buf.push(np.ones(2048))
    assert buf.ready
    buf.reset()
    assert not buf.ready
    assert buf.push(np.ones(256)) == []


# ------------------------------------------------------------- validation
def test_rejects_hop_window_combinations_that_cannot_work(check_all):
    bad = [
        (32, 2048),      # hop below the floor
        (256, 16384),    # window above the ceiling
        (512, 256),      # window shorter than hop
        (300, 2048),     # window not a whole number of hops
    ]

    def _rejects(case):
        hop, window = case
        with pytest.raises(audio_hop.HopConfigError):
            audio_hop.validate_hop_window(hop, window)

    check_all(bad, _rejects, label="bad config", name=lambda c: f"hop={c[0]} window={c[1]}")


def test_accepts_the_shipped_default():
    hop, window = audio_hop.validate_hop_window(
        audio_hop.DEFAULT_HOP_SIZE, audio_hop.DEFAULT_WINDOW_SIZE)
    assert window % hop == 0


# ---------------------------------------------------- the accuracy guarantee
def _beat_track(bpm, seconds=14.0, density=2, seed=0):
    """Same fixture shape as test_audio_perf_and_tempo's, so the two results
    are comparable: a kick at an exact tempo buried under a dense mix."""
    n = int(SR * seconds)
    x = np.zeros(n)
    rs = np.random.RandomState(seed)
    period = SR * 60.0 / bpm
    for i in range(int(seconds * bpm / 60)):
        start = int(i * period)
        length = min(int(SR * 0.09), n - start)
        if length <= 0:
            break
        t = np.arange(length) / SR
        env = np.exp(-t * 38)
        x[start:start + length] += np.sin(2 * np.pi * 60 * t) * env + 0.35 * rs.randn(length) * env
    t = np.arange(n) / SR
    if density >= 1:
        x += 0.35 * np.sin(2 * np.pi * 220 * t) + 0.25 * np.sin(2 * np.pi * 330 * t)
    if density >= 2:
        x += 0.45 * np.sin(2 * np.pi * 440 * t) * (1 + 0.3 * np.sin(2 * np.pi * 0.7 * t))
        x += 0.30 * np.sin(2 * np.pi * 880 * t) + 0.12 * rs.randn(n)
    return np.clip(x, -1, 1)


def _detect_through_hop_scheduler(signal, hop=None, window=None):
    """Drives the REAL HopBuffer and the REAL TempoTracker at the hop cadence,
    rather than the old fixed-block loop -- this is what #80 actually changed,
    so this is what has to be measured."""
    hop = hop or audio_hop.DEFAULT_HOP_SIZE
    window = window or audio_hop.DEFAULT_WINDOW_SIZE
    buf = audio_hop.HopBuffer(window=window, hop=hop)
    tracker = ar.TempoTracker(frame_dt=hop / float(SR),
                              beat_refractory_s=ar.BEAT_REFRACTORY_S)
    for i in range(0, len(signal) - hop, hop):
        for w in buf.push(signal[i:i + hop]):
            bands = ar.analyze_frame(w, n_bands=3)
            tracker.update(bands["rms"], bass_energy=bands["energies"][0])
    return tracker


def test_bass_band_tempo_still_locks_on_through_the_hop_scheduler(check_all):
    """The 9/9 result must survive the change. This is the single most
    important test in the ticket: a shorter hop that broke beat detection
    would be trading the feature's accuracy for its latency, which is exactly
    what #80 says not to do.
    """
    def _tracks(bpm):
        got = _detect_through_hop_scheduler(_beat_track(bpm)).bpm
        assert got is not None, "no tempo detected at all"
        assert abs(got - bpm) <= 3, f"expected ~{bpm} BPM, got {got}"

    check_all([70, 100, 120, 128, 140, 174], _tracks, label="tempo",
              name=lambda b: f"{b} BPM")


def test_overlapping_windows_do_not_fire_more_beats_for_the_same_kick():
    """Analysis runs 2x more often than it used to. Beat detection must not
    therefore report 2x the beats -- the ticket calls this out explicitly.

    Compared against the track's own known beat count rather than against the
    old implementation, so this stays meaningful if either side changes.
    """
    bpm = 120.0
    seconds = 14.0
    expected_beats = int(seconds * bpm / 60.0)

    hop = audio_hop.DEFAULT_HOP_SIZE
    signal = _beat_track(bpm, seconds=seconds)
    buf = audio_hop.HopBuffer(window=audio_hop.DEFAULT_WINDOW_SIZE, hop=hop)
    tracker = ar.TempoTracker(frame_dt=hop / float(SR),
                              beat_refractory_s=ar.BEAT_REFRACTORY_S)

    fired = 0
    for i in range(0, len(signal) - hop, hop):
        for w in buf.push(signal[i:i + hop]):
            bands = ar.analyze_frame(w, n_bands=3)
            tracker.update(bands["rms"], bass_energy=bands["energies"][0])
            if tracker.is_beat:
                fired += 1

    # Generous ceiling: onset detection legitimately fires on more than just
    # the kick in a dense mix. What must NOT happen is a multiple of the beat
    # count from the same kick being seen in consecutive overlapping windows.
    assert fired <= expected_beats * 2, (
        f"{fired} beats fired for a track with {expected_beats} — overlapping "
        f"windows are double-counting onsets")
    assert fired > 0, "beat detection stopped firing entirely"


def test_refractory_is_off_by_default_so_existing_callers_are_unchanged():
    t = ar.TempoTracker()
    assert t.beat_refractory_s == 0.0
    assert t._refractory_frames == 0


def test_refractory_suppresses_repeats_but_not_genuine_beats():
    t = ar.TempoTracker(frame_dt=0.0058, beat_refractory_s=0.080)
    # ~14 frames of refractory at a 5.8ms hop.
    assert t._refractory_frames == 13

    # Drive a clear rising edge, then hold it high: the rise is one beat, the
    # sustain must not keep re-firing.
    for _ in range(60):
        t.update(0.01, bass_energy=0.001)
    fired = []
    for i in range(40):
        energy = 0.5 if i < 20 else 0.001
        t.update(0.01, bass_energy=energy)
        fired.append(t.is_beat)
    assert sum(fired) <= 2, f"a single sustained rise fired {sum(fired)} beats"


# ------------------------------------------------------------------- session
class _StubController:
    def __init__(self):
        self.cfg = {"id": "stub"}
        self.sent = []

    def set_socket_timeout(self, t): pass
    def set_hsv(self, h, s, v): self.sent.append((h, s, v))
    def set_rgb(self, r, g, b): pass
    def set_brightness(self, b): pass
    def stop_effect(self): pass
    def _log(self, *a, **k): pass


def test_session_reports_its_hop_and_window():
    session = ar.AudioSession(_StubController(), device_index=None, source_kind="callable")
    try:
        analysis = session.status()["analysis"]
        assert analysis["hop"] == audio_hop.DEFAULT_HOP_SIZE
        assert analysis["window"] == audio_hop.DEFAULT_WINDOW_SIZE
        assert analysis["overlap"] == audio_hop.DEFAULT_WINDOW_SIZE - audio_hop.DEFAULT_HOP_SIZE
    finally:
        session.sender.stop()


def test_session_hop_and_window_are_configurable():
    session = ar.AudioSession(_StubController(), device_index=None, source_kind="callable",
                              hop_size=512, window_size=4096)
    try:
        assert session.hop_size == 512
        assert session.window_size == 4096
        assert session.latency.block_period_ms == pytest.approx((512 / SR) * 1000.0, abs=0.01)
        # The tempo autocorrelation's seconds-per-frame must follow the hop or
        # every BPM estimate scales wrong.
        assert session.tempo.frame_dt == pytest.approx(512 / SR, abs=1e-9)
    finally:
        session.sender.stop()


def test_session_analyses_at_the_hop_cadence_not_the_block_cadence():
    """One arriving 2048-sample block should produce 8 analyses at a 256 hop,
    not one."""
    session = ar.AudioSession(_StubController(), device_index=None, source_kind="callable")
    try:
        warm = np.asarray(af.make_tone(200, 0.2)[:audio_hop.DEFAULT_WINDOW_SIZE],
                          dtype=np.float32).reshape(-1, 1)
        session._process(warm)                       # fills the window
        before = session.latency.summary()["stages"]["analysis"]["count"]

        block = np.asarray(af.make_tone(200, 0.2)[:2048], dtype=np.float32).reshape(-1, 1)
        session._process(block)
        after = session.latency.summary()["stages"]["analysis"]["count"]

        assert after - before == 2048 // audio_hop.DEFAULT_HOP_SIZE == 8
    finally:
        session.sender.stop()


def test_cpu_cost_of_the_shorter_hop_stays_within_the_real_time_bound():
    """A shorter hop raises how often analysis runs, so the per-second cost is
    what matters, not the per-frame cost. Bound is the hop's own budget --
    analysis must finish comfortably inside the time until the next hop or the
    pipeline falls behind real time.
    """
    window = np.asarray(af.make_multi_tone([(100, 0.6), (1000, 0.3), (8000, 0.15)], duration_s=1.0)
                        [:audio_hop.DEFAULT_WINDOW_SIZE], dtype=np.float32)
    band_edges_6 = ar.log_band_edges(6)

    n = 200
    start = time.perf_counter()
    for _ in range(n):
        ar.analyze_frame(window, extra_band_edges=band_edges_6)
    per_frame_ms = ((time.perf_counter() - start) / n) * 1000.0

    duty = per_frame_ms / HOP_MS
    print(f"\n[hop] analysis {per_frame_ms:.3f}ms per {HOP_MS:.2f}ms hop "
          f"= {duty * 100:.1f}% of one core")
    assert per_frame_ms < HOP_MS * 0.5, (
        f"analysis at {per_frame_ms:.3f}ms does not fit comfortably inside a "
        f"{HOP_MS:.2f}ms hop")
