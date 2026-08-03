"""Capture sources: where an AudioSession's samples come from.

`AudioSession` used to construct `sd.InputStream` itself, which meant the
capture path could only be exercised with real hardware attached -- and meant
there was nowhere to plug in audio arriving over the network from the host
bridge.

A capture source is deliberately tiny: a context manager that, while open,
calls `callback(block)` with a `(frames, channels)` float32 array. That is the
whole contract, and it is the same shape `sd.InputStream`'s callback already
delivered, so `AudioSession._process` needed no change at all.

Prior art: `audio_signal.calibrate_from_device()` already accepts an
injectable `capture_fn` for exactly this reason.

Three implementations:

* `SoundDeviceSource` -- the real microphone path, unchanged behaviour
  including the stereo-then-mono fallback for devices that refuse two
  channels.
* `NetworkSource` -- subscribes to the bridge server, so audio captured on the
  Windows host drives a session running inside the container.
* `CallableSource` -- for tests: drives a session from synthetic audio with no
  device, no socket and no bulb.
"""
import threading

import numpy as np


class CaptureError(Exception):
    """Raised when a source cannot be opened, with a message meant for a user."""


class SoundDeviceSource:
    """Captures from a local audio device via sounddevice/PortAudio."""

    kind = "device"

    def __init__(self, device_index, channels, samplerate, blocksize, callback,
                 latency="low", on_dropped=None):
        self.device_index = device_index
        self.requested_channels = channels
        self.channels = channels
        self.samplerate = samplerate
        self.blocksize = blocksize
        self.callback = callback
        self.latency = latency
        self.on_dropped = on_dropped
        self.overflows = 0
        self._stream = None

    def __enter__(self):
        import sounddevice as sd

        def _cb(indata, frames, time_info, status_flags):
            # PortAudio has been telling us about dropped input all along and
            # this callback threw the flag away. An overflow means samples
            # existed and never reached analysis -- invisible in every other
            # metric, and exactly the thing that makes reactive lighting feel
            # like it "skips" for no traceable reason.
            if status_flags and getattr(status_flags, "input_overflow", False):
                self.overflows += 1
                if self.on_dropped is not None:
                    self.on_dropped(1)
            self.callback(indata)

        channels = self.requested_channels
        try:
            stream = sd.InputStream(device=self.device_index, channels=channels,
                                    samplerate=self.samplerate, blocksize=self.blocksize,
                                    latency=self.latency, callback=_cb)
        except Exception:
            if channels == 2:
                # Fall back to mono if the device can't do stereo. Preserved
                # from the original inline implementation -- several of this
                # machine's inputs are mono-only.
                channels = 1
                stream = sd.InputStream(device=self.device_index, channels=channels,
                                        samplerate=self.samplerate, blocksize=self.blocksize,
                                        latency=self.latency, callback=_cb)
            else:
                raise
        self.channels = channels
        self._stream = stream
        stream.__enter__()
        return self

    def __exit__(self, *exc):
        stream, self._stream = self._stream, None
        if stream is not None:
            return stream.__exit__(*exc)
        return False

    def status(self):
        return {"kind": self.kind, "device_index": self.device_index,
                "channels": self.channels, "overflows": self.overflows}


class NetworkSource:
    """Consumes frames from the host-side bridge via the bridge server.

    Opening this source does NOT wait for a bridge to connect -- a session can
    legitimately start before the bridge does, and the bridge auto-reconnects.
    What it does refuse is the case where the bridge listener isn't running at
    all, because then no audio can ever arrive and the old silent no-op would
    be back.
    """

    kind = "bridge"

    def __init__(self, callback, server=None, require_connected=False, on_dropped=None):
        self.callback = callback
        self._server = server
        self._sub = None
        self._frames = 0
        self.on_dropped = on_dropped
        self._seen_drops = 0
        self._lock = threading.Lock()

    def __enter__(self):
        import audio_bridge

        server = self._server or audio_bridge.get_server()
        if server is None:
            raise CaptureError(
                "the audio bridge listener is not running, so no audio can reach this "
                "session. Start the backend with the bridge enabled, then run "
                "tools/sbd-audio-bridge.py on the Windows host.")
        self._server = server

        def _cb(block):
            with self._lock:
                self._frames += 1
                # The server already counts frames it dropped from a full
                # subscriber queue; forward the delta so a bridge drop and a
                # device overflow land in one number. A plain int read, no
                # status() dict built on the hot path.
                total = getattr(server, "_drops", 0)
                delta, self._seen_drops = total - self._seen_drops, total
            if delta > 0 and self.on_dropped is not None:
                self.on_dropped(delta)
            self.callback(block)

        self._sub = server.subscribe(_cb)
        return self

    def __exit__(self, *exc):
        if self._server is not None and self._sub is not None:
            self._server.unsubscribe(self._sub)
        self._sub = None
        return False

    def status(self):
        st = {"kind": self.kind, "frames_received": self._frames}
        if self._server is not None:
            bs = self._server.status()
            st.update({"connected": bs.get("connected"), "streaming": bs.get("streaming"),
                       "drops": bs.get("drops")})
        return st


class CallableSource:
    """Test source: pulls blocks from an iterable of arrays.

    Runs a background thread so the session's own lifecycle (start, stop,
    silence timeout) is exercised the same way it is with a real device,
    rather than being short-circuited by a synchronous loop.
    """

    kind = "callable"

    def __init__(self, blocks, callback, interval_s=0.0, loop=False):
        self.blocks = list(blocks)
        self.callback = callback
        self.interval_s = interval_s
        self.loop = loop
        self._stop = threading.Event()
        self._thread = None
        self.delivered = 0

    def __enter__(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._pump, name="callable-source", daemon=True)
        self._thread.start()
        return self

    def _pump(self):
        while not self._stop.is_set():
            for block in self.blocks:
                if self._stop.is_set():
                    return
                arr = np.asarray(block, dtype=np.float32)
                if arr.ndim == 1:
                    arr = arr.reshape(-1, 1)
                self.callback(arr)
                self.delivered += 1
                if self.interval_s:
                    if self._stop.wait(self.interval_s):
                        return
            if not self.loop:
                return

    def __exit__(self, *exc):
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2.0)
        return False

    def status(self):
        return {"kind": self.kind, "delivered": self.delivered}
