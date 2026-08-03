"""Latency-measurement harness (Section 10, W1-176-190): times the REAL
analyze_frame() + _apply_mode() decision pipeline against synthetic input
and reports genuine numbers -- not an estimate. Also exercises the fuller
per-frame pipeline (signal conditioning + full-band extraction, as
AudioSession._process actually runs it) so the reported number reflects
what a real capture callback pays, not just the narrower analyze_frame call.

This is a real regression guard, not just a report: the assertions use a
generous bound (the block-size's own real-time budget) so it fails loudly
if a future change makes analysis slower than real-time, while staying far
enough from typical measured numbers (see the printed output) that it
won't flake on a slower CI box.

Run with:
    pytest backend/tests/test_audio_latency.py -v -s
(the -s is needed to see the printed real timing numbers)
"""
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import audio_fixtures as af  # noqa: E402
import audio_reactive as ar  # noqa: E402
import audio_signal as asig  # noqa: E402

# BLOCK_SIZE=512 @ 44100Hz means a real capture callback fires roughly every
# 11.6ms -- analysis must comfortably finish inside that window or the
# pipeline falls behind real-time. Bound set generously (half the frame
# budget) so it fails loudly on a real regression without flaking on a
# slower CI machine.
FRAME_BUDGET_MS = (ar.BLOCK_SIZE / ar.SAMPLE_RATE) * 1000.0  # ~11.6ms
REGRESSION_BOUND_MS = FRAME_BUDGET_MS * 0.5


def _report(label, durations_ms):
    mean = statistics.mean(durations_ms)
    p50 = statistics.median(durations_ms)
    p95 = sorted(durations_ms)[int(len(durations_ms) * 0.95)]
    worst = max(durations_ms)
    print(f"\n[latency] {label}: n={len(durations_ms)} mean={mean:.4f}ms p50={p50:.4f}ms "
          f"p95={p95:.4f}ms worst={worst:.4f}ms (frame budget {FRAME_BUDGET_MS:.2f}ms)")
    return mean, p50, p95, worst


def test_analyze_and_apply_mode_latency_per_mode(check_all):
    """All 20 modes in one test rather than 20 parametrised ones.

    Latency especially wants the collected report: a regression is usually
    in shared code and pushes several modes over the bound at once, so the
    useful output is the full list of which modes blew the budget and by how
    much -- not the first one alphabetically.
    """
    samples = af.make_multi_tone([(100, 0.6), (1000, 0.3), (8000, 0.15)], duration_s=1.0)

    def _check(mode):
        n_bands = 6 if mode in ("spectrum_gradient", "band_flash_overlay", "harmonic_pairs") else 3
        durations_ms = af.time_pipeline(samples, mode=mode, n_bands=n_bands, repeats=1)
        mean, p50, p95, worst = _report(f"analyze_frame+_apply_mode[{mode}]", durations_ms)
        assert mean < REGRESSION_BOUND_MS, (
            f"mean analysis latency {mean:.4f}ms exceeds the real-time regression "
            f"bound of {REGRESSION_BOUND_MS:.4f}ms (frame budget is {FRAME_BUDGET_MS:.2f}ms)"
        )

    check_all(ar.MODES, _check, label="mode")


def test_full_process_pipeline_latency_with_signal_conditioning():
    """Times the FULL per-frame cost AudioSession._process actually pays:
    SignalConditioner.process() + analyze_frame() (with extra_band_edges for
    the full-spectrum status field) + _apply_mode(), back to back, on real
    synthetic audio -- not just the narrower analyze_frame-only measurement
    above."""
    samples = af.make_multi_tone([(100, 0.6), (1000, 0.3), (8000, 0.15)], duration_s=2.0)
    conditioner = asig.SignalConditioner(agc_enabled=True, noise_gate_enabled=True, dc_removal_enabled=True)
    ctx = ar._new_ctx(1.0, 280.0)
    band_edges_6 = ar.log_band_edges(6)
    durations_ms = []
    now = 0.0
    frame_dt = ar.BLOCK_SIZE / float(ar.SAMPLE_RATE)
    for block in af.iter_blocks(samples):
        now += frame_dt
        t0 = time.perf_counter()
        conditioned, _meter = conditioner.process(block, frame_dt=frame_dt, now=now)
        bands = ar.analyze_frame(conditioned, extra_band_edges=band_edges_6)
        bands = conditioner.apply_band_gains(bands)
        ar._apply_mode("band_fixed", bands, ctx)
        durations_ms.append((time.perf_counter() - t0) * 1000.0)

    mean, p50, p95, worst = _report("full AudioSession._process pipeline", durations_ms)
    assert mean < REGRESSION_BOUND_MS, (
        f"full pipeline mean latency {mean:.4f}ms exceeds the real-time regression "
        f"bound of {REGRESSION_BOUND_MS:.4f}ms"
    )


def test_signal_conditioner_alone_latency():
    samples = af.make_white_noise(2.0, amplitude=0.3, seed=1)
    conditioner = asig.SignalConditioner(agc_enabled=True, noise_gate_enabled=True, dc_removal_enabled=True)
    frame_dt = ar.BLOCK_SIZE / float(ar.SAMPLE_RATE)
    now = 0.0
    durations_ms = []
    for block in af.iter_blocks(samples):
        now += frame_dt
        t0 = time.perf_counter()
        conditioner.process(block, frame_dt=frame_dt, now=now)
        durations_ms.append((time.perf_counter() - t0) * 1000.0)
    _report("SignalConditioner.process alone", durations_ms)
