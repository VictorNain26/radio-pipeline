"""
Persistent SQLite tracking for track rotation.

Stores upload timestamps, play counts, and deletion history
to enable age-based tiered rotation and cooldown logic.
"""

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def normalize_track_key(artist: str, title: str) -> str:
    """Create normalized key for track comparison (mirrors download.normalize_track_key)."""
    artist = artist.lower().strip()
    title = title.lower().strip()
    # Unicode quotes/apostrophes must strip like their ASCII forms: ID3 tags
    # often carry ’ where AzuraCast metadata has ' — divergent keys break
    # play-count sync (tracks stuck at 0 plays forever).
    for char in ['(', ')', '[', ']', '"', "'", '’', '‘', '“', '”', '´', '`']:
        artist = artist.replace(char, '')
        title = title.replace(char, '')
    artist = ' '.join(artist.split())
    title = ' '.join(title.split())
    return f"{artist} - {title}"



def _synchronized(method):
    """Serialize access to the shared sqlite3 connection across threads."""
    import functools

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


class TrackDB:
    """SQLite-backed track database for rotation tracking."""

    def __init__(self, db_path: str | Path):
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        # The connection is shared across download worker threads;
        # sqlite3.Connection is not safe for concurrent execute/commit.
        self._lock = threading.RLock()
        self._create_tables()

    def __enter__(self) -> "TrackDB":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _create_tables(self) -> None:
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS tracks (
                track_key TEXT PRIMARY KEY,
                artist TEXT NOT NULL,
                title TEXT NOT NULL,
                uploaded_at REAL NOT NULL,
                deleted_at REAL,
                azuracast_file_id INTEGER,
                play_count INTEGER DEFAULT 0,
                mood TEXT,
                -- Rotation category: HEAVY / MEDIUM / LIGHT (see
                -- config.ROTATION_CATEGORIES). New tracks start HEAVY
                -- (grace period); enforce_tiered_rotation re-tiers them
                -- up or down on every run based on age and play rate.
                tier TEXT DEFAULT 'DISCOVERY'
            );

            -- Idempotent migration : on existing DBs, ensure the new column
            -- is there even if the table predates it.

            CREATE TABLE IF NOT EXISTS sync_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            -- Content-based dedup via Chromaprint
            -- (see scripts/audio_fingerprint.py). One row per track_key seen
            -- by the pipeline (whether it ended up uploaded or rejected).
            -- The hash is indexed for O(1) lookup at download time.
            CREATE TABLE IF NOT EXISTS audio_fingerprints (
                track_key TEXT PRIMARY KEY,
                fingerprint_hash TEXT NOT NULL,
                duration_sec INTEGER,
                computed_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_fp_hash
                ON audio_fingerprints(fingerprint_hash);
        """)
        # Ensure tier column exists on legacy DBs (idempotent).
        try:
            cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(tracks)").fetchall()}
            if "tier" not in cols:
                self.conn.execute("ALTER TABLE tracks ADD COLUMN tier TEXT DEFAULT 'DISCOVERY'")
        except sqlite3.OperationalError:
            pass
        self.conn.commit()

    # ------------------------------------------------------------------
    # Audio fingerprint (Chromaprint) — see audio_fingerprint.py
    # ------------------------------------------------------------------

    @_synchronized
    def record_fingerprint(
        self, track_key: str, fingerprint_hash: str, duration_sec: int
    ) -> None:
        """Persist the Chromaprint hash for this track_key (idempotent)."""
        self.conn.execute(
            """INSERT INTO audio_fingerprints
                   (track_key, fingerprint_hash, duration_sec, computed_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(track_key) DO UPDATE SET
                   fingerprint_hash = excluded.fingerprint_hash,
                   duration_sec = excluded.duration_sec,
                   computed_at = excluded.computed_at""",
            (track_key, fingerprint_hash, duration_sec, time.time()),
        )
        self.conn.commit()

    @_synchronized
    def find_by_fingerprint(self, fingerprint_hash: str) -> dict[str, Any] | None:
        """Return the row that matches a Chromaprint hash, or None."""
        row = self.conn.execute(
            """SELECT af.track_key, af.duration_sec, af.computed_at,
                      t.artist, t.title, t.azuracast_file_id
               FROM audio_fingerprints af
               LEFT JOIN tracks t ON t.track_key = af.track_key
               WHERE af.fingerprint_hash = ?
               LIMIT 1""",
            (fingerprint_hash,),
        ).fetchone()
        return dict(row) if row else None

    @_synchronized
    def record_upload(
        self, track_key: str, artist: str, title: str, file_id: int,
        mood: str | None = None, tier: str = "DISCOVERY",
    ) -> None:
        """Record a track upload (or re-upload)."""
        now = time.time()
        self.conn.execute(
            """INSERT INTO tracks (track_key, artist, title, uploaded_at, deleted_at,
                                   azuracast_file_id, play_count, mood, tier)
               VALUES (?, ?, ?, ?, NULL, ?, 0, ?, ?)
               ON CONFLICT(track_key) DO UPDATE SET
                   uploaded_at = excluded.uploaded_at,
                   deleted_at = NULL,
                   azuracast_file_id = excluded.azuracast_file_id,
                   play_count = 0,
                   mood = excluded.mood,
                   tier = excluded.tier""",
            (track_key, artist, title, now, file_id, mood, tier),
        )
        self.conn.commit()

    @_synchronized
    def update_tier(self, track_key: str, tier: str) -> None:
        """Update the rotation tier of an existing track."""
        self.conn.execute(
            "UPDATE tracks SET tier = ? WHERE track_key = ?",
            (tier, track_key),
        )
        self.conn.commit()

    @_synchronized
    def get_tier(self, track_key: str) -> str | None:
        row = self.conn.execute(
            "SELECT tier FROM tracks WHERE track_key = ?", (track_key,)
        ).fetchone()
        return row["tier"] if row else None

    @_synchronized
    def record_deletion(self, track_key: str) -> None:
        """Mark a track as deleted."""
        now = time.time()
        self.conn.execute(
            """UPDATE tracks SET deleted_at = ?, azuracast_file_id = NULL
               WHERE track_key = ?""",
            (now, track_key),
        )
        self.conn.commit()

    @_synchronized
    def sync_play_counts(self, history_entries: list[dict[str, Any]]) -> None:
        """Increment play counts from AzuraCast history entries."""
        for entry in history_entries:
            song = entry.get("song", {})
            artist = song.get("artist", "") or ""
            title = song.get("title", "") or ""
            if not artist or not title:
                continue
            key = normalize_track_key(artist, title)
            self.conn.execute(
                "UPDATE tracks SET play_count = play_count + 1 WHERE track_key = ?",
                (key,),
            )

        # Advance the sync cursor to the newest entry actually seen, not to
        # "now" — plays occurring between the history query and this write
        # would otherwise be skipped by the next sync.
        played_ats = []
        for entry in history_entries:
            try:
                played_ats.append(float(entry.get("played_at") or 0))
            except (TypeError, ValueError):
                pass
        now = str(max(played_ats, default=time.time()))
        self.conn.execute(
            """INSERT INTO sync_state (key, value) VALUES ('last_history_sync', ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (now,),
        )
        self.conn.commit()
        logger.info("Synced play counts from %d history entries", len(history_entries))

    @_synchronized
    def get_last_sync_timestamp(self) -> float:
        """Return the last history sync timestamp, or 0.0 if never synced."""
        row = self.conn.execute(
            "SELECT value FROM sync_state WHERE key = 'last_history_sync'"
        ).fetchone()
        if row:
            try:
                return float(row["value"])
            except (ValueError, TypeError):
                pass
        return 0.0

    @_synchronized
    def is_in_cooldown(self, track_key: str, cooldown_days: int) -> bool:
        """Check if a deleted track is still in cooldown period."""
        row = self.conn.execute(
            "SELECT deleted_at FROM tracks WHERE track_key = ?",
            (track_key,),
        ).fetchone()
        if not row or row["deleted_at"] is None:
            return False
        return (time.time() - row["deleted_at"]) < cooldown_days * 86400

    @_synchronized
    def get_active_tracks(self) -> list[dict[str, Any]]:
        """Return all active (non-deleted, with file_id) tracks."""
        rows = self.conn.execute(
            """SELECT track_key, artist, title, uploaded_at, azuracast_file_id,
                      play_count, mood
               FROM tracks
               WHERE azuracast_file_id IS NOT NULL AND deleted_at IS NULL"""
        ).fetchall()
        return [dict(r) for r in rows]

    @_synchronized
    def get_track_by_file_id(self, file_id: int) -> dict[str, Any] | None:
        """Look up a track by its AzuraCast file ID."""
        row = self.conn.execute(
            """SELECT track_key, artist, title, uploaded_at, azuracast_file_id,
                      play_count, mood, tier
               FROM tracks WHERE azuracast_file_id = ?""",
            (file_id,),
        ).fetchone()
        return dict(row) if row else None

    def get_stats(self) -> dict[str, int]:
        """Return counts by tier based on ROTATION config thresholds."""
        try:
            from config import ROTATION
        except ImportError:
            # Fallback defaults if config not available
            class _R:
                fresh_days = 10
                current_days = 25
                max_age_days = 40
            ROTATION = _R()

        now = time.time()
        active = self.get_active_tracks()
        stats = {"fresh": 0, "current": 0, "fading": 0, "expired": 0, "total": len(active)}

        for t in active:
            age_days = (now - t["uploaded_at"]) / 86400
            if age_days <= ROTATION.fresh_days:
                stats["fresh"] += 1
            elif age_days <= ROTATION.current_days:
                stats["current"] += 1
            elif age_days <= ROTATION.max_age_days:
                stats["fading"] += 1
            else:
                stats["expired"] += 1

        return stats

    @_synchronized
    def update_mood(self, track_key: str, mood: str) -> None:
        """Update the mood tag for a track."""
        self.conn.execute(
            "UPDATE tracks SET mood = ? WHERE track_key = ?", (mood, track_key)
        )
        self.conn.commit()

    @_synchronized
    def register_untracked_file(
        self,
        track_key: str,
        artist: str,
        title: str,
        uploaded_at: float,
        file_id: int,
    ) -> None:
        """Register an existing AzuraCast file that has no local DB entry."""
        self.conn.execute(
            """INSERT INTO tracks (track_key, artist, title, uploaded_at, deleted_at,
                                   azuracast_file_id, play_count, mood)
               VALUES (?, ?, ?, ?, NULL, ?, 0, NULL)
               ON CONFLICT(track_key) DO UPDATE SET
                   azuracast_file_id = excluded.azuracast_file_id,
                   deleted_at = NULL""",
            (track_key, artist, title, uploaded_at, file_id),
        )
        self.conn.commit()

    def close(self) -> None:
        """Close the database connection."""
        self.conn.close()
