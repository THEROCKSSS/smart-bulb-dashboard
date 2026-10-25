"""One settings contract for the lighting mixer, presets, and session resume."""
import math
from audio_safety import is_flash_heavy
from audio_hop import validate_hop_window, DEFAULT_HOP_SIZE, DEFAULT_WINDOW_SIZE

MODES = [
    "band_fixed", "dominant_band", "weighted_blend", "vu_meter",
    "auto_rotate_hue", "monochrome_pulse", "strobe_on_drop", "palette_cycle",
    "spectrum_gradient", "band_flash_overlay", "stereo_split", "breathing_silence",
    "harmonic_pairs", "kick_snare_split", "energy_contour", "bass_only_pulse",
    "mirror_mode", "random_walk_hue", "silence_flash_recover", "crescendo_ramp",
]

DEFAULTS = {
    "device_index": None, "source": "device", "source_device_name": None,
    "source_hostapi": None, "mode": "band_fixed", "sensitivity": 1.0,
    "monochrome_hue": 280.0, "n_bands": 3, "min_dwell_ms": 90,
    "beat_sensitivity": "normal", "agc_enabled": False,
    "noise_gate_enabled": True, "dc_removal_enabled": True,
    "noise_gate_floor": 0.0015, "agc_target_rms": 0.15,
    "agc_attack_ms": 50.0, "agc_release_ms": 400.0,
    "band_gains": [1.0, 1.0, 1.0], "use_saved_calibration": True,
    "smoothing_ms": 0.0, "brightness_min": 0.0, "brightness_max": 100.0,
    "max_duration_s": None, "warmup_s": 0.0, "auto_resume_grace_s": 8.0,
    "max_flash_rate_hz": None, "disable_flash_heavy": False,
    "max_brightness_swing": None, "silence_auto_off": True,
    "fallback_device_index": None, "hop_size": None, "window_size": None,
}

RANGES = {
    "sensitivity": (0.1, 5), "n_bands": (3, 16), "min_dwell_ms": (40, 5000),
    "noise_gate_floor": (0, 1), "agc_target_rms": (0.001, 1),
    "agc_attack_ms": (1, 5000), "agc_release_ms": (1, 10000),
    "smoothing_ms": (0, 2000), "brightness_min": (0, 100),
    "brightness_max": (0, 100), "max_duration_s": (0.1, 86400),
    "warmup_s": (0, 120), "auto_resume_grace_s": (0, 3600),
    "max_flash_rate_hz": (0.1, 3), "max_brightness_swing": (0, 100),
    "hop_size": (64, 4096), "window_size": (256, 16384),
}

LIVE_FIELDS = (
    "mode", "sensitivity", "beat_sensitivity", "min_dwell_ms", "monochrome_hue",
    "n_bands", "agc_enabled", "noise_gate_enabled", "dc_removal_enabled",
    "noise_gate_floor", "agc_target_rms", "agc_attack_ms", "agc_release_ms",
    "band_gains", "smoothing_ms", "brightness_min", "brightness_max",
    "max_flash_rate_hz", "disable_flash_heavy", "max_brightness_swing",
)
SOURCE_FIELDS = ("source", "device_index", "source_device_name", "source_hostapi")
RESTART_FIELDS = SOURCE_FIELDS + tuple(key for key in DEFAULTS if key not in LIVE_FIELDS and key not in SOURCE_FIELDS)


def defaults():
    return {**DEFAULTS, "band_gains": list(DEFAULTS["band_gains"])}


def normalize(config):
    """Validate a complete or partial stored record; never silently discard edits."""
    unknown = set(config) - DEFAULTS.keys()
    if unknown:
        raise ValueError("unknown audio settings: " + ", ".join(sorted(unknown)))
    result = defaults()
    result.update(config)
    # Older records used null to request a shipped/calibrated numeric default.
    for key, default in DEFAULTS.items():
        if result[key] is None and default is not None:
            result[key] = list(default) if isinstance(default, list) else default
    if result["mode"] not in MODES:
        raise ValueError("unknown mode")
    if result["source"] not in ("device", "bridge"):
        raise ValueError("source must be device or bridge")
    if result["beat_sensitivity"] not in ("subtle", "normal", "aggressive"):
        raise ValueError("unknown beat_sensitivity")
    for key, (low, high) in RANGES.items():
        value = result[key]
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(key + " must be a finite number")
        if not low <= value <= high:
            raise ValueError(f"{key} must be between {low} and {high}")
    for key in ("n_bands", "hop_size", "window_size", "device_index", "fallback_device_index"):
        value = result[key]
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise ValueError(key + " must be a non-negative integer")
    for key, default in DEFAULTS.items():
        if isinstance(default, bool) and not isinstance(result[key], bool):
            raise ValueError(key + " must be a boolean")
    for key in ("source_device_name", "source_hostapi"):
        value = result[key]
        if value is not None and (not isinstance(value, str) or len(value) > 256):
            raise ValueError(key + " must be at most 256 characters")
    hue = result["monochrome_hue"]
    if isinstance(hue, bool) or not isinstance(hue, (int, float)) or not math.isfinite(hue):
        raise ValueError("monochrome_hue must be a finite number")
    result["monochrome_hue"] = hue % 360
    gains = result["band_gains"]
    if not isinstance(gains, list) or not 3 <= len(gains) <= 16:
        raise ValueError("band_gains must contain 3 to 16 gains")
    if any(isinstance(g, bool) or not isinstance(g, (int, float)) or not math.isfinite(g) or not 0 <= g <= 4 for g in gains):
        raise ValueError("band_gains must be finite numbers between 0 and 4")
    result["band_gains"] = list(gains)
    if result["brightness_min"] > result["brightness_max"]:
        raise ValueError("brightness_min cannot exceed brightness_max")
    validate_hop_window(result['hop_size'] or DEFAULT_HOP_SIZE, result['window_size'] or DEFAULT_WINDOW_SIZE)
    if result["disable_flash_heavy"] and is_flash_heavy(result["mode"]):
        result["mode"] = "weighted_blend"
    return result


def start_kwargs(config):
    """Translate the public source name once, for every resume/apply caller."""
    result = normalize(config)
    result["source_kind"] = result.pop("source")
    return result
