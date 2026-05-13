"""Tests for analyze.classify_mood — Russell circumplex sectoring (atan2)."""
from __future__ import annotations

import math

import pytest

# analyze.py only imports math + numpy + config at top level; safe under pytest
from analyze import classify_mood, get_energy_level  # type: ignore[import-not-found]
from config import EnergyLevel, MoodCategory  # type: ignore[import-not-found]


# 8 sectors of 45° each, centered on the canonical (valence, arousal) anchors.
# We pick angles right in the middle of each sector so they're robust to
# boundary rounding.
@pytest.mark.parametrize(
    "valence,arousal,expected",
    [
        # 0° (right): high valence, zero arousal → Energetic
        (1.0, 0.0, MoodCategory.ENERGETIC),
        # 45° (upper-right): + + → Excited
        (math.cos(math.radians(45)), math.sin(math.radians(45)), MoodCategory.EXCITED),
        # 90° (top): zero valence, high arousal → Intense
        (0.0, 1.0, MoodCategory.INTENSE),
        # 135° (upper-left): - + → Angry
        (math.cos(math.radians(135)), math.sin(math.radians(135)), MoodCategory.ANGRY),
        # 180° (left): low valence, zero arousal → Melancholic
        (-1.0, 0.0, MoodCategory.MELANCHOLIC),
        # 225° (lower-left): - - → Sad
        (math.cos(math.radians(225)), math.sin(math.radians(225)), MoodCategory.SAD),
        # 270° (bottom): zero valence, low arousal → Calm
        (0.0, -1.0, MoodCategory.CALM),
        # 315° (lower-right): + - → Relaxed
        (math.cos(math.radians(315)), math.sin(math.radians(315)), MoodCategory.RELAXED),
    ],
)
def test_classify_mood_covers_each_octant(valence, arousal, expected):
    mood, confidence = classify_mood(valence, arousal)
    assert mood == expected
    # All octant centers sit at distance 1.0, so confidence is high.
    assert 0.85 <= confidence <= 0.98


def test_classify_mood_origin_has_low_confidence():
    """At (0,0) we're at the circumplex center: max ambiguity."""
    _mood, confidence = classify_mood(0.0, 0.0)
    # base 0.65 + 0 * 0.30 = 0.65
    assert confidence == pytest.approx(0.65, rel=1e-3)


def test_classify_mood_confidence_caps_at_098():
    """Even with extreme values the boost can't push past the cap."""
    _mood, confidence = classify_mood(1.0, 1.0)
    assert confidence <= 0.98


def test_get_energy_level_thresholds():
    assert get_energy_level(-0.9) == EnergyLevel.VERY_LOW
    assert get_energy_level(-0.5) == EnergyLevel.LOW
    assert get_energy_level(0.0) == EnergyLevel.MEDIUM
    assert get_energy_level(0.5) == EnergyLevel.HIGH
    assert get_energy_level(0.9) == EnergyLevel.VERY_HIGH


def test_classify_mood_sector_boundaries():
    """A point straight above center (90°) sits exactly at Intense's middle.
    Slight rotations should stay on the right side of the 67.5° boundary."""
    # Just inside Intense (89°)
    mood, _ = classify_mood(math.cos(math.radians(89)), math.sin(math.radians(89)))
    assert mood == MoodCategory.INTENSE
    # Just outside Intense, into Excited (60°)
    mood, _ = classify_mood(math.cos(math.radians(60)), math.sin(math.radians(60)))
    assert mood == MoodCategory.EXCITED
