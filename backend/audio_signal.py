"""Signal conditioning for the audio-reactive pipeline: automatic gain
control (AGC), a noise gate, clipping/overload detection, DC offset removal,
multi-band independent gain, a peak-hold input level meter, and per-device
saved calibration (a "sample a few seconds of room silence" flow that picks
a sane noise-gate floor automatically).

Deliberately separate from `audio_reactive.py`: this module owns *signal
conditioning* (what happens to raw samples before they reach the FFT/mode
logic), not color-mode decisions or bulb dispatch. `AudioSession` in
audio_reactive.py owns one `SignalConditioner` instance and runs every
captured block through it before calling `analyze_frame()`.

Everything here is pure/stateful-but-deterministic and fully testable
without a real audio device -- see backend/tests/audio_fixtures.py and
backend/tests/test_audio_signal.py.
"""
import json
import math
import os
import threading
import time

import numpy as np

SAMPLE_RATE = 44100

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)
CALIBRATION_PATH = os.path.join(DATA_DIR, "audio_calibration.json")

# --- AGC defaults ---
# Target level is deliberately well below 1.0 (full scale) -- AGC drives the
# average level here, but real music/voice has transients well above its
# own RMS, so leaving headroom keeps those transients from immediately
# tripping the clip detector once AGC has boosted a quiet source.
DEFAULT_AGC_TARGET_RMS = 0.15
DEFAULT_AGC_ATTACK_MS = 50.0     # gain moving DOWN (loud signal) -- fast, to protect against clipping
DEFAULT_AGC_RELEASE_MS = 400.0   # gain moving UP (quiet signal) -- slow, so it doesn't "pump" audibly
DEFAULT_AGC_MIN_GAIN = 0.1
DEFAULT_AGC_MAX_GAIN = 8.0

# --- noise gate ---
DEFAULT_NOISE_GATE_FLOOR = 0.0015  # rms below this = gated (treated as silence)

# --- clipping / overload ---
CLIP_SAMPLE_THRESHOLD = 0.98     # |sample| at/above this counts as "at the rail"
CLIP_FRACTION_WARN = 0.001       # fraction of a block at/above the rail that flags overload

# --- peak-hold meter ---
PEAK_HOLD_DECAY_PER_S = 1.5  # linear decay rate (full-scale units/sec) once a hold expires

# --- calibration ---
CALIBRATION_SAFETY_MARGIN = 1.6   # noise_gate_floor = measured silence rms * this margin
CALIBRATION_MIN_FLOOR = 0.0002


_cal_lock = threading.Lock()


def _default_calibration_state():
    return {"devices": {}}


def _load_calibration_state():
    if not os.path.exists(CALIBRATION_PATH):
        return _default_calibration_state()
    with open(CALIBRATION_PATH, "r") as f:
        data = json.load(f)
    data.setdefault("devices", {})
    return data


def _save_calibration_state(state):
    with open(CALIBRATION_PATH, "w") as f:
        json.dump(state, f, indent=2)


def get_device_calibration(device_key):
    """Returns the saved calibration dict for `device_key`, or None if
    nothing's been calibrated for it yet. `device_key` is an opaque caller-
    provided string -- audio_reactive/main.py key it off the input device's
    name (stable across device-index reordering on reconnect)."""
    if not device_key:
        return None
    with _cal_lock:
        state = _load_calibration_state()
    return state["devices"].get(device_key)


def save_device_calibration(device_key, noise_gate_floor, sample_rms=None, gain=None):
    if not device_key:
        raise ValueError("device_key is required to save a calibration")
    with _cal_lock:
        state = _load_calibration_state()
        entry = {"noise_gate_floor": round(float(noise_gate_floor), 6), "calibrated_at": time.time()}
        if sample_rms is not None:
            entry["sample_rms"] = round(float(sample_rms), 6)
        if gain is not None:
            entry["gain"] = round(float(gain), 4)
        state["devices"][device_key] = entry
        _save_calibration_state(state)
    return entry


def list_calibrations():
    with _cal_lock:
        state = _load_calibration_state()
    return state["devices"]


def delete_calibration(device_key):
    with _cal_lock:
        state = _load_calibration_state()
        existed = state["devices"].pop(device_key, None) is not None
        if existed:
            _save_calibration_state(state)
    return existed


def compute_noise_floor(samples, safety_margin=CALIBRATION_SAFETY_MARGIN, min_floor=CALIBRATION_MIN_FLOOR):
    """Given a block of samples captured during "room silence" (ambient
    noise/hum/fan, but no intentional signal), returns a recommended
    noise-gate floor: measured RMS plus a safety margin, so real quiet
    passages of actual audio don't get swallowed by the gate, floored at
    `min_floor` so a genuinely dead-silent capture (rms == 0, e.g. a muted
    virtual cable) doesn't produce a zero floor that gates nothing."""
    samples = np.asarray(samples, dtype=np.float64)
    if samples.size == 0:
        return {"noise_gate_floor": min_floor, "sample_rms": 0.0}
    if not np.all(np.isfinite(samples)):
        samples = np.nan_to_num(samples, nan=0.0, posinf=1.0, neginf=-1.0)
    rms = float(np.sqrt(np.mean(np.square(samples))))
    floor = max(min_floor, rms * safety_margin)
    return {"noise_gate_floor": round(floor, 6), "sample_rms": round(rms, 6)}


def calibrate_from_device(device_index, duration_s=3.0, sample_rate=SAMPLE_RATE, capture_fn=None):
    """Runs the real "sample a few seconds of room silence" capture and
    returns the computed floor. `capture_fn(device_index, duration_s,
    sample_rate) -> 1D samples array` defaults to a real blocking
    sounddevice capture; tests inject a synthetic one instead so this whole
    flow -- including the endpoint that calls it -- is exercised without a
    real audio device (see backend/tests/test_audio_api.py)."""
    if capture_fn is None:
        capture_fn = _default_capture_fn
    samples = capture_fn(device_index, duration_s, sample_rate)
    return compute_noise_floor(samples)


def _default_capture_fn(device_index, duration_s, sample_rate):
    import sounddevice as sd  # imported lazily: only needed for the real hardware path
    frames = max(1, int(duration_s * sample_rate))
    recording = sd.rec(frames, samplerate=sample_rate, channels=1, device=device_index, dtype="float64")
    sd.wait()
    return recording[:, 0]


class SignalConditioner:
    """Per-session signal conditioning: DC offset removal, AGC (attack/
    release), a noise gate, clip/overload detection, and a peak-hold level
    meter. One instance lives per `AudioSession`; `process()` is called once
    per captured block (BLOCK_SIZE=512 @ 44100Hz ~= 11.6ms/frame -- every
    *_ms constant above is expressed against that tick rate).

    AGC is opt-in (`agc_enabled=False` by default): it actively rescales
    signal level, which changes what an existing user's `sensitivity` slider
    feels like. Turning it on is a deliberate choice made per-session via the
    audio-reactive start API, not a silent default-behavior change. The
    noise gate and DC removal default ON since they're much lower-risk (a
    conservative floor barely above true silence, and a slow-moving DC
    estimate that's a no-op on already-clean audio).
    """

    def __init__(self, sample_rate=SAMPLE_RATE, target_rms=DEFAULT_AGC_TARGET_RMS,
                 attack_ms=DEFAULT_AGC_ATTACK_MS, release_ms=DEFAULT_AGC_RELEASE_MS,
                 noise_gate_floor=DEFAULT_NOISE_GATE_FLOOR, agc_enabled=False,
                 noise_gate_enabled=True, dc_removal_enabled=True, band_gains=None,
                 min_gain=DEFAULT_AGC_MIN_GAIN, max_gain=DEFAULT_AGC_MAX_GAIN):
        self.sample_rate = sample_rate
        self.target_rms = target_rms
        self.attack_ms = max(1.0, attack_ms)
        self.release_ms = max(1.0, release_ms)
        self.noise_gate_floor = max(0.0, noise_gate_floor)
        self.agc_enabled = agc_enabled
        self.noise_gate_enabled = noise_gate_enabled
        self.dc_removal_enabled = dc_removal_enabled
        self.min_gain = min_gain
        self.max_gain = max_gain
        self.band_gains = list(band_gains) if band_gains else None

        self._dc_estimate = 0.0
        self._gain = 1.0
        self._peak = 0.0
        self._peak_hold = 0.0
        self._peak_hold_at = time.time()
        self.last_meter = None

    def update_noise_gate_floor(self, floor):
        self.noise_gate_floor = max(0.0, floor)

    def process(self, samples, frame_dt=None, now=None):
        """Runs one captured block through DC removal -> clip/peak metering
        -> noise gate -> AGC, in that order (clip/peak are measured on the
        DC-corrected signal, before any gain WE apply, since what matters
        for "is the source overloading" is the incoming signal, not our own
        makeup gain). Returns (conditioned_samples, meter_dict).

        `frame_dt`/`now` are injectable seams for tests: real callers leave
        them None (wall clock / block-duration derived); tests pass explicit
        values so AGC attack/release and peak-hold decay are exercised
        deterministically without real sleeps.
        """
        samples = np.asarray(samples, dtype=np.float64)
        if samples.size == 0:
            samples = np.zeros(1, dtype=np.float64)
        elif not np.all(np.isfinite(samples)):
            samples = np.nan_to_num(samples, nan=0.0, posinf=1.0, neginf=-1.0)

        if frame_dt is None:
            frame_dt = len(samples) / float(self.sample_rate)
        if now is None:
            now = time.time()

        if self.dc_removal_enabled:
            block_mean = float(np.mean(samples))
            alpha_dc = 0.05  # slow-moving running-mean high-pass; fast enough to track a
                              # drifting DC bias but far too slow to touch audible content
            self._dc_estimate += alpha_dc * (block_mean - self._dc_estimate)
            samples = samples - self._dc_estimate

        input_rms = float(np.sqrt(np.mean(np.square(samples))) + 1e-12)

        peak_sample = float(np.max(np.abs(samples))) if samples.size else 0.0
        clip_fraction = float(np.mean(np.abs(samples) >= CLIP_SAMPLE_THRESHOLD)) if samples.size else 0.0
        clipping = clip_fraction > CLIP_FRACTION_WARN or peak_sample >= 0.999

        self._peak = peak_sample
        if peak_sample >= self._peak_hold:
            self._peak_hold = peak_sample
            self._peak_hold_at = now
        else:
            elapsed = max(0.0, now - self._peak_hold_at)
            decayed = self._peak_hold - PEAK_HOLD_DECAY_PER_S * elapsed
            self._peak_hold = max(peak_sample, decayed)
            if self._peak_hold <= peak_sample:
                self._peak_hold_at = now

        gated = self.noise_gate_enabled and input_rms < self.noise_gate_floor

        if self.agc_enabled:
            if not gated and input_rms > 1e-9:
                desired_gain = self.target_rms / input_rms
                desired_gain = max(self.min_gain, min(self.max_gain, desired_gain))
                tau_ms = self.attack_ms if desired_gain < self._gain else self.release_ms
                alpha = 1.0 - math.exp(-(frame_dt * 1000.0) / tau_ms)
                self._gain += alpha * (desired_gain - self._gain)
            elif gated:
                # Relax back toward unity gain slowly while gated, rather
                # than freezing -- otherwise a quiet passage that drove gain
                # up to max_gain leaves it pinned there, and the next real
                # transient above the gate blasts through amplified by a
                # gain computed for near-silence.
                alpha = 1.0 - math.exp(-(frame_dt * 1000.0) / self.release_ms)
                self._gain += alpha * (1.0 - self._gain)

        conditioned = np.zeros_like(samples) if gated else samples * (self._gain if self.agc_enabled else 1.0)
        output_rms = float(np.sqrt(np.mean(np.square(conditioned))) + 1e-12)

        meter = {
            "input_rms": round(input_rms, 6),
            "output_rms": round(output_rms, 6),
            "peak": round(self._peak, 4),
            "peak_hold": round(self._peak_hold, 4),
            "gain": round(self._gain, 4) if self.agc_enabled else 1.0,
            "clipping": bool(clipping),
            "gated": bool(gated),
            "noise_gate_floor": self.noise_gate_floor,
            "agc_enabled": self.agc_enabled,
            "noise_gate_enabled": self.noise_gate_enabled,
            "dc_removal_enabled": self.dc_removal_enabled,
            "dc_offset": round(self._dc_estimate, 6),
        }
        self.last_meter = meter
        return conditioned, meter

    def apply_band_gains(self, bands):
        """Multiplies each band's energy by an independent per-band gain
        (e.g. to compensate a mic that's naturally bass-heavy/treble-shy),
        orthogonal to the whole-signal AGC above, and recomputes fractions
        to match. No-op if band_gains isn't set or its length doesn't match
        this frame's band count (e.g. right after an n_bands change) --
        applying mismatched-length gains would silently misattribute gain to
        the wrong band, which is worse than doing nothing."""
        if not self.band_gains or len(self.band_gains) != len(bands.get("energies", [])):
            return bands
        energies = [e * g for e, g in zip(bands["energies"], self.band_gains)]
        total = sum(energies) + 1e-9
        fractions = [e / total for e in energies]
        return {**bands, "energies": energies, "fractions": fractions}
