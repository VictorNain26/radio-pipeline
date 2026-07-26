"""
Tests for track_db: normalize_track_key (the join key of the whole system),
concurrency safety, and the history sync cursor.
"""

import threading

import pytest

from track_db import TrackDB, normalize_track_key


# ---------------------------------------------------------------------------
# normalize_track_key
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("artist,title,expected", [
    ("Beach House", "Space Song", "beach house - space song"),
    ("  Beach House  ", "  Space Song ", "beach house - space song"),
    ("BEACH HOUSE", "SPACE SONG", "beach house - space song"),
    ("M83", "Wait (Live)", "m83 - wait live"),
    ('Artist "X"', "Song [Remaster]", "artist x - song remaster"),
    ("A  B", "C   D", "a b - c d"),
])
def test_normalize_track_key(artist, title, expected):
    assert normalize_track_key(artist, title) == expected


def test_normalize_is_stable_join_key():
    """Variants that must all collapse to the same key."""
    keys = {
        normalize_track_key("Beach House", "Space Song"),
        normalize_track_key("beach house", "space song"),
        normalize_track_key(" Beach  House ", "Space   Song"),
    }
    assert len(keys) == 1


# ---------------------------------------------------------------------------
# TrackDB behaviour
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    with TrackDB(tmp_path / "test.db") as d:
        yield d


def test_upload_and_cooldown_lifecycle(db):
    db.record_upload("a - b", "a", "b", file_id=42, mood="Calm", tier="HEAVY")
    assert db.get_tier("a - b") == "HEAVY"
    assert not db.is_in_cooldown("a - b", cooldown_days=60)

    db.record_deletion("a - b")
    assert db.is_in_cooldown("a - b", cooldown_days=60)

    # Re-upload clears deletion state
    db.record_upload("a - b", "a", "b", file_id=43)
    assert not db.is_in_cooldown("a - b", cooldown_days=60)


def test_sync_cursor_uses_max_played_at_not_now(db):
    db.record_upload("a - b", "a", "b", file_id=1)
    entries = [
        {"song": {"artist": "a", "title": "b"}, "played_at": 1000.0},
        {"song": {"artist": "a", "title": "b"}, "played_at": 2000.0},
    ]
    db.sync_play_counts(entries)
    # Cursor must be the newest entry seen, not time.time(): plays landing
    # between the history query and the write must not be skipped next sync.
    assert db.get_last_sync_timestamp() == 2000.0
    track = db.get_track_by_file_id(1)
    assert track["play_count"] == 2


def test_fingerprint_roundtrip(db):
    db.record_upload("a - b", "a", "b", file_id=7)
    db.record_fingerprint("a - b", "deadbeef", 180)
    hit = db.find_by_fingerprint("deadbeef")
    assert hit is not None
    assert hit["track_key"] == "a - b"
    assert hit["azuracast_file_id"] == 7
    assert db.find_by_fingerprint("cafebabe") is None


def test_concurrent_access_is_safe(db):
    """3 download workers hammer the same connection — must not raise."""
    errors: list[Exception] = []

    def work(n: int) -> None:
        try:
            for i in range(100):
                key = f"k{n}-{i}"
                db.record_upload(key, "a", "t", i)
                db.is_in_cooldown(key, 60)
                db.record_fingerprint(key, f"h{n}-{i}", 100)
                db.find_by_fingerprint(f"h{n}-{i}")
        except Exception as e:  # noqa: BLE001 — the test asserts none occur
            errors.append(e)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(db.get_active_tracks()) == 300


def test_normalize_track_key_unifies_unicode_quotes():
    """U+2019 curly apostrophes (ID3 tags) and ASCII ' (AzuraCast metadata)
    must produce the same key, else play-count sync silently misses tracks
    (Abdullah Abdelkader stuck at 0 plays for 37 days, 2026-07-19)."""
    from track_db import normalize_track_key
    assert normalize_track_key("Abdullah Abdelkader", "Al Zaman Zamanak (It’s Your Time)") == \
           normalize_track_key("Abdullah Abdelkader", "Al Zaman Zamanak (It's Your Time)")
    assert normalize_track_key("Kool and Together", "Sittin’ On A Red Hot Stove") == \
           "kool and together - sittin on a red hot stove"
    assert normalize_track_key("X", "“Quoted” ‘Title´") == normalize_track_key("X", '"Quoted" Title')
