"""
Tests for the rotation tier system in scripts/classify.py.

Verifies the actual semantics of the BBC A/B/C-style rotation:
  HEAVY  : grace period (new) OR proven (plays/day above expected)
  MEDIUM : post-grace average performance
  LIGHT  : below-average performance, fading out
"""
from __future__ import annotations

import pytest

from classify import compute_rotation_tier, tier_filter_dayparts, TIER_RANK  # type: ignore[import-not-found]
from config import (  # type: ignore[import-not-found]
    DaypartSegment, ROTATION_CATEGORIES,
)


# --- compute_rotation_tier ---


def test_new_track_is_heavy_during_grace_period():
    """The whole point of a discovery webradio: new tracks get full exposure."""
    assert compute_rotation_tier(play_count=0, age_days=0) == "HEAVY"
    assert compute_rotation_tier(play_count=0, age_days=1) == "HEAVY"
    assert compute_rotation_tier(play_count=0, age_days=13.9) == "HEAVY"


def test_grace_period_ends_at_grace_period_days():
    g = ROTATION_CATEGORIES.grace_period_days
    # Just inside grace → HEAVY regardless of plays
    assert compute_rotation_tier(play_count=0, age_days=g - 0.01) == "HEAVY"
    # Just past grace, no plays accumulated → LIGHT (below-average performer)
    assert compute_rotation_tier(play_count=0, age_days=g + 0.01) == "LIGHT"


def test_post_grace_proven_hit_is_heavy():
    """A track playing well above library average stays HEAVY."""
    expected = ROTATION_CATEGORIES.expected_plays_per_day
    ratio = ROTATION_CATEGORIES.heavy_above_average_ratio
    age = 30
    # Plays well above the heavy threshold
    plays = int(age * expected * ratio * 1.5)
    assert compute_rotation_tier(play_count=plays, age_days=age) == "HEAVY"


def test_post_grace_average_is_medium():
    """Tracks performing around the library mean sit in MEDIUM."""
    expected = ROTATION_CATEGORIES.expected_plays_per_day
    light = ROTATION_CATEGORIES.light_below_average_ratio
    heavy = ROTATION_CATEGORIES.heavy_above_average_ratio
    age = 25
    # Plays right in the middle of the band
    target_rate = expected * (light + heavy) / 2
    plays = int(age * target_rate)
    assert compute_rotation_tier(play_count=plays, age_days=age) == "MEDIUM"


def test_post_grace_underperformer_is_light():
    """Tracks well below average fade out."""
    expected = ROTATION_CATEGORIES.expected_plays_per_day
    ratio = ROTATION_CATEGORIES.light_below_average_ratio
    age = 40
    # Plays well below the LIGHT floor
    plays = int(age * expected * ratio * 0.4)
    assert compute_rotation_tier(play_count=plays, age_days=age) == "LIGHT"


def test_zero_plays_post_grace_is_light():
    """A track that has been around for a long time but no one plays it → LIGHT."""
    assert compute_rotation_tier(play_count=0, age_days=50) == "LIGHT"


@pytest.mark.parametrize("plays,age,expected_tier", [
    # (plays, age_days, expected_tier)
    (0,    0,    "HEAVY"),     # just uploaded
    (5,    7,    "HEAVY"),     # 1 week in, grace
    (15,   14,   "HEAVY"),     # just past grace, plays/day = 1.07 > 0.78
    (5,    14.01, "LIGHT"),    # just past grace, plays/day = 0.36 < 0.39
    (12,   20,   "MEDIUM"),    # plays/day = 0.6, in band
    (30,   20,   "HEAVY"),     # plays/day = 1.5, well above
    (3,    30,   "LIGHT"),     # plays/day = 0.1, well below
])
def test_tier_table(plays, age, expected_tier):
    assert compute_rotation_tier(plays, age) == expected_tier


# --- tier_filter_dayparts ---


def test_heavy_keeps_all_dayparts():
    all_dps = [
        DaypartSegment.DAWN,
        DaypartSegment.DAY,
        DaypartSegment.DUSK,
        DaypartSegment.NIGHT,
    ]
    assert tier_filter_dayparts(all_dps, "HEAVY") == all_dps


def test_medium_keeps_first_n():
    all_dps = [
        DaypartSegment.DAWN,
        DaypartSegment.DAY,
        DaypartSegment.DUSK,
        DaypartSegment.NIGHT,
    ]
    n = ROTATION_CATEGORIES.medium_daypart_count
    assert tier_filter_dayparts(all_dps, "MEDIUM") == all_dps[:n]


def test_light_keeps_first_one_by_default():
    all_dps = [
        DaypartSegment.DAY,
        DaypartSegment.DUSK,
        DaypartSegment.NIGHT,
    ]
    n = ROTATION_CATEGORIES.light_daypart_count
    result = tier_filter_dayparts(all_dps, "LIGHT")
    assert len(result) == n
    assert result == all_dps[:n]


def test_legacy_discovery_label_treated_as_light():
    """Legacy DBs may have 'DISCOVERY' as tier value — treat as LIGHT."""
    all_dps = [DaypartSegment.DAY, DaypartSegment.DUSK, DaypartSegment.NIGHT]
    assert tier_filter_dayparts(all_dps, "DISCOVERY") == all_dps[: ROTATION_CATEGORIES.light_daypart_count]


def test_tier_rank_ordering():
    """LIGHT < MEDIUM < HEAVY, and legacy DISCOVERY aliases LIGHT (rank 0)."""
    assert TIER_RANK["LIGHT"] < TIER_RANK["MEDIUM"] < TIER_RANK["HEAVY"]
    assert TIER_RANK.get("DISCOVERY") == 0


# --- math sanity: the system actually creates differentiation ---


def test_average_track_after_2_months_is_medium_at_least():
    """A track at the library mean rate should land in MEDIUM or better."""
    expected = ROTATION_CATEGORIES.expected_plays_per_day
    age = 60
    plays = int(age * expected)  # exactly average
    result = compute_rotation_tier(plays, age)
    assert result in ("MEDIUM", "HEAVY")


def test_disabled_config_returns_all_dayparts():
    """Sanity: if ROTATION_CATEGORIES is disabled, every tier keeps all dayparts."""
    import importlib
    cfg = importlib.import_module("config")
    original = cfg.ROTATION_CATEGORIES.enabled
    try:
        cfg.ROTATION_CATEGORIES.enabled = False
        dps = [DaypartSegment.DAY, DaypartSegment.DUSK]
        assert tier_filter_dayparts(dps, "LIGHT") == dps
        assert tier_filter_dayparts(dps, "MEDIUM") == dps
    finally:
        cfg.ROTATION_CATEGORIES.enabled = original


# --- Discovery/Library weighted playlist variants (2026-07) ---


class TestPlaylistNameForTier:
    """HEAVY (grace + proven) plays from the high-weight Discovery variant;
    MEDIUM/LIGHT/GOLD play from the low-weight Library (base) playlist."""

    def test_heavy_maps_to_discovery_variant(self):
        from config import playlist_name_for_tier
        assert playlist_name_for_tier(DaypartSegment.DAWN, "HEAVY") == "Dawn-Discovery"

    def test_non_heavy_tiers_map_to_base_playlist(self):
        from config import playlist_name_for_tier
        for tier in ("MEDIUM", "LIGHT", "GOLD", "DISCOVERY"):
            assert playlist_name_for_tier(DaypartSegment.NIGHT, tier) == "Night"

    def test_all_playlist_names_includes_both_variants(self):
        from config import get_all_playlist_names, get_enabled_dayparts
        names = get_all_playlist_names()
        for dp in get_enabled_dayparts():
            assert dp.value in names
            assert f"{dp.value}-Discovery" in names
        assert len(names) == 2 * len(get_enabled_dayparts())

    def test_discovery_weight_higher_than_library(self):
        assert ROTATION_CATEGORIES.discovery_weight > ROTATION_CATEGORIES.library_weight


class TestTargetPlaylistNames:
    """target_playlist_names is the single source of truth for
    mood+tier → AzuraCast playlist names (upload, re-tier, GOLD,
    zero-play remediation, reanalysis all delegate to it)."""

    def test_heavy_gets_discovery_variants_of_all_mood_dayparts(self):
        from classify import target_playlist_names
        from config import get_dayparts_for_mood
        names = target_playlist_names("Calm", "HEAVY")
        expected = [f"{dp.value}-Discovery" for dp in get_dayparts_for_mood("Calm")]
        assert names == expected

    def test_medium_gets_base_playlists_capped_at_medium_count(self):
        from classify import target_playlist_names
        names = target_playlist_names("Calm", "MEDIUM")
        assert len(names) <= ROTATION_CATEGORIES.medium_daypart_count
        assert all("-Discovery" not in n for n in names)

    def test_legacy_discovery_tier_behaves_like_light(self):
        from classify import target_playlist_names
        assert target_playlist_names("Calm", "DISCOVERY") == \
               target_playlist_names("Calm", "LIGHT")

    def test_gold_behaves_like_light_on_base_playlists(self):
        from classify import target_playlist_names
        names = target_playlist_names("Sad", "GOLD")
        assert len(names) <= ROTATION_CATEGORIES.light_daypart_count
        assert all("-Discovery" not in n for n in names)
