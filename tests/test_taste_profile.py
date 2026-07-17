"""
Tests for the personal taste profile: library sampling, k-NN scoring,
profile persistence, and the PersonalArtistsSource discovery rotation.
"""

import json
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from discovery_sources import PersonalArtistsSource
from taste_profile import (
    TasteProfile,
    load_seed_artists,
    load_taste_profile,
    normalize_artist_name,
    sample_library,
    save_taste_profile,
)


def _unit(v):
    v = np.asarray(v, dtype=np.float32)
    return v / np.linalg.norm(v)


# ---------------------------------------------------------------------------
# sample_library
# ---------------------------------------------------------------------------

def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")


def test_sample_library_spreads_across_albums(tmp_path):
    _touch(tmp_path / "Air" / "Moon Safari (1998)" / "01 La Femme.flac")
    _touch(tmp_path / "Air" / "Moon Safari (1998)" / "02 Sexy Boy.flac")
    _touch(tmp_path / "Air" / "Talkie Walkie (2004)" / "01 Venus.flac")

    samples = sample_library(tmp_path, per_artist=2)
    files = [p.name for _, p in samples]
    # One track from each album, not two from the same one.
    assert files == ["01 La Femme.flac", "01 Venus.flac"]
    assert all(a == "Air" for a, _ in samples)


def test_sample_library_single_album_goes_deeper(tmp_path):
    _touch(tmp_path / "Solo" / "Album" / "01.flac")
    _touch(tmp_path / "Solo" / "Album" / "02.flac")
    samples = sample_library(tmp_path, per_artist=2)
    assert [p.name for _, p in samples] == ["01.flac", "02.flac"]


def test_sample_library_skips_system_dirs_and_non_audio(tmp_path):
    _touch(tmp_path / "$RECYCLE.BIN" / "junk.flac")
    _touch(tmp_path / "System Volume Information" / "x.mp3")
    _touch(tmp_path / "Real Artist" / "cover.jpg")
    _touch(tmp_path / "Real Artist" / "song.mp3")
    samples = sample_library(tmp_path, per_artist=2)
    assert [(a, p.name) for a, p in samples] == [("Real Artist", "song.mp3")]


def test_sample_library_deterministic(tmp_path):
    for artist in ("B Artist", "A Artist"):
        _touch(tmp_path / artist / "Album" / "01.flac")
    assert sample_library(tmp_path) == sample_library(tmp_path)
    assert sample_library(tmp_path)[0][0] == "A Artist"


def test_sample_library_missing_root():
    assert sample_library(Path("/nonexistent/path")) == []


# ---------------------------------------------------------------------------
# TasteProfile.score
# ---------------------------------------------------------------------------

def test_score_identical_vector_is_one():
    rows = np.stack([_unit([1, 0, 0]), _unit([0, 1, 0]), _unit([0, 0, 1])])
    profile = TasteProfile(embeddings=rows, artists=[], entry_paths=[])
    assert profile.score(_unit([1, 0, 0]), k=1) == pytest.approx(1.0)


def test_score_mean_of_top_k():
    rows = np.stack([_unit([1, 0]), _unit([0, 1]), _unit([1, 1])])
    profile = TasteProfile(embeddings=rows, artists=[], entry_paths=[])
    q = _unit([1, 0])
    # sims: 1.0, 0.0, 0.7071 → top-2 mean = 0.8536
    assert profile.score(q, k=2) == pytest.approx((1.0 + 0.70710678) / 2, abs=1e-4)


def test_score_k_larger_than_profile_is_clamped():
    rows = np.stack([_unit([1, 0]), _unit([0, 1])])
    profile = TasteProfile(embeddings=rows, artists=[], entry_paths=[])
    assert profile.score(_unit([1, 0]), k=50) == pytest.approx(0.5)


def test_score_empty_profile_raises():
    profile = TasteProfile(
        embeddings=np.zeros((0, 4), dtype=np.float32), artists=[], entry_paths=[])
    with pytest.raises(ValueError):
        profile.score(_unit([1, 0, 0, 0]))


# ---------------------------------------------------------------------------
# Persistence round-trip
# ---------------------------------------------------------------------------

def test_save_and_load_roundtrip(tmp_path):
    emb = np.stack([_unit([1, 0, 0]), _unit([0, 1, 0])])
    entries = [
        {"path": "/m/Air/a.flac", "artist": "Air"},
        {"path": "/m/Aïr/b.flac", "artist": "aír"},   # same artist after normalization
    ]
    save_taste_profile(tmp_path, emb, entries, built_at="2026-07-17T00:00:00Z")

    profile = load_taste_profile(tmp_path)
    assert profile is not None
    assert profile.size == 2
    np.testing.assert_allclose(profile.embeddings, emb)
    # Artist dedup is accent/case-insensitive.
    assert profile.artists == ["Air"]
    assert load_seed_artists(tmp_path) == ["Air"]


def test_load_missing_profile_returns_none(tmp_path):
    assert load_taste_profile(tmp_path) is None
    assert load_seed_artists(tmp_path) == []


def test_load_out_of_sync_profile_returns_none(tmp_path):
    emb = np.stack([_unit([1, 0])])
    save_taste_profile(tmp_path, emb, [{"path": "a", "artist": "A"}], built_at="x")
    # Corrupt: index claims 2 entries, matrix has 1.
    idx_path = tmp_path / "taste_profile_index.json"
    idx = json.loads(idx_path.read_text())
    idx["entries"].append({"path": "b", "artist": "B"})
    idx_path.write_text(json.dumps(idx))
    assert load_taste_profile(tmp_path) is None


def test_normalize_artist_name():
    assert normalize_artist_name("Aïr!") == normalize_artist_name("air")
    assert normalize_artist_name("Amadou & Mariam") == "amadou mariam"


# ---------------------------------------------------------------------------
# PersonalArtistsSource
# ---------------------------------------------------------------------------

def _source(tmp_path, seeds, **kw):
    defaults = dict(
        api_key="k",
        seeds=seeds,
        cursor_path=tmp_path / "cursor.json",
        seeds_per_run=2,
        similar_per_seed=2,
        tracks_per_artist=1,
    )
    defaults.update(kw)
    return PersonalArtistsSource(**defaults)


def test_seed_rotation_persists_cursor(tmp_path):
    src = _source(tmp_path, ["A", "B", "C"])
    assert src._pick_seeds() == ["A", "B"]
    assert src._pick_seeds() == ["C", "A"]      # wraps around
    # A fresh instance reads the persisted cursor.
    src2 = _source(tmp_path, ["A", "B", "C"])
    assert src2._pick_seeds() == ["B", "C"]


def test_pick_seeds_empty():
    src = PersonalArtistsSource(api_key="k", seeds=[])
    assert src._pick_seeds() == []
    assert src.fetch() == []


def test_get_similar_filters_known_and_weak(tmp_path):
    src = _source(tmp_path, ["Air", "Beach House"])
    response = {"similarartists": {"artist": [
        {"name": "Beach House", "match": "0.9"},   # already a seed → excluded
        {"name": "Weak Match", "match": "0.1"},    # below min_match → excluded
        {"name": "Stereolab", "match": "0.8"},
        {"name": "Broadcast", "match": "0.7"},
        {"name": "Extra One", "match": "0.6"},     # beyond similar_per_seed cap
    ]}}
    with patch("discovery_sources._http_get_json", return_value=response):
        assert src._get_similar("Air") == ["Stereolab", "Broadcast"]


def test_fetch_dedupes_similar_artists_across_seeds(tmp_path):
    src = _source(tmp_path, ["A", "B"])
    similar = {"similarartists": {"artist": [{"name": "Same Artist", "match": "0.9"}]}}
    top = {"toptracks": {"track": [{"name": "Hit Song", "image": []}]}}

    def fake_get(url, headers=None):
        return similar if "getsimilar" in url else top

    with patch("discovery_sources._http_get_json", side_effect=fake_get):
        tracks = src.fetch()
    # Both seeds return the same similar artist → only one set of top tracks.
    assert len(tracks) == 1
    assert tracks[0]["artist"] == "Same Artist"
    assert tracks[0]["title"] == "Hit Song"
    assert tracks[0]["source"] == "personal"


def test_fetch_survives_api_failure(tmp_path):
    src = _source(tmp_path, ["A"])
    with patch("discovery_sources._http_get_json", return_value=None):
        assert src.fetch() == []
