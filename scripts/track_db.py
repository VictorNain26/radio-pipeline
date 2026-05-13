"""
Persistent SQLite tracking for track rotation.

Stores upload timestamps, play counts, and deletion history
to enable age-based tiered rotation and cooldown logic.
"""

import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def normalize_track_key(artist: str, title: str) -> str:
    """Create normalized key for track comparison (mirrors download.normalize_track_key)."""
    artist = artist.lower().strip()
    title = title.lower().strip()
    for char in ['(', ')', '[', ']', '"', "'"]:
        artist = artist.replace(char, '')
        title = title.replace(char, '')
    artist = ' '.join(artist.split())
    title = ' '.join(title.split())
    return f"{artist} - {title}"


class TrackDB:
    """SQLite-backed track database for rotation tracking."""

    def __init__(self, db_path: str | Path):
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
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
                mood TEXT
            );

            CREATE TABLE IF NOT EXISTS sync_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
        self.conn.commit()

    def record_upload(
        self, track_key: str, artist: str, title: str, file_id: int, mood: str | None = None
    ) -> None:
        """Record a track upload (or re-upload)."""
        now = time.time()
        self.conn.execute(
            """INSERT INTO tracks (track_key, artist, title, uploaded_at, deleted_at,
                                   azuracast_file_id, play_count, mood)
               VALUES (?, ?, ?, ?, NULL, ?, 0, ?)
               ON CONFLICT(track_key) DO UPDATE SET
                   uploaded_at = excluded.uploaded_at,
                   deleted_at = NULL,
                   azuracast_file_id = excluded.azuracast_file_id,
                   play_count = 0,
                   mood = excluded.mood""",
            (track_key, artist, title, now, file_id, mood),
        )
        self.conn.commit()

    def record_deletion(self, track_key: str) -> None:
        """Mark a track as deleted."""
        now = time.time()
        self.conn.execute(
            """UPDATE tracks SET deleted_at = ?, azuracast_file_id = NULL
               WHERE track_key = ?""",
            (now, track_key),
        )
        self.conn.commit()

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

        # Update last sync timestamp
        now = str(time.time())
        self.conn.execute(
            """INSERT INTO sync_state (key, value) VALUES ('last_history_sync', ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (now,),
        )
        self.conn.commit()
        logger.info("Synced play counts from %d history entries", len(history_entries))

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

    def is_in_cooldown(self, track_key: str, cooldown_days: int) -> bool:
        """Check if a deleted track is still in cooldown period."""
        row = self.conn.execute(
            "SELECT deleted_at FROM tracks WHERE track_key = ?",
            (track_key,),
        ).fetchone()
        if not row or row["deleted_at"] is None:
            return False
        return (time.time() - row["deleted_at"]) < cooldown_days * 86400

    def get_active_tracks(self) -> list[dict[str, Any]]:
        """Return all active (non-deleted, with file_id) tracks."""
        rows = self.conn.execute(
            """SELECT track_key, artist, title, uploaded_at, azuracast_file_id,
                      play_count, mood
               FROM tracks
               WHERE azuracast_file_id IS NOT NULL AND deleted_at IS NULL"""
        ).fetchall()
        return [dict(r) for r in rows]

    def get_track_by_file_id(self, file_id: int) -> dict[str, Any] | None:
        """Look up a track by its AzuraCast file ID."""
        row = self.conn.execute(
            """SELECT track_key, artist, title, uploaded_at, azuracast_file_id,
                      play_count, mood
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

    def update_mood(self, track_key: str, mood: str) -> None:
        """Update the mood tag for a track."""
        self.conn.execute(
            "UPDATE tracks SET mood = ? WHERE track_key = ?", (mood, track_key)
        )
        self.conn.commit()

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
