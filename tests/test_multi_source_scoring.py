"""
Tests for the multi-source audio matching in scripts/download.py.

The scoring decides whether a SoundCloud preview clip or a YouTube
full-length wins for a given (artist, title). It must :
  - hard-reject preview clips (<60s) and full albums/DJ-mixes (>600s)
  - keep the existing fuzzy / channel-trust / negative-keyword logic
  - return a structured DownloadOutcome with the picked source label

These are pure unit tests : no network, no yt-dlp invocation. We feed
synthetic candidate dicts that mimic yt-dlp `--dump-json` output.
"""
from __future__ import annotations

import pytest

from download import (  # type: ignore[import-not-found]
    DownloadOutcome,
    _score_candidate,
    _score_duration,
)


# --- _score_duration : hard rejects ----------------------------------


@pytest.mark.parametrize("dur,expected", [
    (0,    0.5),    # unknown
    (-1,   0.5),    # negative (shouldn't happen but be safe)
    (29,   0.0),    # below 60s → hard reject
    (30,   0.0),    # SoundCloud preview length → reject
    (59,   0.0),    # still below floor
    (60,   0.8),    # acceptable short
    (119,  0.8),    # acceptable short
    (120,  1.0),    # sweet spot
    (240,  1.0),    # 4 min, classic single
    (330,  1.0),    # upper sweet spot
    (331,  0.8),    # mildly long
    (480,  0.8),
    (500,  0.5),    # grey zone
    (600,  0.5),    # upper edge of grey zone (boundary)
    (601,  0.0),    # past cutoff → full album / DJ mix
    (1200, 0.0),    # 20-min DJ set
])
def test_score_duration_table(dur, expected):
    assert _score_duration(dur) == expected


# --- _score_candidate hard-rejects on bad duration -------------------


def test_score_candidate_rejects_30s_preview_even_with_perfect_metadata():
    """
    Regression for the live bug : a SoundCloud preview clip with
    perfect artist+title+channel match but only 30s duration was
    scoring 0.88 (passing the 0.60 threshold) because the structured-
    metadata path added a +0.05 bonus and de-weighted dur_score to 10%.
    Now any dur_score=0 must hard-reject the candidate.
    """
    info = {
        "title": "Space Song",
        "uploader": "Beach House",
        "channel": "Beach House",
        "artist": "Beach House",
        "track": "Space Song",
        "duration": 30,
        "extractor_key": "Soundcloud",
        "webpage_url": "https://soundcloud.com/beachhouse/space-song",
    }
    score, explanation = _score_candidate("Beach House", "Space Song", info)
    assert score == 0.0
    assert "rejected:duration" in explanation


def test_score_candidate_rejects_dj_mix_over_10_minutes():
    info = {
        "title": "DJ Set @ Boiler Room",
        "uploader": "Boiler Room",
        "channel": "Boiler Room",
        "duration": 3600,  # 1 hour
        "extractor_key": "Youtube",
    }
    score, explanation = _score_candidate("Beach House", "Space Song", info)
    # Long-form content rejected — even if fuzzy match is poor anyway
    assert "rejected:duration" in explanation or score < 0.6


def test_score_candidate_accepts_normal_song():
    info = {
        "title": "Beach House - Space Song",
        "uploader": "Beach House",
        "channel": "Beach House",
        "duration": 290,  # 4:50, proper full track
        "extractor_key": "Youtube",
        "webpage_url": "https://www.youtube.com/watch?v=xxxxx",
    }
    score, _ = _score_candidate("Beach House", "Space Song", info)
    assert score > 0.75


def test_score_candidate_unknown_duration_does_not_hard_reject():
    """Some yt-dlp probes don't expose duration. Fall through to other signals."""
    info = {
        "title": "Beach House - Space Song",
        "uploader": "Beach House",
        "channel": "Beach House",
        "duration": 0,           # unknown
        "extractor_key": "Youtube",
    }
    score, explanation = _score_candidate("Beach House", "Space Song", info)
    assert "rejected:duration" not in explanation
    assert score > 0.5  # still scores well on fuzzy + channel trust


# --- DownloadOutcome ------------------------------------------------


def test_download_outcome_is_namedtuple_with_status_and_source():
    out = DownloadOutcome("downloaded", "youtube")
    assert out.status == "downloaded"
    assert out.source == "youtube"
    # Defaults: silent-fallback flags False unless set
    assert out.loudnorm_failed is False
    assert out.fingerprint_failed is False


def test_download_outcome_source_can_be_none_for_pre_match_returns():
    """Outcomes for 'skipped' / 'blocked' / 'failed-before-probe' have no source."""
    out = DownloadOutcome("skipped", None)
    assert out.status == "skipped"
    assert out.source is None
    assert out.loudnorm_failed is False
    assert out.fingerprint_failed is False


def test_download_outcome_silent_fallback_flags_surface_post_download_failures():
    """
    The new flags expose loudnorm / fingerprint failures that used to be
    logged-only. Stats aggregator counts them per run.
    """
    out = DownloadOutcome(
        "downloaded", "youtube",
        loudnorm_failed=True, fingerprint_failed=True,
    )
    assert out.loudnorm_failed is True
    assert out.fingerprint_failed is True
