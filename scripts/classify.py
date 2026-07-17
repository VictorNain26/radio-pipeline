#!/usr/bin/env python3
"""
Upload tracks to AzuraCast with daypart-based playlist assignment.

Features v2.0:
- Reads 8-mood circumplex classification from ID3 tags
- Routes tracks to zone playlists (Dawn / Day / Dusk / Night)
- Uses energy levels for smooth programming
- Confidence-based quality filtering
- Professional separation rules support
- Robust HTTP client with retry logic and circuit breaker
"""

import logging
import sys
import time
from pathlib import Path
from typing import Any, TypedDict

from mutagen.id3 import ID3

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from http_client import AzuraCastClient as BaseAzuraCastClient, ClientError, HTTPConnectionError, ServerError, compute_file_hashes
from settings import get_settings, validate_environment
from track_db import TrackDB, normalize_track_key

try:
    from config import (
        MoodCategory,
        DaypartSegment,
        DayType,
        EnergyLevel,
        ROTATION,
        ROTATION_CATEGORIES,
        AUDIO_FILTERS,
        MULTI_SIGNAL_FILTER,
        TASTE_FILTER,
        get_dayparts_for_mood,
        get_enabled_dayparts,
        get_current_day_type,
        get_all_playlist_names,
        should_reject_track,
        is_mood_enabled,
        format_duration,
    )
except ImportError as e:
    print(f"Error: config.py not found or invalid in pipeline root: {e}")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Rotation tier system — designed for an AUTONOMOUS DISCOVERY webradio.
#
# Goal: new tracks must be heard (that's the whole point of "discovery"),
# while tracks that fail to engage listeners fade out. The semantics are
# the BBC 6 Music / MusicMaster A/B/C model:
#
#   HEAVY  — high rotation. Two reasons a track lands here:
#            (a) GRACE PERIOD : age < grace_period_days. Every new track
#                gets full visibility for its first ~2 weeks. No exception.
#            (b) PROVEN        : after the grace period, the play rate
#                (plays/day) is at or above the expected library average
#                (boosted by heavy_above_average_ratio).
#   MEDIUM — standard rotation. Post-grace tracks with average performance.
#   LIGHT  — fading rotation. Post-grace tracks with below-average plays.
#            Reduced exposure means they accumulate fewer plays, accelerating
#            their natural eviction at max_age_days.
#
# Mapping to playlists (tier_filter_dayparts):
#   HEAVY  → ALL mood-compatible dayparts (max exposure)
#   MEDIUM → medium_daypart_count dayparts (default 3)
#   LIGHT  → light_daypart_count dayparts (default 1)
#
# The expected play rate is computed from the library size + AzuraCast's
# observed plays/hour, set in config.ROTATION_CATEGORIES.expected_plays_per_day.
# It does NOT depend on per-track play history alone (which would be too
# noisy for short ages). It's an absolute reference point.
# ---------------------------------------------------------------------------

# Rank for promotion comparisons. Legacy "DISCOVERY" label maps to LIGHT
# (rank 0) — the rename happens implicitly via the re-tier pass.
TIER_RANK = {"LIGHT": 0, "DISCOVERY": 0, "MEDIUM": 1, "HEAVY": 2}


def compute_rotation_tier(play_count: int, age_days: float) -> str:
    """
    Compute the rotation tier for a track from its (play_count, age_days).

    Two cases:
    - In the grace period (age < grace_period_days) → always HEAVY
      regardless of plays. This is the DISCOVERY emphasis: new tracks
      get a fair shot at being heard.
    - Past the grace period → compare actual play rate against the
      library's expected rate:
        * rate >= expected × heavy_above_average_ratio  → HEAVY (proven hit)
        * rate >= expected × light_below_average_ratio  → MEDIUM (average)
        * else                                          → LIGHT (waning)
    """
    cfg = ROTATION_CATEGORIES
    if age_days < cfg.grace_period_days:
        return "HEAVY"
    rate = play_count / max(1.0, age_days)
    expected = cfg.expected_plays_per_day
    if rate >= expected * cfg.heavy_above_average_ratio:
        return "HEAVY"
    if rate >= expected * cfg.light_below_average_ratio:
        return "MEDIUM"
    return "LIGHT"


def tier_filter_dayparts(
    mood_dayparts: list[DaypartSegment], tier: str,
) -> list[DaypartSegment]:
    """
    Restrict the list of mood-compatible dayparts based on the rotation tier.

    Returns:
      HEAVY  → all matching dayparts (max exposure)
      MEDIUM → first medium_daypart_count matching dayparts
      LIGHT  → first light_daypart_count matching dayparts
      (legacy DISCOVERY treated as LIGHT)
    """
    if not ROTATION_CATEGORIES.enabled or tier == "HEAVY":
        return list(mood_dayparts)
    if tier == "MEDIUM":
        return list(mood_dayparts)[: ROTATION_CATEGORIES.medium_daypart_count]
    # LIGHT (or legacy DISCOVERY)
    return list(mood_dayparts)[: ROTATION_CATEGORIES.light_daypart_count]

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# Constants
UPLOAD_TIMEOUT = 180


class TrackFeatures(TypedDict):
    """Track features extracted from ID3 tags (v2.0 with circumplex model)."""
    artist: str
    title: str
    bpm: int
    mood: str | None
    mood_confidence: float
    energy_level: str | None
    duration: int
    valence: float
    arousal: float
    # Flag pour filtrage audio intelligent (True si Last.fm avait des tags)
    has_lastfm_tags: bool
    # Multi-signal filtering fields
    mood_aggressive: float
    genre_top: str
    genre_top_prob: float
    lastfm_tags: str


class ClassifyClient(BaseAzuraCastClient):
    """
    Extended AzuraCast client for classify operations.

    Inherits robust HTTP handling from BaseAzuraCastClient.
    """

    def get_playlists_map(self) -> dict[str, int]:
        """
        Get playlist name to ID mapping.

        Returns:
            Dictionary of playlist names to IDs.

        Raises:
            HTTPConnectionError: If AzuraCast is unreachable.
        """
        try:
            data = self.get_playlists()
            return {p["name"]: p["id"] for p in data}
        except (ClientError, ServerError, HTTPConnectionError) as e:
            logger.error("Failed to fetch playlists: %s", e)
            raise

    def get_existing_paths(self) -> set[str]:
        """
        Get set of existing file paths.

        Returns:
            Set of lowercase file paths.

        Raises:
            HTTPConnectionError: If AzuraCast is unreachable.
        """
        try:
            data = self.get_station_files()
            return {f["path"].lower() for f in data}
        except (ClientError, ServerError, HTTPConnectionError) as e:
            logger.error("Failed to fetch existing files: %s", e)
            raise

    def get_all_files(self) -> list[dict[str, Any]]:
        """
        Get all files with metadata.

        Returns:
            List of file dictionaries with id, path, mtime.

        Raises:
            HTTPConnectionError: If AzuraCast is unreachable.
        """
        try:
            return self.get_station_files()
        except (ClientError, ServerError, HTTPConnectionError) as e:
            logger.error("Failed to fetch files: %s", e)
            raise

    def upload_file(self, filepath: Path) -> int | None:
        """
        Upload file to AzuraCast with retry logic and integrity verification.

        Best practices 2026:
        - Computes hash before upload
        - Verifies upload integrity via size/hash comparison
        - Logs audit trail

        Args:
            filepath: Path to file.

        Returns:
            File ID or None on failure.
        """
        filename = filepath.name

        # Compute hashes before upload for integrity verification
        local_md5, local_sha256 = compute_file_hashes(filepath)
        local_size = filepath.stat().st_size
        logger.debug("  Pre-upload: size=%s, MD5=%s...", local_size, local_md5[:8])

        try:
            with open(filepath, "rb") as f:
                response = self.post(
                    f"/api/station/{self.station_id}/files/upload",
                    files={"file": (filename, f, "audio/mpeg")},
                    timeout=UPLOAD_TIMEOUT,
                )

            if response.status_code not in [200, 201]:
                logger.warning("  Upload failed: HTTP %s", response.status_code)
                return None

            logger.info("  Uploaded")

            # AzuraCast returns the created media object — use its id directly
            # instead of guessing which library file we just uploaded.
            uploaded_file: dict[str, Any] | None = None
            try:
                payload = response.json()
                if isinstance(payload, dict) and payload.get("id"):
                    uploaded_file = payload
            except ValueError:
                pass

            if not uploaded_file:
                # Fallback for AzuraCast versions with an empty upload response:
                # exact basename match against the library, never "most recent".
                time.sleep(1)
                try:
                    data = self.get_station_files()
                except (ClientError, ServerError, HTTPConnectionError):
                    return None
                fname_lower = filename.lower()
                matches = [
                    f for f in data
                    if f.get("path", "").lower().rsplit("/", 1)[-1] == fname_lower
                ]
                if len(matches) == 1:
                    uploaded_file = matches[0]

            if not uploaded_file:
                logger.error("  Upload succeeded but file could not be identified — "
                             "skipping integrity check and playlist assignment")
                return None

            # Verify upload integrity (best practice 2026)
            remote_size = uploaded_file.get("size")
            if remote_size and int(remote_size) != local_size:
                logger.error("  INTEGRITY FAILED: size mismatch (local=%s, remote=%s)", local_size, remote_size)
                # Delete corrupted upload
                self.delete_file(uploaded_file["id"])
                return None

            # Check unique_id (often MD5 in AzuraCast)
            unique_id = uploaded_file.get("unique_id", "")
            if unique_id and len(unique_id) == 32:
                if unique_id.lower() != local_md5.lower():
                    logger.error("  INTEGRITY FAILED: MD5 mismatch")
                    self.delete_file(uploaded_file["id"])
                    return None

            logger.info("  Integrity OK (size=%s)", local_size)
            return uploaded_file["id"]

        except (ClientError, ServerError, HTTPConnectionError) as e:
            logger.warning("  Upload error: %s", e)
            return None
        except OSError as e:
            logger.warning("  File read error: %s", e)
            return None

    def assign_playlists(self, file_id: int, playlist_ids: list[int]) -> bool:
        """
        Assign file to multiple playlists.

        Args:
            file_id: File ID.
            playlist_ids: List of playlist IDs.

        Returns:
            True if successful.
        """
        try:
            response = self.put(
                f"/api/station/{self.station_id}/file/{file_id}",
                json={"playlists": playlist_ids},
            )
            return response.status_code == 200
        except (ClientError, ServerError, HTTPConnectionError) as e:
            logger.warning("Failed to assign playlists: %s", e)
            return False


def get_features_from_tags(filepath: str) -> TrackFeatures | None:
    """
    Extract mood and features from ID3 tags (v2.0 circumplex model).

    Args:
        filepath: Path to MP3 file.

    Returns:
        Track features or None on error.
    """
    try:
        from mutagen import MutagenError

        tags = ID3(filepath)

        # Get artist/title
        artist = str(tags.get('TPE1', ['Unknown'])[0])
        title = str(tags.get('TIT2', ['Unknown'])[0])

        # Get BPM
        bpm = 0
        if "TBPM" in tags:
            try:
                bpm = int(float(str(tags["TBPM"])))
            except (ValueError, TypeError):
                pass

        # Initialize default values
        mood: str | None = None
        mood_confidence: float = 0.0
        energy_level: str | None = None
        duration = 0
        valence = 0.0
        arousal = 0.0
        has_lastfm_tags = False
        mood_aggressive = 0.0
        genre_top = ""
        genre_top_prob = 0.0
        lastfm_tags = ""

        # Read TXXX frames (custom tags)
        for frame in tags.getall("TXXX"):
            try:
                desc = frame.desc.upper()
                value = frame.text[0]

                if desc == "MOOD":
                    mood = value
                elif desc == "MOOD_CONFIDENCE":
                    mood_confidence = float(value)
                elif desc == "ENERGY_LEVEL":
                    energy_level = value
                elif desc == "DURATION":
                    duration = int(value)
                elif desc == "VALENCE":
                    valence = float(value)
                elif desc == "AROUSAL":
                    arousal = float(value)
                elif desc == "HAS_LASTFM_TAGS":
                    has_lastfm_tags = value.lower() == "true"
                elif desc == "MOOD_AGGRESSIVE":
                    mood_aggressive = float(value)
                elif desc == "GENRE_TOP":
                    genre_top = value
                elif desc == "GENRE_TOP_PROB":
                    genre_top_prob = float(value)
                elif desc == "LASTFM_TAGS":
                    lastfm_tags = value
                    # If we have actual lastfm tags string, set the boolean too
                    if value.strip():
                        has_lastfm_tags = True

            except (ValueError, IndexError):
                continue

        return {
            "artist": artist,
            "title": title,
            "bpm": bpm,
            "mood": mood,
            "mood_confidence": mood_confidence,
            "energy_level": energy_level,
            "duration": duration,
            "valence": valence,
            "arousal": arousal,
            "has_lastfm_tags": has_lastfm_tags,
            "mood_aggressive": mood_aggressive,
            "genre_top": genre_top,
            "genre_top_prob": genre_top_prob,
            "lastfm_tags": lastfm_tags,
        }
    except (MutagenError, OSError) as e:
        logger.debug("Failed to read tags: %s", e)
        return None


def should_reject_multisignal(features: TrackFeatures) -> tuple[bool, str]:
    """
    Multi-signal rejection for new tracks with discogs-effnet analysis.

    Uses 4 independent signals:
    1. mood_aggressive > threshold (discogs-effnet, 98% accuracy)
    2. High arousal + negative valence (MusiCNN AV ensemble)
    3. Genre in blocked list (genre_discogs400)
    4. Last.fm tags matching blocked tags

    Rules:
    - mood_aggressive > solo_threshold → reject alone
    - 2+ signals concordant → reject

    Args:
        features: Track features from ID3 tags.

    Returns:
        Tuple of (should_reject, reason).
    """
    config = MULTI_SIGNAL_FILTER
    if not config.enabled:
        return False, ""

    signals = []

    # Signal 1: mood_aggressive > threshold
    if features.get("mood_aggressive", 0) > config.aggressive_threshold:
        signals.append("aggressive_ml")

    # Signal 2: AV high-arousal negative-valence
    if (features.get("arousal", 0) > config.av_arousal_threshold
            and features.get("valence", 0) < config.av_valence_threshold):
        signals.append("arousal_valence")

    # Signal 3: genre blocked
    # Genre labels from discogs400 use "Parent---Subgenre" format
    raw_genre = features.get("genre_top", "").lower()
    genre = raw_genre.split("---")[-1] if "---" in raw_genre else raw_genre
    if genre in config.genre_blocked:
        signals.append("genre_blocked")

    # Signal 4: lastfm tags
    tags = {t.strip().lower() for t in features.get("lastfm_tags", "").split(",") if t.strip()}
    if tags & config.lastfm_blocked_tags:
        signals.append("lastfm_tags")

    # Solo override: very high aggressive confidence
    if features.get("mood_aggressive", 0) > config.aggressive_solo_threshold:
        return True, "aggressive_solo (%.2f)" % features["mood_aggressive"]

    # Consensus: 2+ signals
    if len(signals) >= config.min_signals_to_reject:
        return True, "%d signals: %s" % (len(signals), ", ".join(signals))

    return False, ""


# ---------------------------------------------------------------------------
# Personal taste filter (CLAP profile of Victor's own library).
# Profile is loaded once per run; every failure path degrades to "skip"
# so a missing/corrupt profile can never block the pipeline.
# ---------------------------------------------------------------------------

_TASTE_CACHE: dict[str, Any] = {}


def _get_taste_profile() -> Any | None:
    if "profile" not in _TASTE_CACHE:
        profile = None
        try:
            from taste_profile import load_taste_profile
            profile = load_taste_profile(Path(__file__).parent.parent / "data")
        except Exception as e:
            logger.warning("Taste profile unavailable: %s", e)
        if profile is not None and profile.size < TASTE_FILTER.min_profile_size:
            logger.warning(
                "Taste profile too small (%d < %d) — filter disabled",
                profile.size, TASTE_FILTER.min_profile_size,
            )
            profile = None
        _TASTE_CACHE["profile"] = profile
    return _TASTE_CACHE["profile"]


def check_taste(track_key: str) -> tuple[str, float | None]:
    """
    Score a track against the personal taste profile.

    Returns (verdict, score):
      verdict "reject" — score below threshold (caller decides whether
                          to enforce, per TASTE_FILTER.log_only)
      verdict "ok"     — score at/above threshold
      verdict "skip"   — filter disabled, profile missing, or no
                          embedding for this track (never blocks)
    """
    if not TASTE_FILTER.enabled:
        return "skip", None
    profile = _get_taste_profile()
    if profile is None:
        return "skip", None
    try:
        from audio_embeddings import EmbeddingStore
        store = _TASTE_CACHE.setdefault(
            "store", EmbeddingStore(Path(__file__).parent.parent / "data"))
        if not store.has(track_key):
            logger.info("  [taste] no embedding for %s — skipping check", track_key)
            return "skip", None
        embedding = store.get(track_key)
        score = profile.score(embedding, k=TASTE_FILTER.k)
    except Exception as e:
        logger.warning("  [taste] scoring failed: %s", e)
        return "skip", None
    verdict = "ok" if score >= TASTE_FILTER.threshold else "reject"
    return verdict, score


def process_track(
    filepath: Path,
    client: ClassifyClient,
    playlists: dict[str, int],
    existing: set[str],
    track_db: TrackDB | None = None,
) -> tuple[str, list[str]]:
    """
    Process and upload a single track.

    Assigns tracks to playlists based on mood and day-of-week compatibility.
    A track may be assigned to multiple daypart×day combinations.

    Args:
        filepath: Path to track.
        client: AzuraCast client.
        playlists: Available playlists (daypart name -> ID).
        existing: Set of existing file paths.
        track_db: Optional persistent track database.

    Returns:
        Tuple of (status, playlists_assigned).
        Status: "uploaded", "rejected", "skipped", or "failed".
    """
    filename = filepath.name
    logger.info("\n%s", filename)

    # Check duplicate — exact basename match only. Bidirectional substring
    # matching used to let short names match unrelated paths, deleting
    # never-uploaded tracks.
    name_lower = filename.lower()
    candidates = {name_lower, name_lower.replace(" ", "_")}
    if any(e.rsplit("/", 1)[-1] in candidates for e in existing):
        logger.info("  Skipped: already exists")
        filepath.unlink()
        return "skipped", []

    # Get features from tags
    features = get_features_from_tags(str(filepath))
    if not features or not features["mood"]:
        logger.warning("  Failed: no mood tag")
        filepath.unlink()  # Clean up
        return "failed", []

    mood = features["mood"]
    artist = features["artist"]
    title = features["title"]
    bpm = features["bpm"]
    duration = features["duration"]
    confidence = features["mood_confidence"]
    energy_level = features["energy_level"]
    valence = features["valence"]
    arousal = features["arousal"]

    # Display track info
    duration_str = format_duration(duration) if duration > 0 else "?"
    confidence_str = f"{confidence:.0%}" if confidence > 0 else "?"
    logger.info("  %s - %s", artist, title)
    logger.info("  BPM: %s | Mood: %s (%s) | Duration: %s", bpm, mood, confidence_str, duration_str)
    if energy_level:
        logger.info("  Energy: %s | V/A: %+.2f/%+.2f", energy_level, valence, arousal)

    # Build features dict for rejection check
    reject_features = {
        "mood": mood,
        "bpm": bpm,
        "duration": duration,
        "confidence": confidence,
    }

    # Check if track should be rejected (using config rules)
    reject, reason = should_reject_track(reject_features)
    if reject:
        logger.info("  Rejected: %s", reason)
        filepath.unlink()
        return "rejected", []

    # Multi-signal filter: consensus of ML signals (mood_aggressive,
    # V/A, genre ML, Last.fm tags) against aggressive tracks. Tracks
    # without any of these signals fall through to the taste filter.
    has_multisignal = features.get("mood_aggressive", 0) > 0 or features.get("genre_top", "")

    if has_multisignal:
        reject_ms, reason_ms = should_reject_multisignal(features)
        if reject_ms:
            logger.info("  Rejected (multi-signal): %s", reason_ms)
            filepath.unlink()
            return "rejected", []

    # Personal taste filter — the candidate must sound like Victor's
    # library. In log_only mode the verdict is logged but not enforced.
    taste_verdict, taste_score = check_taste(normalize_track_key(artist, title))
    if taste_verdict != "skip" and taste_score is not None:
        logger.info("  [taste] score=%.3f threshold=%.3f verdict=%s%s",
                    taste_score, TASTE_FILTER.threshold, taste_verdict,
                    " (log-only)" if TASTE_FILTER.log_only else "")
        if taste_verdict == "reject" and not TASTE_FILTER.log_only:
            logger.info("  Rejected: trop éloigné du profil de goût (%.3f < %.3f)",
                        taste_score, TASTE_FILTER.threshold)
            filepath.unlink()
            return "rejected", []

    # Assign to daypart playlists. NEW tracks land in HEAVY tier because
    # they're in the grace period (age=0) — full exposure so listeners can
    # actually discover them. They'll be re-tiered after grace_period_days
    # by enforce_tiered_rotation's re-tier pass.
    initial_tier = compute_rotation_tier(play_count=0, age_days=0.0)  # → HEAVY
    mood_dayparts = get_dayparts_for_mood(mood)
    tier_dayparts = tier_filter_dayparts(mood_dayparts, initial_tier)
    assigned_playlists: list[str] = []
    for segment in tier_dayparts:
        playlist_name = segment.value
        if playlist_name in playlists and playlist_name not in assigned_playlists:
            assigned_playlists.append(playlist_name)

    if not assigned_playlists:
        logger.warning("  Failed: no playlists found for mood '%s'", mood)
        filepath.unlink()  # Clean up
        return "failed", []

    # Upload
    file_id = client.upload_file(filepath)
    if not file_id:
        # Keep the local file: it will be retried on the next run
        # (AzuraCast outages must not permanently lose the batch).
        logger.warning("  Upload failed — keeping local file for retry")
        return "failed", []

    # Record in persistent TrackDB as soon as the file exists server-side,
    # so the DB stays consistent even if playlist assignment fails below.
    if track_db:
        track_key = normalize_track_key(artist, title)
        track_db.record_upload(
            track_key, artist, title, file_id, mood, tier=initial_tier,
        )

    # Assign to all applicable playlists
    playlist_ids = [playlists[name] for name in assigned_playlists]
    if client.assign_playlists(file_id, playlist_ids):
        # Show summary of assignments
        if len(assigned_playlists) <= 5:
            logger.info("  → Assigned to: %s", ", ".join(assigned_playlists))
        else:
            logger.info("  → Assigned to %s playlists", len(assigned_playlists))

        filepath.unlink()
        return "uploaded", assigned_playlists
    else:
        # File is on the server and in the DB; the re-tier pass reassigns
        # playlists on the next run, so don't keep the local copy.
        logger.warning("  Playlist assignment failed (uploaded as id=%s, "
                       "will be reassigned by next rotation pass)", file_id)
        filepath.unlink()
        return "failed", []


def enforce_tiered_rotation(client: ClassifyClient, track_db: TrackDB, new_tracks_count: int) -> int:
    """
    Enforce 3-tier track rotation for discovery webradio.

    Tiers:
      FRESH (0-fresh_days)         : protection totale
      CURRENT (fresh-current_days) : supprimé si library pleine + plays >= seuil
      FADING (current-max_age_days): plafonné à fading_max_pct% de la library
      EXPIRED (>max_age_days)      : force delete

    Args:
        client: AzuraCast client.
        track_db: Persistent track database.
        new_tracks_count: Number of new tracks to be added.

    Returns:
        Number of tracks deleted.
    """
    max_tracks = ROTATION.max_tracks
    fresh_days = ROTATION.fresh_days
    current_days = ROTATION.current_days
    max_age_days = ROTATION.max_age_days
    fading_max_pct = ROTATION.fading_max_pct
    min_plays = ROTATION.min_plays_before_delete

    # --- Phase 1: Sync play counts ---
    last_sync = track_db.get_last_sync_timestamp()
    history = client.get_history_since(last_sync)  # returns [] on API error
    if history:
        track_db.sync_play_counts(history)

    # --- Phase 2: Classify tracks into tiers ---
    files = client.get_all_files()
    current_count = len(files)
    now = time.time()

    logger.info("\n=== Tiered Rotation Check ===")
    logger.info("Library: %s tracks | New to add: %s | Max: %s", current_count, new_tracks_count, max_tracks)
    logger.info("Tiers: FRESH<%sd | CURRENT<%sd | FADING<%sd | EXPIRED", fresh_days, current_days, max_age_days)

    tiers: dict[str, list[dict[str, Any]]] = {
        "FRESH": [], "CURRENT": [], "FADING": [], "EXPIRED": [],
    }

    for f in files:
        file_id = f.get("id")
        artist = f.get("artist", "") or ""
        title = f.get("title", "") or ""

        # Look up in TrackDB by file_id for accurate uploaded_at and play_count
        db_track = track_db.get_track_by_file_id(file_id) if file_id else None

        if db_track:
            uploaded_at = db_track["uploaded_at"]
            play_count = db_track["play_count"]
            track_key = db_track["track_key"]
            mood = db_track.get("mood")
            tier_stored = db_track.get("tier") or "DISCOVERY"
        else:
            # Auto-register untracked files using AzuraCast's uploaded_at
            uploaded_at = f.get("uploaded_at") or f.get("mtime") or now
            play_count = 0
            track_key = normalize_track_key(artist, title) if artist and title else ""
            mood = None
            tier_stored = "DISCOVERY"
            # Persist into TrackDB so future runs have accurate data
            if track_key and file_id:
                track_db.register_untracked_file(track_key, artist, title, uploaded_at, file_id)

        age_days = (now - uploaded_at) / 86400

        entry = {
            "file_id": file_id,
            "artist": artist,
            "title": title,
            "track_key": track_key,
            "age_days": age_days,
            "play_count": play_count,
            "uploaded_at": uploaded_at,
            "mood": mood,
            "tier_stored": tier_stored,
            # Set of playlist names this file is currently assigned to in AzuraCast
            "playlist_names": {p.get("name") for p in (f.get("playlists") or []) if p.get("name")},
        }

        if age_days <= fresh_days:
            tiers["FRESH"].append(entry)
        elif age_days <= current_days:
            tiers["CURRENT"].append(entry)
        elif age_days <= max_age_days:
            tiers["FADING"].append(entry)
        else:
            tiers["EXPIRED"].append(entry)

    for tier_name, tracks in tiers.items():
        logger.info("  %s: %s tracks", tier_name, len(tracks))

    # --- Monitor: warn about active tracks with 0 plays after 7+ days ---
    zero_play_old = [
        e for e in (tiers["CURRENT"] + tiers["FADING"])
        if e["play_count"] == 0 and e["age_days"] > 7
    ]
    if zero_play_old:
        logger.warning("  ⚠ %s tracks actives > 7j avec 0 plays (possible problème AzuraCast scheduling):", len(zero_play_old))
        for e in zero_play_old[:10]:
            logger.warning("    - %s - %s (%.0fj)", e["artist"], e["title"], e["age_days"])
        if len(zero_play_old) > 10:
            logger.warning("    ... et %s autres", len(zero_play_old) - 10)

    deleted_count = 0

    # --- Phase 3: Force delete EXPIRED (>max_age_days) ---
    for entry in tiers["EXPIRED"]:
        if client.delete_file(entry["file_id"]):
            logger.info("  EXPIRED: %s - %s (%.0fd, %s plays)", entry['artist'], entry['title'], entry['age_days'], entry['play_count'])
            if entry["track_key"]:
                track_db.record_deletion(entry["track_key"])
            deleted_count += 1

    # --- Phase 4: Cap FADING to fading_max_pct% of library ---
    fading_max_count = int(max_tracks * fading_max_pct / 100)
    fading_excess = len(tiers["FADING"]) - fading_max_count

    if fading_excess > 0:
        # Sort by play_count ascending: least-played leave first (failed to get traction)
        fading_sorted = sorted(tiers["FADING"], key=lambda x: x["play_count"], reverse=False)
        for entry in fading_sorted[:fading_excess]:
            if client.delete_file(entry["file_id"]):
                logger.info("  FADING cap: %s - %s (%.0fd, %s plays)", entry['artist'], entry['title'], entry['age_days'], entry['play_count'])
                if entry["track_key"]:
                    track_db.record_deletion(entry["track_key"])
                deleted_count += 1

    # --- Phase 5: Cap total if library + new > max_tracks ---
    remaining = current_count - deleted_count + new_tracks_count
    overflow = max(0, remaining - max_tracks)

    if overflow > 0:
        # Candidates: CURRENT with plays >= min_plays, oldest first
        candidates = sorted(
            [t for t in tiers["CURRENT"] if t["play_count"] >= min_plays],
            key=lambda x: x["uploaded_at"],
        )
        for entry in candidates:
            if overflow <= 0:
                break
            if client.delete_file(entry["file_id"]):
                logger.info("  CURRENT overflow: %s - %s (%.0fd, %s plays)", entry['artist'], entry['title'], entry['age_days'], entry['play_count'])
                if entry["track_key"]:
                    track_db.record_deletion(entry["track_key"])
                deleted_count += 1
                overflow -= 1

    # --- Phase 6: Re-tier pass (HEAVY ↔ MEDIUM ↔ LIGHT) ---
    # Two-way: tracks moving up gain playlists, tracks moving down lose them.
    # Demotion is NOT silent: assign_playlists() is REPLACE semantics, so a
    # demoted track is actually removed from the dayparts it no longer
    # qualifies for. This is what makes "discovery" emergent: anchors stay
    # heavy, average performers stay average, fading tracks fade out.
    #
    # We skip FRESH tracks: their tier is whatever compute_rotation_tier
    # says (HEAVY during grace period), and they already got assigned to
    # the right playlists at upload time. Including them in the re-tier
    # would be a no-op + waste API calls.
    retier_changed = 0
    retier_skipped_no_mood = 0
    if ROTATION_CATEGORIES.enabled:
        try:
            playlist_id_map = client.get_playlists_map()
        except (ClientError, ServerError, HTTPConnectionError) as e:
            logger.error("Re-tier pass SKIPPED — cannot fetch playlist map: %s", e)
            playlist_id_map = {}

        if playlist_id_map:
            # Re-tier post-grace tracks only (CURRENT + FADING). FRESH stay
            # at their upload-time HEAVY assignment.
            for entry in (tiers["CURRENT"] + tiers["FADING"]):
                mood = entry["mood"]
                if not mood:
                    retier_skipped_no_mood += 1
                    continue
                stored = entry["tier_stored"]
                # Treat the legacy "DISCOVERY" label as unset (TIER_RANK 0)
                new_tier = compute_rotation_tier(entry["play_count"], entry["age_days"])

                target_dayparts = tier_filter_dayparts(get_dayparts_for_mood(mood), new_tier)
                target_names = {dp.value for dp in target_dayparts}
                current_names = entry["playlist_names"]

                # Only push the membership update if the set actually differs.
                # Persist the tier label in DB on every iteration (cheap).
                if entry["track_key"]:
                    track_db.update_tier(entry["track_key"], new_tier)

                if target_names == current_names:
                    continue

                target_ids = [playlist_id_map[n] for n in target_names if n in playlist_id_map]
                if not target_ids:
                    logger.warning(
                        "  Re-tier %s skipped (no playlist IDs for target dayparts): %s - %s",
                        new_tier, entry["artist"], entry["title"],
                    )
                    continue

                if client.assign_playlists(entry["file_id"], target_ids):
                    retier_changed += 1
                    added = target_names - current_names
                    removed = current_names - target_names
                    bits = []
                    if added:
                        bits.append("+" + ",".join(sorted(added)))
                    if removed:
                        bits.append("-" + ",".join(sorted(removed)))
                    logger.info(
                        "  RE-TIER %s→%s: %s - %s [%s]",
                        stored, new_tier, entry["artist"], entry["title"],
                        " ".join(bits) if bits else "no playlist diff",
                    )
        if retier_skipped_no_mood:
            logger.warning(
                "Re-tier: %d tracks skipped (no mood in DB — possibly pre-MTG legacy uploads)",
                retier_skipped_no_mood,
            )

    # --- Phase 7: Summary ---
    logger.info("\n=== Rotation Summary ===")
    logger.info("Deleted: %s", deleted_count)
    if ROTATION_CATEGORIES.enabled:
        logger.info("Re-tier changes (playlists updated): %s", retier_changed)
    logger.info("Library after rotation: %s (+ %s new)", current_count - deleted_count, new_tracks_count)

    stats = track_db.get_stats()
    logger.info("TrackDB stats: %s", stats)

    # Prune CLAP embeddings of deleted tracks — without this the store
    # grows forever while rotation shrinks the library. Runs even when
    # this rotation pass deleted nothing: tracks can disappear outside
    # rotation (manual purge, server-side removal) and their embeddings
    # must not linger.
    try:
        from audio_embeddings import EmbeddingStore

        valid_keys = {t["track_key"] for t in track_db.get_active_tracks()}
        removed = EmbeddingStore(Path(__file__).parent.parent / "data").prune(valid_keys)
        if removed:
            logger.info("Embedding store: pruned %d stale entries", removed)
    except ImportError:
        pass  # CLAP deps not installed — nothing to prune

    return deleted_count


def main() -> int:
    """
    Main entry point.

    Rotation always runs (even with 0 new files) to enforce age-based tiers.

    Returns:
        Exit code (0 for success, 1 for failure).
    """
    logger.info("=== Upload to AzuraCast (v2.2 - Tiered Rotation) ===")

    # Validate configuration
    is_valid, errors = validate_environment()
    if not is_valid:
        for error in errors:
            logger.error("Config error: %s", error)
        return 1

    settings = get_settings()

    # Get music files (from downloads/ where analyze.py processes them)
    music_dir = Path(__file__).parent.parent / "downloads"
    files = list(music_dir.glob("*.mp3")) if music_dir.exists() else []

    # Initialize persistent TrackDB (closed in finally block)
    db_path = Path(__file__).parent.parent / "data" / "tracks.db"
    track_db = TrackDB(db_path)

    try:
        return _main_inner(settings, files, music_dir, track_db)
    finally:
        track_db.close()


def _main_inner(
    settings: Any,
    files: list[Path],
    music_dir: Path,
    track_db: TrackDB,
) -> int:
    """Inner main logic (TrackDB is guaranteed to be closed by caller)."""
    # Show current day type
    current_day = get_current_day_type()
    day_labels = {
        DayType.WEEKDAY: "Lundi-Jeudi (Semaine)",
        DayType.FRIDAY: "Vendredi",
        DayType.SATURDAY: "Samedi",
        DayType.SUNDAY: "Dimanche",
    }
    logger.info("Aujourd'hui: %s", day_labels[current_day])

    # Show all possible playlists (daypart × day)
    all_playlist_names = get_all_playlist_names()

    logger.info("Files: %s", len(files))
    logger.info("Server: %s", settings.azuracast_url)
    logger.info("Total playlists possible: %s", len(all_playlist_names))

    # Show audio filters
    if AUDIO_FILTERS.duration_min or AUDIO_FILTERS.duration_max:
        filter_parts = []
        if AUDIO_FILTERS.duration_min:
            filter_parts.append(f"min {format_duration(AUDIO_FILTERS.duration_min)}")
        if AUDIO_FILTERS.duration_max:
            filter_parts.append(f"max {format_duration(AUDIO_FILTERS.duration_max)}")
        logger.info("Duration filter: %s", ", ".join(filter_parts))

    # Initialize robust client with retry logic
    client = ClassifyClient(
        base_url=settings.azuracast_url,
        api_key=settings.azuracast_api_key,
        station_id=settings.azuracast_station_id,
        timeout=settings.http_timeout,
    )

    # Health check before proceeding
    if not client.health_check():
        logger.error("AzuraCast is not reachable. Aborting.")
        return 1

    # Get playlists (with retry logic)
    try:
        playlists = client.get_playlists_map()
    except (ClientError, ServerError, HTTPConnectionError) as e:
        logger.error("Cannot connect to AzuraCast: %s", e)
        return 1

    if not playlists:
        logger.error("Error: Could not fetch playlists")
        return 1
    logger.info("Available playlists (%s): %s", len(playlists), ", ".join(playlists.keys()))

    # Check which playlists exist vs expected
    existing_expected = [p for p in all_playlist_names if p in playlists]
    missing = [p for p in all_playlist_names if p not in playlists]

    logger.info("Found playlists: %s/%s", len(existing_expected), len(all_playlist_names))
    if missing:
        logger.warning("Missing playlists: %s", len(missing))
        logger.warning("Run setup_playlists.py to create missing playlists")

    # Get existing files (with retry logic)
    try:
        existing = client.get_existing_paths()
    except (ClientError, ServerError, HTTPConnectionError) as e:
        logger.error("Cannot fetch existing files: %s", e)
        logger.error("Aborting to prevent duplicates.")
        return 1

    logger.info("Existing files: %s", len(existing))

    # Enforce tiered rotation ALWAYS (even with 0 new files)
    try:
        enforce_tiered_rotation(client, track_db, len(files))
    except (ClientError, ServerError, HTTPConnectionError) as e:
        logger.warning("Rotation check failed: %s", e)
        # Continue anyway - rotation is not critical

    # Refresh existing paths after rotation
    try:
        existing = client.get_existing_paths()
    except (ClientError, ServerError, HTTPConnectionError):
        pass  # Use previous set if refresh fails

    # If no new files, we're done (rotation already ran)
    if not files:
        logger.info("No MP3 files to process")
        return 0

    # Initialize stats
    results: dict[str, int] = {
        "uploaded": 0,
        "rejected": 0,
        "skipped": 0,
        "failed": 0,
    }
    playlist_counts: dict[str, int] = {}

    # Process files
    for filepath in files:
        status, assigned_playlists = process_track(
            filepath, client, playlists, existing, track_db
        )
        results[status] += 1

        # Count assignments per playlist
        for pl in assigned_playlists:
            playlist_counts[pl] = playlist_counts.get(pl, 0) + 1

    # Print results
    logger.info("\n=== Results ===")
    logger.info("  Uploaded: %s", results['uploaded'])
    logger.info("  Rejected: %s", results['rejected'])
    logger.info("  Skipped: %s", results['skipped'])
    logger.info("  Failed: %s", results['failed'])

    if results['uploaded'] > 0 and playlist_counts:
        logger.info("\n=== Playlist Distribution ===")
        for name, count in sorted(playlist_counts.items()):
            logger.info("  %s: %s", name, count)

    # Write actual upload count for run.sh stats
    upload_count_file = Path(__file__).parent.parent / "data" / "last_upload_count.txt"
    upload_count_file.parent.mkdir(parents=True, exist_ok=True)
    upload_count_file.write_text(str(results['uploaded']), encoding="utf-8")

    # Cleanup: clear tracks-to-download.json only on a fully successful pass.
    # On failures the local MP3s are kept and retried next run; clearing the
    # batch here would have made an AzuraCast outage permanently lose it.
    tracks_file = Path(__file__).parent.parent / "tracks-to-download.json"
    if tracks_file.exists():
        if results["failed"] == 0:
            tracks_file.write_text("[]", encoding="utf-8")
            logger.info("\nCleanup: tracks-to-download.json cleared")
        else:
            logger.info("\nCleanup: keeping tracks-to-download.json (%d failures)",
                        results["failed"])

    # Cleanup: remove downloads directory if empty
    if music_dir.exists() and not any(music_dir.iterdir()):
        music_dir.rmdir()
        logger.info("Cleanup: downloads/ directory removed")

    return 0


if __name__ == "__main__":
    sys.exit(main())
