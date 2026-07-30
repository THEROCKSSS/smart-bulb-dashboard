"""Light-show export/replay (Week 1 Phase D, section 12 -- deliberately
scoped down from the full roadmap ask; see module docstring at the bottom
and the round report for what was explicitly skipped and why).

Captures the sequence of (timestamp, hue, saturation, brightness) actions a
session actually *sent* to a bulb during a run (captured in
`audio_reactive.BulbSender`, see `_captured` there), exports it as a JSON
file, and replays it later without needing live audio -- reusing
`BulbSender` itself for the replay so it gets the same dwell-pacing and
short-socket-timeout protection real audio-reactive sends get.
"""
import json
import os
import threading
import time
import uuid

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
LIGHTSHOWS_DIR = os.path.join(DATA_DIR, "lightshows")
os.makedirs(LIGHTSHOWS_DIR, exist_ok=True)

_lock = threading.Lock()


def _path_for(lightshow_id):
    return os.path.join(LIGHTSHOWS_DIR, f"{lightshow_id}.json")


def export_lightshow(device_id, name, points):
    """`points` is a list of {"t": relative_seconds, "h":, "s":, "v":}
    dicts, in chronological order (as produced by
    audio_reactive.AudioSession.get_captured_points()). Returns the saved
    record (including the full points list, so a caller can act on it
    immediately without a second read)."""
    if not points:
        raise ValueError("no captured actions to export -- run a session first")
    with _lock:
        lightshow_id = str(uuid.uuid4())[:8]
        record = {
            "id": lightshow_id,
            "name": name,
            "device_id": device_id,
            "created_at": time.time(),
            "duration_s": round(points[-1]["t"], 3) if points else 0.0,
            "point_count": len(points),
            "points": points,
        }
        with open(_path_for(lightshow_id), "w") as f:
            json.dump(record, f, indent=2)
        return record


def list_lightshows(device_id=None):
    """Metadata only (no points list, which can be large)."""
    out = []
    for fname in sorted(os.listdir(LIGHTSHOWS_DIR)):
        if not fname.endswith(".json"):
            continue
        try:
            with open(os.path.join(LIGHTSHOWS_DIR, fname), "r") as f:
                record = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        if device_id and record.get("device_id") != device_id:
            continue
        out.append({k: v for k, v in record.items() if k != "points"})
    return out


def get_lightshow(lightshow_id):
    path = _path_for(lightshow_id)
    if not os.path.exists(path):
        return None
    with open(path, "r") as f:
        return json.load(f)


def delete_lightshow(lightshow_id):
    path = _path_for(lightshow_id)
    if os.path.exists(path):
        os.remove(path)
        return True
    return False


class LightshowReplay:
    """Plays back a captured points list against a real controller, using a
    `BulbSender` (imported lazily to avoid a module-load cycle with
    audio_reactive, which does not import this module) so replay gets the
    same dwell pacing and short send-timeout as a live session."""

    def __init__(self, controller, points, loop=False):
        import audio_reactive  # local import: one-directional dependency

        self.controller = controller
        self.points = points
        self.loop = loop
        self._stop = threading.Event()
        self._thread = None
        self._sender = audio_reactive.BulbSender(controller, min_dwell_ms=audio_reactive.MIN_DWELL_FLOOR_MS)

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        try:
            while not self._stop.is_set():
                t0 = time.time()
                for p in self.points:
                    target_at = t0 + p["t"]
                    wait_s = target_at - time.time()
                    if wait_s > 0 and self._stop.wait(wait_s):
                        return
                    self._sender.queue(("hsv", p["h"], p["s"], p["v"]))
                if not self.loop:
                    return
        finally:
            self.controller._log("lightshow_replay_stop", {})

    def stop(self):
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        self._sender.stop()

    def is_alive(self):
        return self._thread is not None and self._thread.is_alive()

    def status(self):
        return {"active": self.is_alive(), "loop": self.loop, "point_count": len(self.points),
                "sender": self._sender.status()}


# ------------------------------------------------------------- registry ---
_replays = {}
_replays_lock = threading.Lock()


def start_replay(device_id, controller, lightshow_id, loop=False):
    record = get_lightshow(lightshow_id)
    if not record:
        raise ValueError(f"unknown lightshow '{lightshow_id}'")
    with _replays_lock:
        existing = _replays.get(device_id)
        if existing and existing.is_alive():
            existing.stop()
        replay = LightshowReplay(controller, record["points"], loop=loop)
        _replays[device_id] = replay
        replay.start()
        controller._log("lightshow_replay_start", {"lightshow_id": lightshow_id, "loop": loop})
        return replay


def stop_replay(device_id):
    with _replays_lock:
        replay = _replays.pop(device_id, None)
    if replay:
        replay.stop()
        return True
    return False


def get_replay_status(device_id):
    with _replays_lock:
        replay = _replays.get(device_id)
    if not replay:
        return {"active": False}
    return replay.status()


# --- explicitly out of scope this round (section 12) ------------------------
# A full embedded music player (playlist, waveform scrubber, audio format
# support) is a large, separate frontend feature outside this project's
# core scope (bulb control) -- a shallow version would just be a broken
# stub, so it isn't built here. Playlist management, a scrubber UI, and
# any Spotify/streaming integration are likewise skipped for the same
# reason. Only the piece that's a natural, genuinely useful extension of
# the *existing* audio-reactive pipeline -- capturing and replaying a
# session's actual color-over-time output -- is implemented above.
