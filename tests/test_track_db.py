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
    """3 download workers hammer the same connection - must not raise."""
    errors: list[Exception] = []

    def work(n: int) -> None:
        try:
            for i in range(100):
                key = f"k{n}-{i}"
                db.record_upload(key, "a", "t", i)
                db.is_in_cooldown(key, 60)
                db.record_fingerprint(key, f"h{n}-{i}", 100)
                db.find_by_fingerprint(f"h{n}-{i}")
        except Exception as e:  # noqa: BLE001 - the test asserts none occur
            errors.append(e)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(db.get_active_tracks()) == 300


def test_normalize_track_key_unifies_unicode_quotes():
    """Unicode apostrophes (ID3 tags) and ASCII (AzuraCast metadata)
    must produce the same key, else play-count sync silently misses tracks
    (Abdullah Abdelkader stuck at 0 plays for 37 days, 2026-07-19)."""
    from track_db import normalize_track_key
    assert normalize_track_key("Abdullah Abdelkader", "Al Zaman Zamanak (It's Your Time)") == \
           normalize_track_key("Abdullah Abdelkader", "Al Zaman Zamanak (It's Your Time)")
    assert normalize_track_key("Kool and Together", "Sittin' On A Red Hot Stove") == \
           "kool and together - sittin on a red hot stove"
    # Smart quotes and curly apostrophes must normalize
    assert normalize_track_key("X", '"Quoted" Title') == normalize_track_key("X", '"Quoted" Title')


# ---------------------------------------------------------------------------
# Verdicts (rejection registry for cold-phase download)
# ---------------------------------------------------------------------------

def test_record_and_get_verdict(db):
    """Record a verdict and retrieve it."""
    db.record_verdict("a - b", "rejected_taste", reason="0.41 < 0.62", score=0.41)
    v = db.get_verdict("a - b")
    assert v is not None
    assert v["verdict"] == "rejected_taste"
    assert v["reason"] == "0.41 < 0.62"
    assert v["score"] == 0.41
    assert "decided_at" in v


def test_record_verdict_without_optional_fields(db):
    """Record a verdict with only required fields."""
    db.record_verdict("a - b", "rejected_taste", score=0.41)
    v = db.get_verdict("a - b")
    assert v is not None
    assert v["verdict"] == "rejected_taste"
    assert v["reason"] is None
    assert v["score"] == 0.41


def test_get_nonexistent_verdict(db):
    """Getting a verdict that doesn't exist returns None."""
    assert db.get_verdict("nonexistent - key") is None


def test_has_active_verdict_nonperishable(db):
    """Non-perishable verdicts are always active."""
    db.record_verdict("a - b", "some_verdict", score=0.5)
    # Even with taste_ttl_days=0, non-perishable verdicts stay active
    assert db.has_active_verdict("a - b", taste_ttl_days=0) is True


def test_has_active_verdict_perishable_fresh(db):
    """Fresh perishable verdicts are active."""
    db.record_verdict("a - b", "rejected_taste", score=0.41)
    # Just recorded, should be active
    assert db.has_active_verdict("a - b", taste_ttl_days=30) is True


def test_has_active_verdict_perishable_stale(db):
    """Old perishable verdicts expire."""
    import time
    db.record_verdict("a - b", "rejected_taste", score=0.41)

    # Manually update decided_at to simulate an old verdict
    old_time = time.time() - (31 * 86400)  # 31 days ago
    db.conn.execute(
        "UPDATE verdicts SET decided_at = ? WHERE track_key = ?",
        (old_time, "a - b")
    )
    db.conn.commit()

    # With taste_ttl_days=30, the verdict is now stale
    assert db.has_active_verdict("a - b", taste_ttl_days=30) is False


def test_has_active_verdict_nonexistent(db):
    """Non-existent verdicts return False."""
    assert db.has_active_verdict("nonexistent - key", taste_ttl_days=30) is False


def test_verdict_cleared_on_upload(db):
    """A verdict is deleted when the track is uploaded."""
    db.record_verdict("a - b", "rejected_taste", score=0.41)
    assert db.get_verdict("a - b") is not None

    # Upload the track
    db.record_upload("a - b", "A", "B", file_id=7)

    # Verdict should be gone
    assert db.get_verdict("a - b") is None


# ---------------------------------------------------------------------------
# Track key repair (handle AzuraCast metadata drift)
# ---------------------------------------------------------------------------

def test_repair_track_key_basic(db):
    """Repair a track key when metadata drifts."""
    db.record_upload("ancien - titre", "Ancien", "Titre", file_id=7)
    db.record_fingerprint("ancien - titre", "HASH", 200)

    assert db.repair_track_key("ancien - titre", "nouveau - titre") is True

    # Track should be at new key
    track = db.get_track_by_file_id(7)
    assert track is not None
    assert track["track_key"] == "nouveau - titre"

    # Fingerprint should also move
    fp = db.find_by_fingerprint("HASH")
    assert fp is not None
    assert fp["track_key"] == "nouveau - titre"


def test_repair_track_key_no_old_track(db):
    """Repair fails if old key doesn't exist."""
    assert db.repair_track_key("nonexistent - key", "new - key") is False


def test_repair_track_key_same_key(db):
    """Repair fails if old and new keys are the same."""
    db.record_upload("a - b", "A", "B", file_id=7)
    assert db.repair_track_key("a - b", "a - b") is False


def test_repair_track_key_empty_keys(db):
    """Repair fails if either key is empty."""
    db.record_upload("a - b", "A", "B", file_id=7)
    assert db.repair_track_key("", "new - key") is False
    assert db.repair_track_key("a - b", "") is False


def test_repair_track_key_with_collision(db):
    """When new key exists, the old collision is deleted first."""
    db.record_upload("ancien - titre", "Ancien", "Titre", file_id=7)
    db.record_fingerprint("ancien - titre", "HASH1", 200)

    db.record_upload("nouveau - titre", "Nouveau", "Titre", file_id=99)
    db.record_fingerprint("nouveau - titre", "HASH2", 180)

    # Repair: old track moves to new key, collision is deleted
    assert db.repair_track_key("ancien - titre", "nouveau - titre") is True

    # After repair, only one row at "nouveau - titre" with file_id=7
    rows = db.conn.execute(
        "SELECT track_key, azuracast_file_id FROM tracks WHERE track_key = 'nouveau - titre'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["azuracast_file_id"] == 7


