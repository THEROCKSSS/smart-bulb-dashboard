#!/usr/bin/env python
"""sbd-audio-bridge -- stream Windows audio into the Smart Bulb Dashboard.

The backend runs in a Linux container and cannot see Windows audio devices,
so an audio-reactive session there would start, report itself running, and
never react to sound. This tool captures on the host and streams raw PCM to
the backend's bridge listener, which feeds it to sessions exactly as a local
microphone would.

Quick start
-----------
Find out which device actually carries the sound you can hear -- play
something first, then::

    python tools/sbd-audio-bridge.py --probe

Then stream it::

    python tools/sbd-audio-bridge.py --device 85

`--list` shows the de-duplicated device list. `--probe` is usually the faster
route: it measures real signal instead of making you guess from names.

Capturing "what I hear"
-----------------------
sounddevice 0.5.5 does not expose WASAPI loopback, so there is no universal
"capture the speakers" device. On Windows the practical routes are:

* **Voicemeeter** -- its `Voicemeeter Out B1/B2/B3` buses appear as normal
  input devices carrying whatever is routed to them.
* **VB-Audio Virtual Cable** -- set `CABLE Input` as an app's (or the
  system's) output, then capture `CABLE Output`.
* **Stereo Mix** -- present on some sound cards, disabled by default.

`--probe` finds whichever of these is live on your machine without you having
to know which one your routing uses.

Latency
-------
Frames are sent as captured, with TCP_NODELAY, and stale frames are dropped
rather than queued -- queuing turns a brief stall into permanently growing
latency. The transport itself costs well under a millisecond on loopback.

The bulb is the real floor: its round-trip was measured at 111-152ms. Nothing
here changes that, and no setting in this tool can.
"""
import argparse
import queue
import socket
import struct
import sys
import threading
import time

import numpy as np

try:
    import sounddevice as sd
except Exception as exc:  # pragma: no cover - environment problem, not logic
    print(f"sounddevice is required: {exc}", file=sys.stderr)
    raise SystemExit(2)

MAGIC_HEADER = b"SBDA"
MAGIC_FRAME = b"FRM0"
PROTOCOL_VERSION = 1

TARGET_SAMPLE_RATE = 44100   # what the analysis pipeline expects
# Must track the backend's analysis HOP (audio_hop.DEFAULT_HOP_SIZE), not its
# old BLOCK_SIZE. Since issue #80 the backend decides a colour every hop, but a
# decision can never be fresher than the audio it is made from -- so shipping
# 512-sample frames put an 11.6ms floor under a 5.8ms pipeline no matter how
# short the hop was. Measured live on a bridge session with 512: software
# latency 12.3ms and `within_target: false`, with the dashboard correctly
# reporting `floor_source: "source delivery"` rather than blaming the hop.
#
# 256 @ 44100Hz = 5.8ms, matching the hop. Costs twice as many TCP writes
# (~172/s, ~344KB/s over loopback), which is nothing.
TARGET_BLOCK = 256
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8503

MME_NAME_LIMIT = 31
HOSTAPI_PREFERENCE = ("Windows WASAPI", "Windows DirectSound", "Windows WDM-KS", "MME")

_HEADER_STRUCT = struct.Struct("<4sBIBI")
_FRAME_HEADER_STRUCT = struct.Struct("<4sI")


class Resampler:
    """Continuous linear resampler, native capture rate -> pipeline rate.

    Needed because WASAPI shared mode will not resample for us: this machine's
    devices run at 48000 while the analysis pipeline is fixed at 44100, and
    asking PortAudio for 44100 fails outright with
    "Invalid sample rate [PaErrorCode -9997]".

    The ratio is not a whole number (48000/44100 = 1.088...), so the read
    position has to carry its fraction across block boundaries. Resetting it
    per block would drop or repeat a fraction of a sample every time, which
    accumulates into audible clicking and, worse here, a slow drift in the
    onset timing that tempo detection depends on.

    Linear interpolation is deliberate: this feeds band-energy analysis, not a
    listener. It costs almost nothing and its error sits far above the bands
    that matter for beat detection.
    """

    def __init__(self, in_rate, out_rate, channels):
        self.ratio = float(in_rate) / float(out_rate)
        self.channels = channels
        self.passthrough = (in_rate == out_rate)
        self._buf = np.zeros((0, channels), dtype=np.float32)
        self._pos = 0.0

    def feed(self, block):
        """Add captured frames; return a list of exactly-`TARGET_BLOCK` outputs."""
        if self.passthrough:
            self._buf = np.concatenate((self._buf, block)) if self._buf.size else block
            out = []
            while len(self._buf) >= TARGET_BLOCK:
                out.append(self._buf[:TARGET_BLOCK].copy())
                self._buf = self._buf[TARGET_BLOCK:]
            return out

        self._buf = np.concatenate((self._buf, block)) if self._buf.size else block.copy()
        out = []
        while True:
            # Last index linear interpolation will touch, +1 for the partner.
            needed = self._pos + (TARGET_BLOCK - 1) * self.ratio + 2
            if len(self._buf) < needed:
                break
            idx = self._pos + np.arange(TARGET_BLOCK, dtype=np.float64) * self.ratio
            base = np.floor(idx).astype(np.int64)
            frac = (idx - base).astype(np.float32)[:, None]
            resampled = self._buf[base] * (1.0 - frac) + self._buf[base + 1] * frac
            out.append(resampled.astype(np.float32))
            self._pos += TARGET_BLOCK * self.ratio
            consumed = int(np.floor(self._pos))
            if consumed:
                self._buf = self._buf[consumed:]
                self._pos -= consumed
        return out


def native_rate(device):
    """The rate the device actually runs at -- asking for anything else fails."""
    try:
        info = sd.query_devices(device) if device is not None else sd.query_devices(kind="input")
        return int(round(float(info.get("default_samplerate") or TARGET_SAMPLE_RATE)))
    except Exception:
        return TARGET_SAMPLE_RATE


# --------------------------------------------------------------- devices ---
def _hostapi_names():
    try:
        return {i: a.get("name", "") for i, a in enumerate(sd.query_hostapis())}
    except Exception:
        return {}


def _rank(api):
    try:
        return HOSTAPI_PREFERENCE.index(api)
    except ValueError:
        return len(HOSTAPI_PREFERENCE)


def _key(name):
    # Trailing strip matters: MME truncates at exactly 31 chars and the cut
    # often leaves a trailing space, which would otherwise make the same
    # device look like two.
    return " ".join((name or "").split()).lower()[:MME_NAME_LIMIT].strip()


def input_devices():
    """De-duplicated input devices, preferred host API first."""
    apis = _hostapi_names()
    raw = []
    for i, d in enumerate(sd.query_devices()):
        if d.get("max_input_channels", 0) > 0:
            raw.append({"index": i, "name": d.get("name"),
                        "channels": d.get("max_input_channels"),
                        "samplerate": d.get("default_samplerate"),
                        "hostapi": apis.get(d.get("hostapi"), "")})
    groups = {}
    for e in raw:
        groups.setdefault(_key(e["name"]), []).append(e)
    out = []
    for members in groups.values():
        members.sort(key=lambda e: (_rank(e["hostapi"]), e["index"]))
        best = dict(members[0])
        best["name"] = max((m["name"] or "" for m in members), key=len)
        best["aliases"] = [(m["index"], m["hostapi"]) for m in members[1:]]
        out.append(best)
    out.sort(key=lambda e: e["index"])
    return out


def cmd_list():
    devs = input_devices()
    print(f"{len(devs)} input devices (duplicates collapsed):\n")
    for d in devs:
        extra = f"  (+{len(d['aliases'])} other host APIs)" if d["aliases"] else ""
        print(f"  [{d['index']:>3}] {d['hostapi']:<22} {d['name']}{extra}")
    print("\nTip: --probe tells you which of these actually has sound on it.")
    return 0


def _measure(index, channels, seconds, samplerate=None):
    """Peak/RMS of a short capture. Returns None if the device won't open.

    Opens at the device's OWN rate. Probing everything at 44100 made every
    WASAPI device report "could not open", because WASAPI shared mode refuses
    a rate the device is not running at.
    """
    if samplerate is None:
        samplerate = native_rate(index)
    peak = {"v": 0.0, "rms": 0.0, "n": 0}

    def cb(indata, frames, t, status):
        a = np.abs(indata)
        if a.size:
            peak["v"] = max(peak["v"], float(a.max()))
            peak["rms"] += float(np.sqrt(np.mean(np.square(indata))))
            peak["n"] += 1

    try:
        with sd.InputStream(device=index, channels=channels, samplerate=samplerate,
                            blocksize=TARGET_BLOCK, callback=cb):
            time.sleep(seconds)
    except Exception:
        return None
    rms = peak["rms"] / peak["n"] if peak["n"] else 0.0
    return {"peak": peak["v"], "rms": rms}


def cmd_probe(seconds, samplerate=None):
    devs = input_devices()
    print("Probing every input device for signal.")
    print("PLAY SOMETHING NOW -- a device with nothing playing looks identical to a broken one.\n")
    results = []
    for d in devs:
        ch = min(2, d["channels"]) or 1
        got = _measure(d["index"], ch, seconds, None)
        if got is None:
            status, peak = "could not open", None
        elif got["peak"] < 0.0005:
            status, peak = "silent", got["peak"]
        else:
            status, peak = "SIGNAL", got["peak"]
        results.append((d, status, peak))
        mark = "*" if status == "SIGNAL" else " "
        shown = f"{peak:.4f}" if peak is not None else "  -   "
        print(f" {mark} [{d['index']:>3}] peak={shown}  {status:<14} {d['name'][:44]}")

    live = [(d, p) for d, s, p in results if s == "SIGNAL"]
    print()
    if not live:
        print("No device had signal.")
        print("  - Is audio actually playing right now?")
        print("  - For desktop audio you need Voicemeeter / VB-Cable routing;")
        print("    a plain microphone only hears the room.")
        return 1
    live.sort(key=lambda t: t[1], reverse=True)
    best = live[0][0]
    print(f"Loudest: [{best['index']}] {best['name']}")
    print(f"Stream it with:  python tools/sbd-audio-bridge.py --device {best['index']}")
    return 0


# Devices that carry what the computer is PLAYING, rather than what a
# microphone can hear in the room. Loopback wins even when a mic is louder:
# a mic picks up the room as well as the audio, so the lights end up reacting
# to a cough or a keyboard. Measured on this machine, naive "loudest wins"
# chose "Primary Sound Capture Driver" (a mic hearing the speakers, peak 0.53)
# over the clean Voicemeeter feed (peak 0.23).
_LOOPBACK_HINTS = (
    "cable output", "voicemeeter out", "stereo mix", "what u hear",
    "loopback", "wave out mix", "vb-audio", "virtual",
)


def _is_loopback_like(name):
    low = name.lower()
    return any(h in low for h in _LOOPBACK_HINTS)


def pick_loudest(seconds=0.6):
    """The best device with signal on it right now, or None.

    Exists because a hardcoded device index is a trap: the launcher shipped
    with `--device 85`, which is silent on this machine, so the bridge would
    connect, stream perfectly, and deliver nothing but zeros. The dashboard
    correctly reported "silent" and the natural conclusion was that the bridge
    was broken.

    "Best" is loopback-first, then loudest -- not simply loudest. See
    `_LOOPBACK_HINTS`.

    Quieter and faster than `cmd_probe` because it runs before every auto
    session rather than being asked for by a person.
    """
    candidates = []
    for d in input_devices():
        ch = min(2, d["channels"]) or 1
        got = _measure(d["index"], ch, seconds, None)
        if got is None or got["peak"] < 0.0005:
            continue
        candidates.append((d, got["peak"]))
    if not candidates:
        return None
    # Sort loopback-like first, then by peak within each tier.
    candidates.sort(key=lambda t: (_is_loopback_like(t[0]["name"]), t[1]), reverse=True)
    return candidates[0]
    return 0


def resolve_device(spec):
    """Accept an index or a case-insensitive name substring."""
    if spec is None:
        return None
    try:
        return int(spec)
    except (TypeError, ValueError):
        pass
    needle = str(spec).lower()
    matches = [d for d in input_devices() if needle in (d["name"] or "").lower()]
    if not matches:
        raise SystemExit(f"no input device matching {spec!r}. Try --list.")
    if len(matches) > 1:
        names = ", ".join(f"[{m['index']}] {m['name']}" for m in matches[:5])
        raise SystemExit(f"{spec!r} is ambiguous: {names}")
    return matches[0]["index"]


# ---------------------------------------------------------------- stream ---
class Streamer:
    """Captures from a device and streams it to the backend.

    Capture runs on PortAudio's thread and must never block, so it hands
    blocks to a small bounded queue. When the queue is full the OLDEST frame
    is discarded -- dropping audio is recoverable, growing latency is not.
    """

    def __init__(self, device, host, port, channels, quiet=False):
        self.device = device
        self.host = host
        self.port = port
        self.channels = channels
        # Capture at whatever the device actually runs at and convert here.
        # WASAPI shared mode refuses any other rate outright.
        self.capture_rate = native_rate(device)
        self.out_rate = TARGET_SAMPLE_RATE
        self.quiet = quiet
        self.q = queue.Queue(maxsize=8)
        self.sent = 0
        self.dropped = 0
        self.peak = 0.0
        self._resampler = None
        self._stop = threading.Event()

    def _push(self, block):
        try:
            self.q.put_nowait(block)
        except queue.Full:
            # Drop the OLDEST frame, never the newest: stale audio is worse
            # than missing audio, and queuing would grow latency permanently.
            try:
                self.q.get_nowait()
                self.dropped += 1
            except queue.Empty:
                pass
            try:
                self.q.put_nowait(block)
            except queue.Full:
                self.dropped += 1

    def _on_block(self, indata, frames, t, status):
        block = np.asarray(indata, dtype=np.float32)
        if block.size:
            p = float(np.abs(block).max())
            if p > self.peak:
                self.peak = p
        for out in self._resampler.feed(block.copy()):
            self._push(np.ascontiguousarray(out, dtype="<f4"))

    def _connect(self):
        sock = socket.create_connection((self.host, self.port), timeout=5)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        # Declare the rate we SEND at, not the rate we capture at -- the
        # resampler has already converted by the time anything is written.
        sock.sendall(_HEADER_STRUCT.pack(
            MAGIC_HEADER, PROTOCOL_VERSION, self.out_rate, self.channels, TARGET_BLOCK))
        return sock

    def run_once(self):
        info = sd.query_devices(self.device) if self.device is not None else {}
        name = info.get("name", "default") if isinstance(info, dict) else "default"
        self._resampler = Resampler(self.capture_rate, self.out_rate, self.channels)

        # Open the audio device BEFORE the socket. Connecting first meant a
        # device that refuses to open still produced a connect/disconnect on
        # every retry, so the dashboard indicator flickered "connected" for a
        # bridge that was never going to send a single frame.
        with sd.InputStream(device=self.device, channels=self.channels,
                            samplerate=self.capture_rate, blocksize=TARGET_BLOCK,
                            latency="low", callback=self._on_block):
            sock = self._connect()
            conv = ("passthrough" if self.capture_rate == self.out_rate
                    else f"resampled {self.capture_rate} -> {self.out_rate}")
            print(f"connected to {self.host}:{self.port}  <-  [{self.device}] {name}")
            print(f"  capture {self.capture_rate} Hz, {self.channels} ch ({conv}), {TARGET_BLOCK}-frame blocks")
            print("  streaming; Ctrl-C to stop\n")

            last_report = time.time()
            try:
                while not self._stop.is_set():
                    try:
                        block = self.q.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    payload = block.tobytes()
                    sock.sendall(_FRAME_HEADER_STRUCT.pack(MAGIC_FRAME, len(payload)))
                    sock.sendall(payload)
                    self.sent += 1

                    now = time.time()
                    if not self.quiet and now - last_report >= 1.0:
                        bar = "#" * min(24, int(self.peak * 48))
                        print(f"\r  frames {self.sent:>7}  dropped {self.dropped:<5} "
                              f"peak {self.peak:.3f} |{bar:<24}|", end="", flush=True)
                        self.peak = 0.0
                        last_report = now
            finally:
                try:
                    sock.close()
                except Exception:
                    pass

    def run_forever(self, retry_s=2.0):
        """Reconnect on failure. The container restarts (restart: unless-stopped),
        and a bridge that dies on first disconnect turns every restart into a
        silently dead audio session."""
        while not self._stop.is_set():
            try:
                self.run_once()
            except KeyboardInterrupt:
                raise
            except (ConnectionRefusedError, OSError) as e:
                print(f"\n  not connected ({e}); retrying in {retry_s:.0f}s", flush=True)
            except Exception as e:
                print(f"\n  bridge error: {e}; retrying in {retry_s:.0f}s", flush=True)
            if self._stop.wait(retry_s):
                return

    def stop(self):
        self._stop.set()


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="sbd-audio-bridge",
        description="Stream Windows audio into the Smart Bulb Dashboard running in Docker.")
    ap.add_argument("--list", action="store_true", help="list input devices (duplicates collapsed)")
    ap.add_argument("--probe", action="store_true", help="find which device actually has sound on it")
    ap.add_argument("--probe-seconds", type=float, default=1.0, help="probe duration per device")
    ap.add_argument("--device", help="device index, or part of its name")
    ap.add_argument("--auto", action="store_true",
                    help="pick whichever device actually has sound on it right now")
    ap.add_argument("--host", default=DEFAULT_HOST, help=f"backend host (default {DEFAULT_HOST})")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"bridge port (default {DEFAULT_PORT})")
    ap.add_argument("--channels", type=int, default=2, choices=(1, 2), help="capture channels")
    ap.add_argument("--quiet", action="store_true", help="no level meter")
    args = ap.parse_args(argv)

    if args.list:
        return cmd_list()
    if args.probe:
        return cmd_probe(args.probe_seconds)

    if args.auto and not args.device:
        print("Finding which device actually has sound on it...")
        found = pick_loudest()
        if found is None:
            print("\nNo device has any signal right now.")
            print("  - Is audio actually playing?")
            print("  - Desktop audio needs Voicemeeter / VB-Cable routing;")
            print("    a plain microphone only hears the room.")
            print("  - Run with --probe for the full per-device breakdown.")
            return 1
        best, peak = found
        print(f"Using [{best['index']}] {best['name']}  (peak {peak:.4f})\n")
        args.device = str(best["index"])

    device = resolve_device(args.device)
    channels = args.channels
    if device is not None:
        info = sd.query_devices(device)
        maxch = info.get("max_input_channels", 1)
        if maxch < channels:
            channels = max(1, maxch)

    # Capture straight at the pipeline's rate: PortAudio converts from the
    # device's native rate for us, so the container never sees 48kHz audio
    # mislabelled as 44.1k -- which would skew every tempo estimate.
    streamer = Streamer(device, args.host, args.port, channels, quiet=args.quiet)
    try:
        streamer.run_forever()
    except KeyboardInterrupt:
        streamer.stop()
        print(f"\nstopped. {streamer.sent} frames sent, {streamer.dropped} dropped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
