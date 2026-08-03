"""Hop/window decoupling for the analysis pipeline (issue #80).

The pipeline used to analyse in 512-sample blocks at 44100Hz, which made one
number do two incompatible jobs:

* **latency** -- a block cannot be handed over until its last sample exists,
  so 512 samples is 11.6ms of delay before analysis even starts. Already over
  the 10ms budget before any bridge hop is added.
* **frequency resolution** -- 512 samples resolves to ~86Hz per bin. Bass
  lives below 250Hz, so the entire bass range is about three bins wide.

Shrinking the block fixes the first and ruins the second, and the second is
what makes beat detection work: measured on a dense mix, bass-band onset
tracking gets 9/9 tempos right where broadband RMS gets 1/9.

The resolution is to stop conflating them:

* the **hop** is how often analysis runs, and it sets latency
* the **window** is how many samples each analysis sees, and it sets
  frequency resolution

`HopBuffer` keeps a rolling window and emits one full window every hop, so a
short hop and a long window coexist. At the default 256/2048 that is 5.8ms of
latency with 21.5Hz resolution -- better on *both* axes than the 512/512 it
replaces (11.6ms, 86Hz).

Note `FFT_SIZE = 4096` in audio_reactive already zero-pads. Zero-padding
interpolates bins; it does not add true resolution. True resolution comes from
window *length*, which is why the window has to grow rather than the padding.

Deliberately no knowledge of analysis: `analyze_frame()` is not modified and
not imported. It receives a longer array and is otherwise untouched, which is
what keeps the 21 golden-value tests valid as the guard proving this changed
timing and nothing else.
"""
import numpy as np

# 256 samples @ 44100Hz = 5.8ms. With analysis measured at ~0.6ms that lands
# sound-to-decision near 6.4ms, inside the 10ms requirement with margin for a
# bridge hop.
DEFAULT_HOP_SIZE = 256

# 1024 samples @ 44100Hz = 23.2ms of context, 43Hz per bin -- twice the bass
# resolution of the 512-sample window it replaces.
#
# 2048 was tried first and is NOT better despite resolving bass twice as
# finely again. Measured on the dense-mix tempo fixture, tracking the same six
# tempos through the real hop scheduler:
#
#     window 1024 -> 6/6 correct
#     window 2048 -> 5/6   (174 BPM read as 178.2)
#     window 4096 -> 3/6   (and every estimate biased high)
#
# A longer window smears the transient it is trying to locate: the kick enters
# gradually over more hops, the onset peak flattens, and the autocorrelation
# lag lands a frame short. Frequency resolution and time resolution trade
# against each other, and beat detection needs the time side.
#
# Verified independent of the beat refractory -- disabling it gives identical
# BPM figures, so the window really is the variable.
DEFAULT_WINDOW_SIZE = 1024

# Guard rails. A hop below this buys latency that the bulb's own 50-150ms
# round trip renders meaningless while multiplying CPU; a window above this
# starts smearing transients enough to hurt onset detection.
MIN_HOP_SIZE = 64
MAX_WINDOW_SIZE = 8192


class HopConfigError(ValueError):
    """Raised for a hop/window combination that cannot work."""


def validate_hop_window(hop, window):
    """Both must be sane and the window must be a whole number of hops.

    The whole-multiple rule is not arbitrary: it keeps every emitted window
    aligned to a hop boundary, so consecutive windows overlap by exactly
    `window - hop` samples and the overlap never drifts.
    """
    hop = int(hop)
    window = int(window)
    if hop < MIN_HOP_SIZE:
        raise HopConfigError(f"hop {hop} is below the {MIN_HOP_SIZE}-sample floor")
    if window > MAX_WINDOW_SIZE:
        raise HopConfigError(f"window {window} is above the {MAX_WINDOW_SIZE}-sample ceiling")
    if window < hop:
        raise HopConfigError(f"window {window} cannot be shorter than hop {hop}")
    if window % hop != 0:
        raise HopConfigError(
            f"window {window} must be a whole number of hops (hop {hop}); "
            f"otherwise the overlap drifts frame to frame")
    return hop, window


class HopBuffer:
    """Accumulates arriving audio and emits one full window per hop.

    Decouples the size blocks *arrive* in from the size analysis *runs* on,
    which matters beyond latency: the bridge delivers whatever frame size the
    host captured, and a device delivers whatever PortAudio chose. Neither has
    to match the analysis window any more.

    Emits nothing until the window has filled once. A part-filled window is
    half real audio and half zeros, and that discontinuity splatters across
    the spectrum -- it would read as a transient at session start and fire a
    false beat before any audio had really begun.
    """

    def __init__(self, window=DEFAULT_WINDOW_SIZE, hop=DEFAULT_HOP_SIZE):
        self.hop, self.window = validate_hop_window(hop, window)
        self._buf = np.zeros(self.window, dtype=np.float32)
        self._pending = 0      # samples accumulated since the last emit
        self._filled = 0       # real samples ever written, capped at window
        self.emitted = 0
        self.dropped_partial = 0

    @property
    def ready(self):
        return self._filled >= self.window

    def reset(self):
        self._buf[:] = 0.0
        self._pending = 0
        self._filled = 0

    def push(self, samples):
        """Feed arriving samples; return a list of windows to analyse.

        Usually zero or one window. More than one only when a block arrives
        carrying several hops' worth at once -- which is exactly what a bursty
        capture backend does, and is why this returns a list rather than an
        Optional.
        """
        arr = np.asarray(samples, dtype=np.float32).reshape(-1)
        out = []
        i = 0
        n = arr.size
        while i < n:
            take = min(self.hop - self._pending, n - i)
            if take <= 0:
                break
            chunk = arr[i:i + take]
            # Roll the window left by `take` and append. O(window) per chunk,
            # ~2048 floats -- microseconds against the ~600us analysis it feeds.
            self._buf = np.concatenate((self._buf[take:], chunk))
            self._pending += take
            self._filled = min(self.window, self._filled + take)
            i += take
            if self._pending >= self.hop:
                self._pending = 0
                if self.ready:
                    out.append(self._buf.copy())
                    self.emitted += 1
                else:
                    self.dropped_partial += 1
        return out

    def status(self):
        return {
            "hop": self.hop,
            "window": self.window,
            "overlap": self.window - self.hop,
            "ready": self.ready,
            "emitted": self.emitted,
            "warmup_frames_skipped": self.dropped_partial,
        }
