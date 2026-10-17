"""Lighting-response limits and smoothing have predictable time-based behavior."""
import math
import pytest
import audio_signal


def test_brightness_limits_apply_even_when_smoothing_is_disabled():
    envelope = audio_signal.LightingEnvelope()
    assert envelope.process(("hsv", 20, 100, 95), 0.01, maximum=60)[3] == 60
    assert envelope.process(("hsv", 20, 100, 1), 0.01, minimum=5)[3] == 5


def test_smoothing_uses_elapsed_time_and_takes_the_short_hue_path():
    envelope = audio_signal.LightingEnvelope()
    envelope.process(("hsv", 350, 100, 20), 0.01, smoothing_ms=100)
    result = envelope.process(("hsv", 10, 100, 80), math.log(2) * 0.1, smoothing_ms=100)
    assert min(result[1], 360-result[1]) < 0.01
    assert result[3] == pytest.approx(50)


def test_new_brightness_ceiling_takes_effect_immediately():
    envelope = audio_signal.LightingEnvelope()
    envelope.process(("hsv", 0, 100, 100), 0.01, smoothing_ms=500)
    result = envelope.process(("hsv", 0, 100, 100), 0.01, smoothing_ms=500, maximum=30)
    assert result[3] <= 30
