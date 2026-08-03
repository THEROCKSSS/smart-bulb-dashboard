"""Every genre preset must be startable.

A preset is a saved set of arguments to start_session(), so a typo in one is
not a cosmetic problem -- it is an option in the UI that 400s when clicked.
These checks are cheap and catch the whole class.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import audio_reactive as ar  # noqa: E402

REQUIRED = {"id", "name", "mode", "sensitivity", "min_dwell_ms", "n_bands",
            "monochrome_hue", "beat_sensitivity", "palette", "description"}


def test_preset_ids_are_unique():
    ids = [p["id"] for p in ar.AUDIO_GENRE_PRESETS]
    assert len(ids) == len(set(ids))


def _preset_id(preset):
    return preset.get("id", "<no id>")


def test_every_preset_is_valid_and_startable(check_all):
    """One test over all 24 presets rather than 24 parametrised ones.

    A change that breaks every preset at once -- renaming a MODES entry, say
    -- should read as one failure listing 24 casualties, not as 24 separate
    red lines. See the `check_all` fixture in conftest.py.
    """
    check_all(ar.AUDIO_GENRE_PRESETS, _assert_preset_is_valid_and_startable,
              label="preset", name=_preset_id)


def _assert_preset_is_valid_and_startable(preset):
    assert REQUIRED <= set(preset), f"missing {REQUIRED - set(preset)}"
    assert preset["mode"] in ar.MODES
    assert preset["beat_sensitivity"] in ar.BEAT_SENSITIVITY_PRESETS
    assert preset["n_bands"] >= 1
    assert 0 <= preset["monochrome_hue"] < 360
    assert preset["sensitivity"] > 0
    assert preset["palette"], "an empty palette renders as a blank swatch"
    # Every palette entry must name a real colour. Invented ids render as
    # nothing -- which is exactly what happened when this set was written
    # ("warm_white" instead of "white_warm", among others).
    known_colors = {c["id"] for c in ar.PRESET_COLORS}
    unknown = [c for c in preset["palette"] if c not in known_colors]
    assert not unknown, f"{preset['id']} references unknown colours: {unknown}"
    assert preset["description"].strip()

    # The dwell floor exists to stop a preset hammering the bulb faster than
    # it can physically respond; a preset shipping below it would bypass the
    # very guard the API enforces on hand-entered values.
    assert ar.MIN_DWELL_FLOOR_MS <= preset["min_dwell_ms"] <= ar.MIN_DWELL_MS_CEILING

    # The same validation the start route runs, so a preset can never be
    # something the API would refuse. (sensitivity isn't part of this check --
    # it's a gain multiplier, not a bounded config field.)
    # Raises AudioConfigError on anything the route would 400 on, so simply
    # not raising is the assertion.
    try:
        ar.validate_start_config(
            n_bands=preset["n_bands"],
            min_dwell_ms=preset["min_dwell_ms"],
            mode=preset["mode"],
        )
    except ar.AudioConfigError as e:
        pytest.fail(f"{preset['id']} would be rejected by the API: {e}")


def test_every_preset_is_findable_by_id(check_all):
    def _findable(preset):
        assert ar.find_genre_preset(preset["id"]) is preset

    check_all(ar.AUDIO_GENRE_PRESETS, _findable,
              label="preset", name=_preset_id)


def test_bpm_suggestions_point_at_presets_that_exist():
    """A suggestion naming a deleted or renamed preset would surface a
    recommendation the user cannot act on."""
    known = {p["id"] for p in ar.AUDIO_GENRE_PRESETS}
    for lo, hi, preset_id in ar.BPM_PRESET_SUGGESTIONS:
        assert preset_id in known, f"BPM {lo}-{hi} suggests unknown preset '{preset_id}'"


def test_bpm_suggestion_ranges_are_contiguous_and_ordered():
    """Gaps would leave a detected tempo with no suggestion at all."""
    ranges = ar.BPM_PRESET_SUGGESTIONS
    for (lo, hi, _), (next_lo, _, _) in zip(ranges, ranges[1:]):
        assert lo < hi
        assert hi == next_lo, f"gap or overlap between {hi} and {next_lo}"


def test_the_documented_use_cases_are_all_represented():
    """The docs promise a preset for parties, for general listening, and for
    slow material. If a category empties out, the docs are lying."""
    tags = {w.strip() for p in ar.AUDIO_GENRE_PRESETS if p.get("best_for")
            for w in p["best_for"].split(",")}
    assert {"party", "general", "slow"} <= tags
