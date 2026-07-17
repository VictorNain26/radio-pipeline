"""
Tests for the 2026-07 rotation redesign: nightly taste curation quota,
measured play rate, and the GOLD catalogue tier.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from classify import (
    _rank_by_taste,
    compute_rotation_tier,
    measure_expected_plays_per_day,
    qualifies_for_gold,
    tier_filter_dayparts,
)
from config import MoodCategory, ROTATION, ROTATION_CATEGORIES, get_dayparts_for_mood


# ---------------------------------------------------------------------------
# measure_expected_plays_per_day
# ---------------------------------------------------------------------------

def _entry(play_count, age_days):
    return {"play_count": play_count, "age_days": age_days}


def test_measured_rate_from_real_data():
    # 100 plays over 100 track-days → 1.0/day
    entries = [_entry(50, 50.0), _entry(50, 50.0)]
    assert measure_expected_plays_per_day(entries) == pytest.approx(1.0)


def test_measured_rate_falls_back_on_thin_signal():
    entries = [_entry(5, 10.0)]  # only 10 track-days < 30
    assert measure_expected_plays_per_day(entries) == \
        ROTATION_CATEGORIES.expected_plays_per_day


def test_measured_rate_ignores_day_old_tracks_and_clamps():
    # Fresh uploads (age<1) excluded; extreme rates clamped to [0.2, 3.0]
    entries = [_entry(999, 0.5), _entry(400, 40.0)]  # 400/40 = 10 → clamp 3.0
    assert measure_expected_plays_per_day(entries) == 3.0
    entries = [_entry(0, 100.0)]
    assert measure_expected_plays_per_day(entries) == 0.2


# ---------------------------------------------------------------------------
# compute_rotation_tier with measured rate
# ---------------------------------------------------------------------------

def test_tier_uses_measured_expected_rate():
    age = ROTATION_CATEGORIES.grace_period_days + 10
    plays = int(age * 1.0)  # rate 1.0/day
    # vs measured 0.5 → 2× the average → HEAVY
    assert compute_rotation_tier(plays, age, expected=0.5) == "HEAVY"
    # vs measured 2.0 → half the average → LIGHT
    assert compute_rotation_tier(plays, age, expected=2.0) == "LIGHT"


def test_tier_default_expected_unchanged():
    # Backward compat: no expected → config constant
    age = ROTATION_CATEGORIES.grace_period_days + 10
    assert compute_rotation_tier(0, age) == "LIGHT"
    assert compute_rotation_tier(0, 1.0) == "HEAVY"  # grace period


# ---------------------------------------------------------------------------
# GOLD
# ---------------------------------------------------------------------------

def test_gold_requires_proven_and_on_color():
    age = 60.0
    expected = 0.65
    proven_plays = int(age * expected * ROTATION_CATEGORIES.heavy_above_average_ratio) + 1
    good_taste = ROTATION.gold_min_taste + 0.05
    assert qualifies_for_gold(proven_plays, age, good_taste, expected) is True
    # Proven but off-color → no
    assert qualifies_for_gold(proven_plays, age, ROTATION.gold_min_taste - 0.05,
                              expected) is False
    # On-color but under-played → no
    assert qualifies_for_gold(3, age, good_taste, expected) is False
    # No taste score at all → no
    assert qualifies_for_gold(proven_plays, age, None, expected) is False


def test_gold_dayparts_shrink_like_light():
    moods_with_zones = [m for m in MoodCategory if len(get_dayparts_for_mood(m)) > 1]
    assert moods_with_zones, "need at least one mood with several dayparts"
    mood = moods_with_zones[0]
    dayparts = get_dayparts_for_mood(mood)
    assert tier_filter_dayparts(dayparts, "GOLD") == \
        tier_filter_dayparts(dayparts, "LIGHT")
    assert len(tier_filter_dayparts(dayparts, "GOLD")) == \
        ROTATION_CATEGORIES.light_daypart_count


# ---------------------------------------------------------------------------
# Nightly curation ranking
# ---------------------------------------------------------------------------

def test_rank_by_taste_best_first_unknown_last():
    files = [Path("/x/a.mp3"), Path("/x/b.mp3"), Path("/x/c.mp3")]
    keys = {files[0]: "a - a", files[1]: "b - b", files[2]: None}
    scores = {"a - a": 0.70, "b - b": 0.85}

    with patch("classify._track_key_of_file", side_effect=lambda f: keys[f]), \
         patch("classify.check_taste", side_effect=lambda k: scores[k]):
        ranked = _rank_by_taste(files)

    assert [r[0].name for r in ranked] == ["b.mp3", "a.mp3", "c.mp3"]
    assert ranked[0][2] == pytest.approx(0.85)
    assert ranked[2][2] == -1.0  # no tags → ranked last, still processed


def test_rank_by_taste_handles_skip_verdict():
    with patch("classify._track_key_of_file", return_value="x - y"), \
         patch("classify.check_taste", return_value=None):
        ranked = _rank_by_taste([Path("/x/a.mp3")])
    assert ranked[0][2] == -1.0


def test_quota_config_sane():
    # 6/night ≈ 42/week: each add can still get 15-20 weekly heavy plays.
    assert 1 <= ROTATION.max_uploads_per_night <= 10
    assert 0.0 < ROTATION.gold_max_pct <= 50.0


# ---------------------------------------------------------------------------
# Carryover (gem safety net)
# ---------------------------------------------------------------------------

def test_carryover_keeps_on_color_leftovers(tmp_path):
    from classify import _should_carry_over
    from config import TASTE_FILTER
    f = tmp_path / "gem.mp3"
    f.write_bytes(b"x")  # fresh mtime
    good = TASTE_FILTER.threshold + 0.1
    bad = TASTE_FILTER.threshold - 0.1
    assert _should_carry_over(f, good, already_carried=0) is True
    # Off-color leftovers go to cooldown instead
    assert _should_carry_over(f, bad, already_carried=0) is False
    # Pool full → no more carryover
    assert _should_carry_over(f, good, already_carried=ROTATION.carryover_max_files) is False
    # Missing file → no carryover
    assert _should_carry_over(tmp_path / "gone.mp3", good, already_carried=0) is False


def test_carryover_ages_out(tmp_path):
    import os
    import time as _time
    from classify import _should_carry_over
    from config import TASTE_FILTER
    f = tmp_path / "old.mp3"
    f.write_bytes(b"x")
    old = _time.time() - (ROTATION.carryover_max_days + 1) * 86400
    os.utime(f, (old, old))
    assert _should_carry_over(f, TASTE_FILTER.threshold + 0.1, already_carried=0) is False
