"""Photosensitive-epilepsy safety cap for the audio-reactive engine (Week 1
Phase D, section 13). This module is intentionally standalone (no import of
`audio_reactive`) so `audio_reactive.py` can import it without a cycle.

Guidance cited: the widely-used accessibility threshold for flashing content
is **no more than 3 flashes per second**, per WCAG 2.3.1 ("Three Flashes or
Below Threshold", https://www.w3.org/WAI/WCAG21/Understanding/three-flashes-or-below-threshold.html)
and the older Ofcom/ITU guidance it descends from (ITU-R BT.1702, "Guidance
for Television Programme Makers on Material Which May Provoke Seizures in
Viewers with Photosensitive Epilepsy"). Both use "a flash" to mean a
transition large enough in luminance to be visually a flash, not any tiny
brightness wobble -- see `FLASH_BRIGHTNESS_JUMP` below for the threshold used
here.

This is deliberately enforced at the *send* layer (wrapping whatever action
`_apply_mode()` already computed) rather than inside any individual mode's
formula, so it applies uniformly no matter which mode is running or what
sensitivity/dwell the user configured -- it cannot be bypassed by mode choice
or a user-supplied config value.
"""
import json
import os
import threading
import time

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)
SAFETY_SETTINGS_PATH = os.path.join(DATA_DIR, "audio_safety.json")

# --- hard, non-bypassable ceiling -------------------------------------------
# No configuration value accepted anywhere in this codebase may push the
# *effective* flash rate above this, regardless of what is requested.
HARD_MAX_FLASH_RATE_HZ = 3.0
MIN_FLASH_RATE_HZ = 0.2  # a floor so "0" isn't silently misinterpreted

# A "flash" for rate-limiting purposes: a brightness jump at least this many
# percentage points above the previously *sent* brightness. Anchored to how
# `strobe_on_drop`/`band_flash_overlay` actually behave (they jump to ~100
# from a much lower resting brightness on a detected hit).
FLASH_BRIGHTNESS_JUMP = 30.0

# Modes whose own formulas already produce hard, high-contrast brightness
# jumps tied directly to transient detection (a hit/beat), as opposed to
# smooth/ambient hue or gradual brightness movement. Classified by reading
# `_apply_mode()` in audio_reactive.py (not modified by this module):
#   - strobe_on_drop: literal on/off strobe on a hard hit (0 -> 100 sat/val
#     swap plus a big brightness swing) -- the canonical flash-heavy mode.
#   - band_flash_overlay: ambient gradient base with a full accent flash
#     (+35 brightness spike) layered on top whenever any single band spikes.
# Every other mode moves brightness/hue smoothly frame to frame (pulse,
# gradient, breathing, blend, etc.) with no hard on/off jump built in.
FLASH_HEAVY_MODES = frozenset({"strobe_on_drop", "band_flash_overlay"})

_lock = threading.Lock()


def is_flash_heavy(mode):
    return mode in FLASH_HEAVY_MODES


def mode_metadata(all_modes):
    """Per-mode metadata for the mode-info API: which modes are flash-heavy
    (should be labeled/avoidable by a frontend) vs. ambient."""
    return [
        {
            "mode": m,
            "flash_heavy": is_flash_heavy(m),
            "category": "flash-heavy" if is_flash_heavy(m) else "ambient",
        }
        for m in all_modes
    ]


def clamp_max_flash_rate(requested_hz):
    """Clamp a *requested* max-flash-rate to the safe, non-bypassable range.
    This is the one function every configuration path must run its value
    through -- callers cannot ask for a higher effective rate than the hard
    ceiling no matter what value they pass in."""
    try:
        requested_hz = float(requested_hz)
    except (TypeError, ValueError):
        requested_hz = HARD_MAX_FLASH_RATE_HZ
    return max(MIN_FLASH_RATE_HZ, min(HARD_MAX_FLASH_RATE_HZ, requested_hz))


# --- persisted settings ------------------------------------------------------
_DEFAULT_SETTINGS = {"max_flash_rate_hz": HARD_MAX_FLASH_RATE_HZ, "disable_flash_heavy": False}


def _load_settings():
    if not os.path.exists(SAFETY_SETTINGS_PATH):
        return dict(_DEFAULT_SETTINGS)
    try:
        with open(SAFETY_SETTINGS_PATH, "r") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return dict(_DEFAULT_SETTINGS)
    data.setdefault("max_flash_rate_hz", HARD_MAX_FLASH_RATE_HZ)
    data.setdefault("disable_flash_heavy", False)
    data["max_flash_rate_hz"] = clamp_max_flash_rate(data["max_flash_rate_hz"])
    return data


def _save_settings(data):
    with open(SAFETY_SETTINGS_PATH, "w") as f:
        json.dump(data, f, indent=2)


def get_safety_settings():
    with _lock:
        return _load_settings()


def set_max_flash_rate(hz):
    """User-configurable ceiling, distinct from the UX-focused dwell slider.
    Always clamped to <= HARD_MAX_FLASH_RATE_HZ regardless of what's asked."""
    with _lock:
        settings = _load_settings()
        settings["max_flash_rate_hz"] = clamp_max_flash_rate(hz)
        _save_settings(settings)
        return settings


def set_disable_flash_heavy(disabled):
    """One-click 'disable all flash-heavy modes' toggle."""
    with _lock:
        settings = _load_settings()
        settings["disable_flash_heavy"] = bool(disabled)
        _save_settings(settings)
        return settings


def reduced_motion_profile():
    """A gentle 'reduced motion' audio-reactive preset: no strobe-style
    modes, a low, non-bypassable-adjacent flash rate ceiling, and a capped
    overall sensitivity so brightness swings stay gentle. Returned as a dict
    of session-start kwargs a caller can merge into a start request."""
    return {
        "mode": "breathing_silence",
        "sensitivity": 0.6,
        "n_bands": 3,
        "disable_flash_heavy": True,
        "max_flash_rate_hz": 1.0,
        "max_brightness_swing": 40.0,
    }


# --- the actual per-frame enforcement ----------------------------------------
def _brightness_of(action):
    return action[3] if action[0] == "hsv" else action[4]


def _with_brightness(action, brightness):
    if action[0] == "hsv":
        return (action[0], action[1], action[2], brightness)
    return (action[0], action[1], action[2], action[3], brightness)


def apply_flash_cap(action, ctx, max_rate_hz=None, max_brightness_swing=None):
    """Clamp `action` so this bulb never flashes faster than `max_rate_hz`
    (itself clamped to <= HARD_MAX_FLASH_RATE_HZ). If a frame's brightness
    jump looks like a flash (>= FLASH_BRIGHTNESS_JUMP over the last *sent*
    brightness) and the minimum inter-flash interval hasn't elapsed yet, the
    jump is suppressed by holding the previous brightness instead -- the hue
    / saturation the mode computed still comes through, only the flash
    itself is capped.

    `ctx` is the same mutable per-session dict `_apply_mode` uses; this
    function stashes its own state under keys prefixed `_safety_` so it
    can't collide with any mode's own ctx keys.

    Also supports an optional `max_brightness_swing`: an absolute cap on
    how far brightness may move in a single frame (used by the reduced-
    motion profile to keep swings gentle even outside the flash-rate path).
    """
    now = time.time()
    rate_hz = clamp_max_flash_rate(max_rate_hz if max_rate_hz is not None else HARD_MAX_FLASH_RATE_HZ)
    min_interval_s = 1.0 / rate_hz

    brightness = _brightness_of(action)
    prev_brightness = ctx.get("_safety_prev_brightness", brightness)

    if max_brightness_swing is not None:
        max_swing = abs(max_brightness_swing)
        delta = brightness - prev_brightness
        if delta > max_swing:
            brightness = prev_brightness + max_swing
        elif delta < -max_swing:
            brightness = prev_brightness - max_swing

    is_flash = (brightness - prev_brightness) >= FLASH_BRIGHTNESS_JUMP
    last_flash_at = ctx.get("_safety_last_flash_at", 0.0)
    if is_flash:
        if (now - last_flash_at) < min_interval_s:
            # Too soon since the last real flash -- suppress this one.
            brightness = prev_brightness
        else:
            ctx["_safety_last_flash_at"] = now

    if brightness != _brightness_of(action):
        action = _with_brightness(action, brightness)
    ctx["_safety_prev_brightness"] = brightness
    return action
