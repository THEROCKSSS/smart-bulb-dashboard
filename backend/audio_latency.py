"""Per-stage latency instrumentation for audio-reactive sessions.

Answers "how long does sound take to become light, and which part of that is
actually ours" with measured numbers instead of a claim. Before this module
the only figure anywhere in the project was `BulbSender._last_latency_ms` --
one number, covering only the bulb send, with no representative-vs-worst
split and nothing at all for capture or analysis.

Why the split matters more than the total: the three stages have wildly
different ceilings and only two of them are ours to fix.

* **capture** -- a block cannot be handed over until its last sample exists,
  so the block period (`BLOCK_SIZE / SAMPLE_RATE`) is a floor set by
  configuration, and the reported figure is that floor plus however far behind
  schedule delivery is actually running.

  It is emphatically NOT the median gap between callbacks, which was the first
  thing this module did and was wrong. Measured live against DirectSound: p50
  gap 0.93ms, mean gap 11.59ms. The backend delivers in bursts -- several
  callbacks back to back, then a ~30ms pause -- so half the gaps are sub-
  millisecond and a median reads "capture is nearly free". It is not: the
  blocks arriving back-to-back are a backlog being flushed, and their samples
  are correspondingly stale. Reporting that median made a 12ms pipeline claim
  it met a 10ms target, which is precisely the false pass this instrumentation
  exists to make impossible.

  So: representative = block period. Worst case = block period + the largest
  excess gap seen. Raw inter-arrival stats are kept alongside as a delivery-
  health diagnostic, where a bursty median is informative rather than a lie.
* **analysis** -- the per-frame cost of turning samples into a colour
  decision. Ours, small, and already optimised once (0.260 -> 0.111 ms/frame).
* **bulb round-trip** -- measured at 111-152ms on this project's real
  hardware, roughly ten times the entire software budget. It is a hardware
  floor, no setting can move it, and hiding it is what makes someone conclude
  the software is slow and start tuning knobs that cannot possibly help. So
  it is reported, and reported as `kind: "hardware"`.

`DEFAULT_MIN_DWELL_MS = 90` in audio_reactive already encodes this knowledge
in a comment ("matches this bulb's measured ~50-100ms round trip"); this
module turns that comment into a live measurement.

Cost discipline: the analysis path runs ~86x/second per session, so recording
is an append to a bounded deque under a short lock and nothing else. Every
percentile is computed on *read* (a status poll, a few times a second at
most), never on the hot path.
"""
import collections
import threading

# Rolling window per stage. At ~86 frames/second this is a hair under three
# seconds of history -- long enough for p95 to mean something, short enough
# that the numbers still describe *now* rather than a spike from a minute ago.
DEFAULT_WINDOW = 256

# A block arriving more than this multiple of its nominal period after the
# previous one is counted late. 2.0 means "a whole extra block period went by"
# -- generous enough that ordinary scheduling jitter on a busy Windows box
# doesn't cry wolf, tight enough that a real underrun is visible.
LATE_ARRIVAL_FACTOR = 2.0

# The stated requirement for the part the software controls: sound-to-decision
# at or under this. Capture + analysis only -- explicitly NOT including the
# bulb, which no setting can bring under it.
SOFTWARE_TARGET_MS = 10.0

_SOFTWARE_STAGES = ("capture", "analysis")
_HARDWARE_STAGES = ("bulb",)

_STAGE_LABELS = {
    "capture": "Capture",
    "analysis": "Analysis",
    "bulb": "Bulb round-trip",
}


def percentile(values, fraction):
    """Nearest-rank percentile over an already-sorted sequence.

    Deliberately not statistics.quantiles: that interpolates and raises on
    n < 2, and this is called on windows that are legitimately tiny for the
    first second of a session.
    """
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    idx = int(round(fraction * (len(values) - 1)))
    return values[max(0, min(len(values) - 1, idx))]


class StageWindow:
    """A bounded rolling window of one stage's timings, summarised on read.

    `worst_ever_ms` is tracked separately from the window on purpose: the
    window forgets, and "the worst spike since this session started" is
    exactly the thing you want to still be able to see two minutes after it
    happened.
    """

    def __init__(self, maxlen=DEFAULT_WINDOW):
        self._samples = collections.deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._count = 0
        self._worst_ever = None
        self._total = 0.0

    def record(self, ms):
        """Hot path. An append and three scalar updates, nothing more."""
        with self._lock:
            self._samples.append(ms)
            self._count += 1
            self._total += ms
            if self._worst_ever is None or ms > self._worst_ever:
                self._worst_ever = ms

    def reset(self):
        with self._lock:
            self._samples.clear()
            self._count = 0
            self._worst_ever = None
            self._total = 0.0

    def snapshot(self):
        with self._lock:
            return list(self._samples), self._count, self._worst_ever, self._total

    def summary(self):
        samples, count, worst_ever, total = self.snapshot()
        if not samples:
            return {
                "p50_ms": None, "p95_ms": None, "max_ms": None,
                "mean_ms": None, "worst_ever_ms": worst_ever,
                "count": count, "window_n": 0,
            }
        ordered = sorted(samples)
        return {
            "p50_ms": round(percentile(ordered, 0.50), 3),
            "p95_ms": round(percentile(ordered, 0.95), 3),
            "max_ms": round(ordered[-1], 3),
            "mean_ms": round(total / count, 3) if count else None,
            "worst_ever_ms": round(worst_ever, 3) if worst_ever is not None else None,
            "count": count,
            "window_n": len(ordered),
        }


class LatencyTracker:
    """Per-stage latency for one audio session.

    Shared by `AudioSession` and `GroupAudioSession`; in the group case every
    bulb's sender feeds the same `bulb` stage, so the figure is the group's
    aggregate round-trip rather than N separate ones. That is the useful
    reading -- a group is only as responsive as its slowest bulb.
    """

    def __init__(self, block_period_ms, window=DEFAULT_WINDOW, target_ms=SOFTWARE_TARGET_MS):
        self.block_period_ms = float(block_period_ms)
        self.target_ms = float(target_ms)
        # Only these two are measured directly. Capture is derived -- see
        # `_capture_summary` and the module docstring for why a measured
        # median gap is the wrong number.
        self._stages = {"analysis": StageWindow(window), "bulb": StageWindow(window)}
        # How far past the nominal block period each delivery ran. Zero for a
        # block that arrived on or ahead of schedule.
        self._excess = StageWindow(window)
        # Raw inter-arrival gaps, kept for delivery health (burstiness, stalls).
        self._interval = StageWindow(window)
        self._window = window
        self._counter_lock = threading.Lock()
        self._frames = 0
        self._late = 0
        self._dropped = 0
        self._last_arrival = None

    # -- recording -----------------------------------------------------------
    def record_arrival(self, at):
        """Call at the top of the capture callback with a perf_counter value.

        Returns the measured interval in ms, or None for the first block of a
        session (which has no predecessor to measure against).
        """
        with self._counter_lock:
            self._frames += 1
            previous, self._last_arrival = self._last_arrival, at
        if previous is None:
            return None
        interval_ms = (at - previous) * 1000.0
        self._interval.record(interval_ms)
        # A block that arrived early is part of a burst, not a fast capture --
        # it contributes no excess, and the floor still applies to it.
        self._excess.record(max(0.0, interval_ms - self.block_period_ms))
        if interval_ms > self.block_period_ms * LATE_ARRIVAL_FACTOR:
            with self._counter_lock:
                self._late += 1
        return interval_ms

    def record_analysis(self, ms):
        self._stages["analysis"].record(ms)

    def record_bulb(self, ms):
        self._stages["bulb"].record(ms)

    def note_dropped(self, n=1):
        """Frames the pipeline knows it never got -- a PortAudio input
        overflow, or a block the bridge server dropped from a full queue."""
        if n <= 0:
            return
        with self._counter_lock:
            self._dropped += n

    def reset_stream(self):
        """A capture stream reopened, so the next block starts a new arrival
        chain. Without this the gap across a restart is charged as one
        enormous late frame that never actually happened to anyone."""
        with self._counter_lock:
            self._last_arrival = None

    # -- reading -------------------------------------------------------------
    def frame_counts(self):
        with self._counter_lock:
            frames, late, dropped = self._frames, self._late, self._dropped
        pct = round(100.0 * late / frames, 2) if frames else 0.0
        return {
            "processed": frames,
            "late": late,
            "dropped": dropped,
            "late_pct": pct,
            "late_threshold_ms": round(self.block_period_ms * LATE_ARRIVAL_FACTOR, 3),
        }

    def _capture_summary(self):
        """Capture latency = the block period, plus however far behind
        schedule delivery ran. See the module docstring for why this is
        derived rather than measured as a median gap."""
        excess = self._excess.summary()
        interval = self._interval.summary()
        period = round(self.block_period_ms, 3)

        def _plus(v):
            return round(period + v, 3) if v is not None else period

        return {
            "p50_ms": _plus(excess["p50_ms"]),
            "p95_ms": _plus(excess["p95_ms"]),
            "max_ms": _plus(excess["max_ms"]),
            "mean_ms": _plus(excess["mean_ms"]),
            "worst_ever_ms": _plus(excess["worst_ever_ms"]),
            "count": excess["count"],
            "window_n": excess["window_n"],
            # The structural part, split out so a reader can see how much of
            # the figure is configuration and how much is the source misbehaving.
            "floor_ms": period,
            "excess_p95_ms": excess["p95_ms"],
            # Delivery health. A bursty backend shows a tiny median gap and a
            # large p95 here; that is diagnostic, not a latency claim.
            "interval_p50_ms": interval["p50_ms"],
            "interval_p95_ms": interval["p95_ms"],
            "interval_mean_ms": interval["mean_ms"],
        }

    def summary(self):
        stages = {"capture": self._capture_summary()}
        for name, window in self._stages.items():
            stages[name] = window.summary()
        for name, entry in stages.items():
            entry["label"] = _STAGE_LABELS[name]
            entry["kind"] = "hardware" if name in _HARDWARE_STAGES else "software"

        def _sum(keys, field):
            parts = [stages[k][field] for k in keys if stages[k][field] is not None]
            return round(sum(parts), 3) if parts else None

        software_p50 = _sum(_SOFTWARE_STAGES, "p50_ms")
        software_p95 = _sum(_SOFTWARE_STAGES, "p95_ms")
        hardware_p50 = _sum(_HARDWARE_STAGES, "p50_ms")
        total_p50 = _sum(_SOFTWARE_STAGES + _HARDWARE_STAGES, "p50_ms")

        return {
            "stages": stages,
            "block_period_ms": round(self.block_period_ms, 3),
            "window": self._window,
            "frames": self.frame_counts(),
            "budget": {
                # The number the ≤10ms requirement is actually about.
                "software_p50_ms": software_p50,
                "software_p95_ms": software_p95,
                # The part no setting can fix. Shown, never folded away.
                "hardware_p50_ms": hardware_p50,
                "total_p50_ms": total_p50,
                "target_ms": self.target_ms,
                "within_target": (software_p50 is not None and software_p50 <= self.target_ms),
                "hardware_floor_note": (
                    "Bulb round-trip is a hardware floor -- no setting changes it."
                ),
            },
        }


class NullLatencyTracker:
    """No-op tracker so a `BulbSender` built without a session (tests, and the
    scheduler's one-off sends) needs no `if self.tracker is not None` guard on
    the hot path."""

    block_period_ms = 0.0

    def record_arrival(self, at):
        return None

    def record_analysis(self, ms):
        pass

    def record_bulb(self, ms):
        pass

    def note_dropped(self, n=1):
        pass

    def reset_stream(self):
        pass

    def frame_counts(self):
        return {"processed": 0, "late": 0, "dropped": 0, "late_pct": 0.0, "late_threshold_ms": 0.0}

    def summary(self):
        return None
