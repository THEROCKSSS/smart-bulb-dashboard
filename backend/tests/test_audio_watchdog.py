"""Week 1 Phase D, section 9: socket-timeout + watchdog behavior for a
simulated slow/hanging bulb send.

This is the known unresolved limitation documented in
iterations/003-audio-engine-v2/README.md: a slow/offline bulb used to tie
up its BulbSender thread for a long single send attempt (tinytuya's default
5s connection timeout), delaying how quickly the sender picks up a fresh
value once the bulb recovers. Two independent mitigations are tested here:

1. `set_socket_timeout` is actually called with the short, explicit
   AUDIO_SEND_SOCKET_TIMEOUT_S when a BulbSender is created (and restored
   to the default when it stops) -- this is what bounds a *single* slow
   send attempt.
2. The watchdog: if a send genuinely hangs past WATCHDOG_STALL_S (e.g. a
   buggy/blocking controller call that ignores the timeout), the sender
   thread is detected as stalled and restarted, and the sender keeps
   accepting/delivering fresh queued values afterward.
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import audio_reactive  # noqa: E402


class SocketTimeoutRecordingController:
    """Records every set_socket_timeout call so we can assert BulbSender
    actually applies (and restores) the short audio-specific timeout."""

    def __init__(self, device_id="bulb-1"):
        self.cfg = {"id": device_id}
        self.timeout_calls = []
        self.send_calls = []

    def set_socket_timeout(self, seconds):
        self.timeout_calls.append(seconds)

    def set_hsv(self, h, s, v):
        self.send_calls.append((h, s, v))

    def _log(self, *a, **k):
        pass


def test_bulbsender_applies_short_audio_socket_timeout_and_restores_default():
    controller = SocketTimeoutRecordingController()
    sender = audio_reactive.BulbSender(controller, min_dwell_ms=audio_reactive.MIN_DWELL_FLOOR_MS)
    try:
        assert controller.timeout_calls[0] == audio_reactive.AUDIO_SEND_SOCKET_TIMEOUT_S
        assert audio_reactive.AUDIO_SEND_SOCKET_TIMEOUT_S < audio_reactive.DEFAULT_SOCKET_TIMEOUT_S, (
            "the audio-specific timeout must be *shorter* than tinytuya's normal default -- "
            "that's the entire point (bound a single slow send)"
        )
    finally:
        sender.stop()
    assert controller.timeout_calls[-1] == audio_reactive.DEFAULT_SOCKET_TIMEOUT_S, (
        "the normal (longer) default timeout must be restored on stop so a subsequent "
        "manual command isn't stuck with the short audio-only timeout"
    )


class HangingThenRecoveringController:
    """Simulates a bulb whose send hangs for longer than the watchdog
    threshold on its first call (as if the socket timeout itself failed to
    fire, e.g. a buggy driver path) and then behaves normally afterward --
    exactly the scenario the watchdog exists to recover from."""

    def __init__(self, hang_s, device_id="bulb-1"):
        self.cfg = {"id": device_id}
        self.hang_s = hang_s
        self.call_count = 0
        self.received = []
        self._lock = threading.Lock()

    def set_socket_timeout(self, seconds):
        pass

    def set_hsv(self, h, s, v):
        with self._lock:
            self.call_count += 1
            first_call = self.call_count == 1
        if first_call:
            time.sleep(self.hang_s)  # simulate one genuinely stuck send
        with self._lock:
            self.received.append((h, s, v))

    def _log(self, *a, **k):
        pass


def test_watchdog_restarts_a_stalled_sender_and_keeps_delivering_values(monkeypatch):
    """A short, test-scale watchdog stall threshold + poll interval so this
    doesn't need to actually wait out the real WATCHDOG_STALL_S (6s)
    constant, and isn't racy against the real 1s poll interval: patch the
    poll interval down to 0.1s (many detection opportunities) and the
    stall threshold to 0.3s, then simulate a send that hangs for 2s -- a
    large margin over the stall threshold so there's no race between
    detection and the hang naturally resolving on its own."""
    monkeypatch.setattr(audio_reactive, "WATCHDOG_STALL_S", 0.3)
    monkeypatch.setattr(audio_reactive, "WATCHDOG_POLL_INTERVAL_S", 0.1)
    controller = HangingThenRecoveringController(hang_s=2.0)
    sender = audio_reactive.BulbSender(controller, min_dwell_ms=audio_reactive.MIN_DWELL_FLOOR_MS)
    try:
        sender.queue(("hsv", 10, 100, 50))  # this call will hang for 1s inside the first (now-orphaned) thread

        # Give the watchdog a chance to notice the stall (poll rather than
        # a single fixed sleep, since exact timing depends on scheduling).
        deadline = time.time() + 4.0
        restarted = False
        while time.time() < deadline:
            if sender._restart_count > 0:
                restarted = True
                break
            time.sleep(0.05)
        assert restarted, "watchdog never restarted the stalled sender within 3s"

        # After the restart, the sender must still accept and deliver a
        # *fresh* queued value -- proving it's genuinely usable again, not
        # just marked as restarted.
        sender.queue(("hsv", 99, 100, 80))
        deadline = time.time() + 2.0
        delivered = False
        while time.time() < deadline:
            if any(v == (99, 100, 80) for v in controller.received):
                delivered = True
                break
            time.sleep(0.05)
        assert delivered, f"sender never delivered the fresh value after restart; received={controller.received}"

        status = sender.status()
        assert status["restart_count"] >= 1
    finally:
        sender.stop()


def test_bulbsender_status_reports_zero_restarts_when_healthy():
    controller = SocketTimeoutRecordingController()
    sender = audio_reactive.BulbSender(controller, min_dwell_ms=audio_reactive.MIN_DWELL_FLOOR_MS)
    try:
        sender.queue(("hsv", 5, 100, 40))
        time.sleep(0.2)
        assert sender.status()["restart_count"] == 0
    finally:
        sender.stop()
