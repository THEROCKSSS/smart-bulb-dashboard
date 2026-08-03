"""Audio bridge server: accepts PCM frames from a host-side capture tool.

Why this exists
---------------
The backend runs in a Linux container on Docker Desktop for Windows, which has
no access to the host's audio devices. An audio-reactive session started in
the container therefore succeeds, reports itself running, and never reacts to
sound -- a silent no-op, the worst kind of failure.

This module is the receiving half of the fix. A small Windows-side tool
(`tools/sbd-audio-bridge.py`) captures audio and streams raw PCM here over
loopback TCP; sessions then consume it exactly as they would a local
microphone.

Design decisions worth keeping
------------------------------
* **Loopback only.** The listener binds 127.0.0.1 (inside the container, the
  published port is bound to 127.0.0.1 on the host too). The payload is live
  microphone or desktop audio and must not be reachable from the LAN.

* **Latest-wins, never queue.** If consumers fall behind, the oldest frames
  are dropped. Queuing converts a momentary CPU spike into permanently growing
  latency, which is precisely the failure this whole feature exists to avoid.
  Drops are counted and surfaced, never silent.

* **The listener outlives sessions.** A bridge can be connected with no audio
  session running -- that is exactly the state the dashboard's connectivity
  indicator needs to report. Sessions subscribe and unsubscribe; the
  connection is independent of them.

* **One bridge at a time.** A second connection is refused rather than
  silently interleaving two audio streams into one analysis pipeline.

Wire format
-----------
All little-endian. Header once per connection, then frames until close::

    header: b"SBDA" | version u8 | sample_rate u32 | channels u8 | block u32
    frame:  b"FRM0" | payload_len u32 | payload (float32 * block * channels)

`FRM0` per frame is what makes resynchronisation possible: a receiver that
loses alignment scans forward for the marker rather than reinterpreting
arbitrary bytes as audio (which is how you get NaN/Inf reaching a bulb).
"""
import socket
import struct
import threading
import time

import numpy as np

MAGIC_HEADER = b"SBDA"
MAGIC_FRAME = b"FRM0"
PROTOCOL_VERSION = 1

DEFAULT_BRIDGE_PORT = 8503
DEFAULT_BIND_HOST = "0.0.0.0"  # inside the container; the published port is loopback-bound

# A frame older than this means the bridge is connected but has stopped
# sending -- distinct from "not connected", and worth showing differently.
STALE_AFTER_S = 2.0

_HEADER_STRUCT = struct.Struct("<4sBIBI")
_FRAME_HEADER_STRUCT = struct.Struct("<4sI")

# Refuse absurd frames rather than trying to allocate them.
MAX_PAYLOAD_BYTES = 1 << 20


class BridgeServer:
    """Accepts one capture client and fans its frames out to subscribers."""

    def __init__(self, host=DEFAULT_BIND_HOST, port=DEFAULT_BRIDGE_PORT, expected_sample_rate=None):
        self.host = host
        self.port = port
        self.expected_sample_rate = expected_sample_rate

        self._sock = None
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

        self._subscribers = {}
        self._next_sub_id = 1

        self._client_addr = None
        self._connected_at = None
        self._last_frame_at = None
        self._frames = 0
        self._drops = 0
        self._sample_rate = None
        self._channels = None
        self._block_size = None
        self._last_error = None
        self._peak = 0.0
        self._listening = False
        self._client_thread = None

    # ------------------------------------------------------------ lifecycle
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._serve, name="audio-bridge", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        sock, self._sock = self._sock, None
        if sock:
            try:
                sock.close()
            except Exception:
                pass

    # ----------------------------------------------------------- subscribe
    def subscribe(self, callback):
        """Register `callback(block)` where block is (frames, channels) float32.

        Returns a token to pass to unsubscribe(). Callbacks run on the bridge
        reader thread, so they must be cheap and must not raise -- an
        exception in one subscriber must not tear down the connection for
        everyone else.
        """
        with self._lock:
            sub_id = self._next_sub_id
            self._next_sub_id += 1
            self._subscribers[sub_id] = callback
        return sub_id

    def unsubscribe(self, sub_id):
        with self._lock:
            self._subscribers.pop(sub_id, None)

    @property
    def subscriber_count(self):
        with self._lock:
            return len(self._subscribers)

    # -------------------------------------------------------------- status
    def status(self):
        now = time.time()
        last = self._last_frame_at
        connected = self._client_addr is not None
        age = (now - last) if last else None
        return {
            "listening": self._listening,
            "port": self.port,
            "connected": connected,
            # "connected but gone quiet" is a different problem from "not
            # connected", and the UI should be able to say so.
            "streaming": bool(connected and age is not None and age < STALE_AFTER_S),
            "client": self._client_addr,
            "connected_at": self._connected_at,
            "uptime_s": round(now - self._connected_at, 1) if self._connected_at else None,
            "last_frame_age_s": round(age, 3) if age is not None else None,
            "frames": self._frames,
            "drops": self._drops,
            "sample_rate": self._sample_rate,
            "channels": self._channels,
            "block_size": self._block_size,
            "peak": round(self._peak, 5),
            "subscribers": self.subscriber_count,
            "error": self._last_error,
        }

    # --------------------------------------------------------------- serve
    def _serve(self):
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind((self.host, self.port))
            self._sock.listen(1)
            self._sock.settimeout(0.5)
            self._listening = True
        except Exception as e:
            self._last_error = f"could not listen on {self.host}:{self.port}: {e}"
            self._listening = False
            return

        while not self._stop.is_set():
            try:
                conn, addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            if self._client_addr is not None:
                # One bridge at a time: two streams into one analysis
                # pipeline would interleave into noise.
                try:
                    conn.close()
                except Exception:
                    pass
                continue

            try:
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except Exception:
                pass
            # Claim the slot HERE, not inside the worker: the accept loop can
            # come back round before a freshly-spawned thread has run its
            # first line, and then two clients both look like "the first one".
            self._client_addr = f"{addr[0]}:{addr[1]}"
            # Read on a worker so the accept loop keeps running. Handling the
            # client inline blocked accept() for the whole session, which
            # meant a second bridge was not actually refused -- it sat in the
            # listen backlog and silently took over whenever the first
            # disconnected. Found by the test that asserts refusal.
            self._client_thread = threading.Thread(
                target=self._handle_client, args=(conn, addr),
                name="audio-bridge-client", daemon=True)
            self._client_thread.start()

        self._listening = False

    def _handle_client(self, conn, addr):
        # _client_addr was already claimed by the accept loop; setting it
        # again here would reopen the race it exists to close.
        self._connected_at = time.time()
        self._frames = 0
        self._drops = 0
        self._peak = 0.0
        self._last_frame_at = None
        self._last_error = None
        try:
            conn.settimeout(5.0)
            self._read_stream(conn)
        except Exception as e:
            self._last_error = str(e)
        finally:
            try:
                conn.close()
            except Exception:
                pass
            self._client_addr = None
            self._connected_at = None
            self._sample_rate = None
            self._channels = None
            self._block_size = None

    def _recv_exactly(self, conn, n):
        buf = bytearray()
        while len(buf) < n:
            if self._stop.is_set():
                return None
            chunk = conn.recv(n - len(buf))
            if not chunk:
                return None
            buf.extend(chunk)
        return bytes(buf)

    def _read_stream(self, conn):
        raw = self._recv_exactly(conn, _HEADER_STRUCT.size)
        if not raw:
            return
        magic, version, sample_rate, channels, block = _HEADER_STRUCT.unpack(raw)
        if magic != MAGIC_HEADER:
            raise ValueError(f"bad header magic {magic!r}")
        if version != PROTOCOL_VERSION:
            raise ValueError(f"unsupported bridge protocol version {version}")
        if channels < 1 or channels > 2:
            raise ValueError(f"unsupported channel count {channels}")
        if self.expected_sample_rate and sample_rate != self.expected_sample_rate:
            # The bridge resamples on the host precisely so this cannot
            # happen; if it does, tempo and pitch would be skewed and it is
            # better to refuse than to silently analyse the wrong thing.
            raise ValueError(
                f"bridge sample rate {sample_rate} != expected {self.expected_sample_rate}; "
                "the bridge must resample on the host")

        self._sample_rate = sample_rate
        self._channels = channels
        self._block_size = block

        while not self._stop.is_set():
            head = self._recv_exactly(conn, _FRAME_HEADER_STRUCT.size)
            if not head:
                return
            magic, payload_len = _FRAME_HEADER_STRUCT.unpack(head)
            if magic != MAGIC_FRAME:
                if not self._resync(conn):
                    return
                continue
            if payload_len == 0 or payload_len > MAX_PAYLOAD_BYTES or payload_len % 4:
                if not self._resync(conn):
                    return
                continue
            payload = self._recv_exactly(conn, payload_len)
            if payload is None:
                return
            self._dispatch(payload, channels)

    def _resync(self, conn):
        """Scan forward for the next frame marker after corruption."""
        window = bytearray()
        deadline = time.time() + 5.0
        while not self._stop.is_set() and time.time() < deadline:
            byte = conn.recv(1)
            if not byte:
                return False
            window.extend(byte)
            if len(window) > len(MAGIC_FRAME):
                del window[0]
            if bytes(window) == MAGIC_FRAME:
                head = self._recv_exactly(conn, 4)
                if not head:
                    return False
                (payload_len,) = struct.unpack("<I", head)
                if 0 < payload_len <= MAX_PAYLOAD_BYTES and payload_len % 4 == 0:
                    payload = self._recv_exactly(conn, payload_len)
                    if payload is None:
                        return False
                    self._dispatch(payload, self._channels or 1)
                    return True
        return False

    def _dispatch(self, payload, channels):
        samples = np.frombuffer(payload, dtype="<f4")
        if samples.size % channels:
            self._drops += 1
            return
        block = samples.reshape(-1, channels).astype(np.float32, copy=True)

        # Never let a corrupt frame reach the analysis pipeline. The fuzz
        # suite already guarantees analyze_frame survives NaN/Inf, but a
        # silently-wrong colour is still worse than a dropped frame.
        if not np.all(np.isfinite(block)):
            self._drops += 1
            return

        self._frames += 1
        self._last_frame_at = time.time()
        try:
            self._peak = float(np.abs(block).max())
        except ValueError:
            self._peak = 0.0

        with self._lock:
            subscribers = list(self._subscribers.values())
        for cb in subscribers:
            try:
                cb(block)
            except Exception:
                # One bad subscriber must not kill the connection for the
                # others, nor stop the status indicator updating.
                self._drops += 1


# Process-wide instance. The listener is started from the app's lifespan hook
# so that the connectivity indicator works even with no session running.
_server = None


def get_server():
    return _server


def start_server(host=DEFAULT_BIND_HOST, port=DEFAULT_BRIDGE_PORT, expected_sample_rate=None):
    global _server
    if _server is None:
        _server = BridgeServer(host=host, port=port, expected_sample_rate=expected_sample_rate)
    _server.start()
    return _server


def stop_server():
    global _server
    if _server is not None:
        _server.stop()
        _server = None


def status():
    if _server is None:
        return {"listening": False, "connected": False, "streaming": False,
                "port": DEFAULT_BRIDGE_PORT, "frames": 0, "drops": 0,
                "subscribers": 0, "error": None}
    return _server.status()
