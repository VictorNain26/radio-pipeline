"""Tests for genre_client.py — blocklist/allowlist + cache + dedup of tags."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from genre_client import (  # type: ignore[import-not-found]
    DiscogsClient,
    GenreClient,
    MusicBrainzClient,
    _DiskCache,
)


class _FakeLastFM:
    """Stand-in for LastFMClient with a configurable tag map."""

    def __init__(self, track_tags: dict[tuple[str, str], list[str]] | None = None,
                 artist_tags: dict[str, list[str]] | None = None) -> None:
        self.track_tags = track_tags or {}
        self.artist_tags = artist_tags or {}

    def get_track_tags(self, artist: str, title: str) -> list[str]:
        return list(self.track_tags.get((artist, title), []))

    def get_artist_tags(self, artist: str) -> list[str]:
        return list(self.artist_tags.get(artist, []))


class _FakeMB(MusicBrainzClient):
    def __init__(self, tags_by_key: dict[tuple[str, str], list[str]]) -> None:
        self._tags = tags_by_key

    def get_tags(self, artist: str, title: str) -> list[str]:
        return list(self._tags.get((artist, title), []))


class _FakeDC(DiscogsClient):
    def __init__(self, tags_by_key: dict[tuple[str, str], list[str]]) -> None:
        self._tags = tags_by_key

    def get_tags(self, artist: str, title: str) -> list[str]:
        return list(self._tags.get((artist, title), []))


def _build(tmp_path: Path, *,
           mb: dict[tuple[str, str], list[str]] | None = None,
           dc: dict[tuple[str, str], list[str]] | None = None,
           lf_track: dict[tuple[str, str], list[str]] | None = None,
           lf_artist: dict[str, list[str]] | None = None,
           blocked: set[str] | None = None) -> GenreClient:
    return GenreClient(
        blocked_genres=blocked or {"metal", "death metal", "grindcore"},
        lastfm=_FakeLastFM(lf_track, lf_artist),
        musicbrainz=_FakeMB(mb or {}),
        discogs=_FakeDC(dc or {}),
        cache=_DiskCache(tmp_path / "genre_cache.json"),
    )


def test_blocklist_hard_rejects_when_any_source_matches(tmp_path):
    gc = _build(tmp_path, mb={("X", "Y"): ["rock", "death metal"]})
    r = gc.check_genre("X", "Y")
    assert r.is_blocked is True
    assert "death metal" in (r.blocked_reason or "")


def test_unblocked_tags_accepted(tmp_path):
    gc = _build(tmp_path, dc={("Beach House", "Space Song"): ["rock", "dream pop"]})
    r = gc.check_genre("Beach House", "Space Song")
    assert r.is_blocked is False


def test_no_tags_is_not_blocked(tmp_path):
    gc = _build(tmp_path)
    r = gc.check_genre("Unknown", "Obscure Track")
    assert r.is_blocked is False
    assert r.tags == []


def test_lastfm_artist_fallback_used_when_track_has_no_tags(tmp_path):
    gc = _build(
        tmp_path,
        lf_track={},
        lf_artist={"Artist X": ["ambient", "dream pop"]},
    )
    r = gc.check_genre("Artist X", "Whatever")
    # Tags came from the artist-level fallback
    assert "ambient" in r.tags or "dream pop" in r.tags


def test_union_of_tags_no_duplicates(tmp_path):
    gc = _build(
        tmp_path,
        mb={("A", "B"): ["indie", "rock"]},
        dc={("A", "B"): ["indie", "shoegaze"]},
        lf_track={("A", "B"): ["indie", "dream pop"]},
    )
    r = gc.check_genre("A", "B")
    # union has indie + rock + shoegaze + dream pop, no duplicates
    assert sorted(r.tags) == sorted({"indie", "rock", "shoegaze", "dream pop"})


def test_disk_cache_persists_across_runs(tmp_path):
    cache_path = tmp_path / "genre_cache.json"

    # First run populates the cache and flushes
    gc1 = GenreClient(
        blocked_genres=set(),
        lastfm=_FakeLastFM(),
        musicbrainz=_FakeMB({("A", "B"): ["indie"]}),
        discogs=_FakeDC({}),
        cache=_DiskCache(cache_path),
    )
    gc1.check_genre("A", "B")
    gc1.flush_cache()
    assert cache_path.exists()

    # Second run with NO backends — must still hit cache
    gc2 = GenreClient(
        blocked_genres=set(),
        lastfm=None,
        musicbrainz=_FakeMB({}),  # empty: would return []
        discogs=_FakeDC({}),
        cache=_DiskCache(cache_path),
    )
    r = gc2.check_genre("A", "B")
    assert "indie" in r.tags
    assert r.sources_hit == ["cache"]


def test_cache_ttl_expires(tmp_path):
    cache = _DiskCache(tmp_path / "c.json", ttl=1)  # 1 second TTL
    cache.put("k", ["foo"])
    assert cache.get("k") == ["foo"]
    time.sleep(1.1)
    assert cache.get("k") is None
