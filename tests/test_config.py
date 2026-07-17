"""Tests for config.py helpers: should_reject_track, day-part lookup, validate_config."""
from __future__ import annotations

import pytest

from config import (  # type: ignore[import-not-found]
    AUDIO_FILTERS,
    DAYPARTS,
    DaypartSegment,
    MoodCategory,
    get_daypart_for_hour,
    get_dayparts_for_mood,
    get_day_type,
    is_smooth_energy_transition,
    should_reject_track,
    validate_config,
    EnergyLevel,
)


def test_validate_config_passes_on_default_setup():
    is_valid, errors = validate_config()
    assert is_valid, f"validate_config() returned errors: {errors}"


def test_dayparts_cover_24h():
    covered = set()
    for profile in DAYPARTS.values():
        if not profile.enabled:
            continue
        if profile.start_hour > profile.end_hour:  # wraps midnight
            covered.update(range(profile.start_hour, 24))
            covered.update(range(0, profile.end_hour))
        else:
            covered.update(range(profile.start_hour, profile.end_hour))
    assert covered == set(range(24)), "Day-parts must cover every hour"


def test_get_daypart_for_hour_handles_midnight_wrap():
    """Night daypart spans 22 → 5. Both ends should map to NIGHT."""
    assert get_daypart_for_hour(22) == DaypartSegment.NIGHT
    assert get_daypart_for_hour(2) == DaypartSegment.NIGHT
    assert get_daypart_for_hour(4) == DaypartSegment.NIGHT
    # 5 belongs to DAWN (boundary handled exclusive end of NIGHT)
    assert get_daypart_for_hour(5) == DaypartSegment.DAWN
    # All 4 zones reachable
    assert get_daypart_for_hour(9) == DaypartSegment.DAY
    assert get_daypart_for_hour(17) == DaypartSegment.DUSK


def test_dayparts_for_mood_returns_at_least_one():
    """Every enabled mood must be routable to a daypart."""
    for mood in MoodCategory:
        dps = get_dayparts_for_mood(mood)
        assert dps, f"Mood {mood.value} has no daypart routing"


def test_should_reject_track_filters_by_duration():
    too_short = {"mood": "Energetic", "duration": 30, "bpm": 120, "confidence": 0.8}
    rejected, reason = should_reject_track(too_short)
    assert rejected and "court" in (reason or "")

    too_long = {"mood": "Energetic", "duration": 9999, "bpm": 120, "confidence": 0.8}
    rejected, reason = should_reject_track(too_long)
    assert rejected and "long" in (reason or "")


def test_should_reject_track_accepts_valid_track():
    ok = {"mood": "Energetic", "duration": 180, "bpm": 120, "confidence": 0.8}
    rejected, _ = should_reject_track(ok)
    assert rejected is False


def test_is_smooth_energy_transition():
    assert is_smooth_energy_transition(EnergyLevel.LOW, EnergyLevel.MEDIUM)
    assert is_smooth_energy_transition(EnergyLevel.MEDIUM, EnergyLevel.MEDIUM)
    assert not is_smooth_energy_transition(EnergyLevel.VERY_LOW, EnergyLevel.HIGH)


@pytest.mark.parametrize("weekday,expected_str", [
    (0, "weekday"), (3, "weekday"),
    (4, "friday"),
    (5, "saturday"),
    (6, "sunday"),
])
def test_get_day_type(weekday, expected_str):
    assert get_day_type(weekday).value == expected_str
