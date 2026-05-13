"""Tests for discovery_sources.py — RSS title parsers + stable IDs."""
from __future__ import annotations

import pytest

from discovery_sources import (  # type: ignore[import-not-found]
    PARSERS,
    parse_dash,
    parse_pitchfork,
    parse_tilde,
    _stable_id,
    _make_track,
)


@pytest.mark.parametrize(
    "title,expected",
    [
        # Real Gorilla vs Bear samples (em-dash)
        ("Joanne Robertson + Oliver Coates – Always Were", ("Joanne Robertson + Oliver Coates", "Always Were")),
        ("Fine – Portal", ("Fine", "Portal")),
        ("Jessy Lanza – Slapped By My Life", ("Jessy Lanza", "Slapped By My Life")),
        # ASCII hyphen should also work (used by many feeds)
        ("Some Artist - Some Track", ("Some Artist", "Some Track")),
        # Items without separators must NOT match (e.g. tour announcements)
        ("gorilla vs. bear modern yacht rock 2025 end of summer mix", None),
        ("", None),
    ],
)
def test_parse_dash(title, expected):
    entry = {"title": title}
    assert parse_dash(entry) == expected


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Lawrence English / Werner Dafeldecker ~ Fathom Tides",
         ("Lawrence English / Werner Dafeldecker", "Fathom Tides")),
        ("SHHE ~ THALASSA", ("SHHE", "THALASSA")),
        ("No separator here", None),
    ],
)
def test_parse_tilde(title, expected):
    entry = {"title": title}
    assert parse_tilde(entry) == expected


@pytest.mark.parametrize(
    "title,link,expected",
    [
        # Real Pitchfork samples
        ("“Coconut Water”",
         "https://pitchfork.com/reviews/tracks/trim-coconut-water/",
         ("Trim", "Coconut Water")),
        ("“Rock Music”",
         "https://pitchfork.com/reviews/tracks/charli-xcx-rock-music/",
         ("Charli Xcx", "Rock Music")),
        ("“Bleed (Jane Remover Diss)”",
         "https://pitchfork.com/reviews/tracks/young-dabo-bleed-jane-remover-diss/",
         ("Young Dabo", "Bleed (Jane Remover Diss)")),
        # Atypical title (symbols collapse differently) — should fail gracefully
        ("“D!e”",
         "https://pitchfork.com/reviews/tracks/north-west-die/",
         None),
        # Missing link → cannot extract artist
        ("“Some Track”", "", None),
        # Title slug not present at end of URL → fail
        ("“Unrelated”", "https://pitchfork.com/reviews/tracks/foo-bar/", None),
    ],
)
def test_parse_pitchfork(title, link, expected):
    entry = {"title": title, "link": link}
    assert parse_pitchfork(entry) == expected


def test_decode_entities_in_dash():
    """Stereogum gives HTML-decoded text via feedparser, but the safety net
    in _decode_entities should also handle still-encoded entities."""
    # feedparser already decodes, but make sure parser is resilient
    entry = {"title": "LL Burns – “Got It All”"}
    assert parse_dash(entry) == ("LL Burns", "Got It All")


def test_parsers_registry_contains_all_strategies():
    assert set(PARSERS) == {"dash", "tilde", "dash_quoted", "pitchfork"}


def test_stable_id_is_deterministic_per_source_and_track():
    a = _stable_id("hypem", "Beach House", "Space Song")
    b = _stable_id("hypem", "Beach House", "Space Song")
    assert a == b
    assert a.startswith("hypem_")
    # Case insensitive
    assert _stable_id("hypem", "BEACH HOUSE", "space song") == a
    # Different source → different id
    assert _stable_id("rss", "Beach House", "Space Song") != a


def test_make_track_canonical_shape():
    t = _make_track("rss", "  Beach House ", "  Space Song  ", cover="http://x")
    assert t["artist"] == "Beach House"
    assert t["title"] == "Space Song"
    assert t["search"] == "Beach House - Space Song"
    assert t["cover"] == "http://x"
    assert t["source"] == "rss"
    assert isinstance(t["id"], str) and len(t["id"]) > 0
