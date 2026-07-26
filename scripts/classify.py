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

import json
import logging
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any, TypedDict

from mutagen.id3 import ID3

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from http_client import AzuraCastClient as BaseAzuraCastClient, ClientError, HTTPConnectionError, ServerError, compute_file_hashes
from settings import get_settings, validate_environment
from track_db import TrackDB, normalize_track_key
from library_state import ReconcileReport, reconcile

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
        playlist_name_for_tier,
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


def compute_rotation_tier(
    play_count: int, age_days: float, expected: float | None = None,
) -> str:
    """
    Compute the rotation tier for a track from its (play_count, age_days).

    Two cases:
    - In the grace period (age < grace_period_days) → always HEAVY
      regardless of plays. This is the DISCOVERY emphasis: new tracks
      get a fair shot at being heard.
    - Past the grace period → compare actual play rate against the
      library's expected rate (measured when available, config fallback):
        * rate >= expected × heavy_above_average_ratio  → HEAVY (proven hit)
        * rate >= expected × light_below_average_ratio  → MEDIUM (average)
        * else                                          → LIGHT (waning)
    """
    cfg = ROTATION_CATEGORIES
    if age_days < cfg.grace_period_days:
        return "HEAVY"
    rate = play_count / max(1.0, age_days)
    if expected is None:
        expected = cfg.expected_plays_per_day
    if rate >= expected * cfg.heavy_above_average_ratio:
        return "HEAVY"
    if rate >= expected * cfg.light_below_average_ratio:
        return "MEDIUM"
    return "LIGHT"


def measure_expected_plays_per_day(entries: list[dict[str, Any]]) -> float:
    """
    Measure the library-wide average plays/track/day from real data
    (Σ play_count / Σ age_days over tracks at least 1 day old), instead
    of trusting the hardcoded config constant — which silently drifts
    whenever library size or listening volume changes.

    Falls back to ROTATION_CATEGORIES.expected_plays_per_day when there
    is not enough signal (< 30 track-days). Clamped to [0.2, 3.0].
    """
    total_plays = 0
    total_days = 0.0
    for e in entries:
        age = e.get("age_days", 0.0)
        if age >= 1.0:
            total_plays += e.get("play_count", 0)
            total_days += age
    if total_days < 30.0:
        return ROTATION_CATEGORIES.expected_plays_per_day
    return min(3.0, max(0.2, total_plays / total_days))


def qualifies_for_gold(
    play_count: int,
    age_days: float,
    taste_score: float | None,
    expected_rate: float,
) -> bool:
    """
    GOLD graduation rule: at expiry, a track survives as permanent
    catalogue if it is PROVEN (play rate at least heavy_above_average_ratio
    times the library rate) and ON-COLOR (taste score at or above
    ROTATION.gold_min_taste). No taste score (no profile/embedding) means
    no graduation — the catalogue only takes verified matches.
    """
    if taste_score is None or taste_score < ROTATION.gold_min_taste:
        return False
    rate = play_count / max(1.0, age_days)
    return rate >= expected_rate * ROTATION_CATEGORIES.heavy_above_average_ratio


def tier_filter_dayparts(
    mood_dayparts: list[DaypartSegment], tier: str,
) -> list[DaypartSegment]:
    """
    Restrict the list of mood-compatible dayparts based on the rotation tier.

    Returns:
      HEAVY  → all matching dayparts (max exposure)
      MEDIUM → first medium_daypart_count matching dayparts
      LIGHT  → first light_daypart_count matching dayparts
      (GOLD catalogue and legacy DISCOVERY treated as LIGHT)
    """
    if not ROTATION_CATEGORIES.enabled or tier == "HEAVY":
        return list(mood_dayparts)
    if tier == "MEDIUM":
        return list(mood_dayparts)[: ROTATION_CATEGORIES.medium_daypart_count]
    # LIGHT (or legacy DISCOVERY)
    return list(mood_dayparts)[: ROTATION_CATEGORIES.light_daypart_count]


def target_playlist_names(mood: "MoodCategory | str", tier: str) -> list[str]:
    """
    Expected AzuraCast playlist names for a track, from its mood and
    rotation tier.

    Single source of truth shared by upload, re-tier, GOLD graduation,
    zero-play remediation and reanalysis: the tier picks how many dayparts
    (tier_filter_dayparts) and which weight variant (playlist_name_for_tier).
    """
    dayparts = tier_filter_dayparts(get_dayparts_for_mood(mood), tier)
    return [playlist_name_for_tier(dp, tier) for dp in dayparts]

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# Constants
UPLOAD_TIMEOUT = 180


def _normalize_filename(name: str) -> str:
    """
    Canonical form for comparing a local filename with a library path
    basename: AzuraCast rewrites names on upload (lowercase, spaces to
    underscores, possibly accent transliteration), so only the
    alphanumeric skeleton is stable across versions.
    """
    decomposed = unicodedata.normalize("NFKD", name.lower())
    return "".join(c for c in decomposed if c.isalnum() and c.isascii())


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

    def get_existing_track_keys(self) -> set[str]:
        """
        Get the set of track keys (normalized artist - title) already in
        the library.

        Keyed on metadata, not the file path: AzuraCast sanitizes stored
        filenames (lowercase, underscores) and preserves non-Latin scripts
        verbatim, so a path-based skeleton can both miss real duplicates
        and collide across distinct non-Latin titles. Artist/title survive
        upload untouched and are the same identity download.py dedups on.

        Returns:
            Set of normalized track keys (files without artist/title are
            skipped — they cannot be matched by identity anyway).

        Raises:
            HTTPConnectionError: If AzuraCast is unreachable.
        """
        try:
            data = self.get_station_files()
            keys = set()
            for f in data:
                artist, title = f.get("artist") or "", f.get("title") or ""
                if artist and title:
                    keys.add(normalize_track_key(artist, title))
            return keys
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
                # normalized basename match against the library, never "most
                # recent". Normalized because AzuraCast sanitizes filenames on
                # upload (e.g. "St. Vincent - Marry Me.mp3" is stored as
                # "st._vincent_-_marry_me.mp3" since the 2026-07 update), so an
                # exact comparison never matches; stripping everything but
                # alphanumerics survives any space/underscore/case policy.
                # Ambiguity (0 or >1 matches) still fails safe.
                time.sleep(1)
                try:
                    data = self.get_station_files()
                except (ClientError, ServerError, HTTPConnectionError):
                    return None
                wanted = _normalize_filename(filename)
                matches = [
                    f for f in data
                    if _normalize_filename(
                        f.get("path", "").rsplit("/", 1)[-1]) == wanted
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
                elif desc == "MOOD_AGGRESSIVE":
                    mood_aggressive = float(value)
                elif desc == "GENRE_TOP":
                    genre_top = value
                elif desc == "GENRE_TOP_PROB":
                    genre_top_prob = float(value)
                elif desc == "LASTFM_TAGS":
                    lastfm_tags = value

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


def check_taste(track_key: str) -> float | None:
    """
    Score a track against the personal taste profile.

    Returns the taste score, or None when the check cannot run (filter
    disabled, profile missing, no embedding) — None never blocks.
    Callers compare against TASTE_FILTER.threshold.
    """
    if not TASTE_FILTER.enabled:
        return None
    profile = _get_taste_profile()
    if profile is None:
        return None
    try:
        from audio_embeddings import EmbeddingStore
        store = _TASTE_CACHE.setdefault(
            "store", EmbeddingStore(Path(__file__).parent.parent / "data"))
        if not store.has(track_key):
            logger.info("  [taste] no embedding for %s — skipping check", track_key)
            return None
        embedding = store.get(track_key)
        return profile.score(embedding, k=TASTE_FILTER.k)
    except Exception as e:
        logger.warning("  [taste] scoring failed: %s", e)
        return None


def _track_key_of_file(filepath: Path) -> str | None:
    """Read artist/title from ID3 tags and build the pipeline track key."""
    try:
        from mutagen.id3 import ID3
        tags = ID3(str(filepath))
        artist = str(tags.get("TPE1", "") or "").strip()
        title = str(tags.get("TIT2", "") or "").strip()
    except Exception:
        return None
    if not artist or not title:
        return None
    return normalize_track_key(artist, title)


def _should_carry_over(
    filepath: Path, taste_score: float, already_carried: int,
) -> bool:
    """
    Gem safety net: a quota leftover stays on disk for tomorrow's ranking
    when it is on-color (taste >= production threshold), young enough
    (file mtime < carryover_max_days), and the carryover pool has room.
    Iteration is best-score-first, so the pool keeps the best leftovers.
    """
    if already_carried >= ROTATION.carryover_max_files:
        return False
    if taste_score < TASTE_FILTER.threshold:
        return False
    try:
        age_days = (time.time() - filepath.stat().st_mtime) / 86400
    except OSError:
        return False
    return age_days < ROTATION.carryover_max_days


def _rank_by_taste(files: list[Path]) -> list[tuple[Path, str | None, float]]:
    """
    Order the night's batch by taste score, best first, for the upload
    quota. Files without a score (no embedding/profile) rank last but
    are still processed if the quota allows.
    """
    ranked: list[tuple[Path, str | None, float]] = []
    for filepath in files:
        track_key = _track_key_of_file(filepath)
        score = -1.0
        if track_key:
            taste = check_taste(track_key)
            if taste is not None:
                score = taste
        ranked.append((filepath, track_key, score))
    ranked.sort(key=lambda t: t[2], reverse=True)
    return ranked


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
        existing: Set of track keys already in the library (normalized
            artist - title), from client.get_existing_track_keys().
        track_db: Optional persistent track database.

    Returns:
        Tuple of (status, playlists_assigned).
        Status: "uploaded", "rejected", "skipped", or "failed".
    """
    filename = filepath.name
    logger.info("\n%s", filename)

    # Check duplicate by identity (artist - title), not filename: it is the
    # signal the server preserves faithfully across its own sanitization,
    # and the same one download.py dedups on. A file with unreadable tags
    # (key is None) falls through to the mood check below, which rejects it.
    track_key = _track_key_of_file(filepath)
    if track_key and track_key in existing:
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
    taste_score = check_taste(normalize_track_key(artist, title))
    if taste_score is not None:
        on_color = taste_score >= TASTE_FILTER.threshold
        logger.info("  [taste] score=%.3f threshold=%.3f verdict=%s%s",
                    taste_score, TASTE_FILTER.threshold,
                    "ok" if on_color else "reject",
                    " (log-only)" if TASTE_FILTER.log_only else "")
        if not on_color and not TASTE_FILTER.log_only:
            logger.info("  Rejected: trop éloigné du profil de goût (%.3f < %.3f)",
                        taste_score, TASTE_FILTER.threshold)
            filepath.unlink()
            return "rejected", []

    # Assign to daypart playlists. NEW tracks land in HEAVY tier because
    # they're in the grace period (age=0) — full exposure so listeners can
    # actually discover them. They'll be re-tiered after grace_period_days
    # by enforce_tiered_rotation's re-tier pass.
    initial_tier = compute_rotation_tier(play_count=0, age_days=0.0)  # → HEAVY
    assigned_playlists = [
        name for name in dict.fromkeys(target_playlist_names(mood, initial_tier))
        if name in playlists
    ]

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


def _write_reconcile_report(report: ReconcileReport) -> None:
    """Persister le rapport de réconciliation pour le récap quotidien."""
    path = Path(__file__).parent.parent / "data" / "last_reconcile.json"
    payload = {
        "az_files": report.az_files,
        "db_active_before": report.db_active_before,
        "ghosts_cleared": report.ghosts_cleared,
        "untracked_registered": report.untracked_registered,
        "keys_repaired": report.keys_repaired,
        "disk_files": report.disk_files,
        "disk_drift": report.disk_drift,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError as e:
        logger.warning("Écriture de %s impossible : %s", path, e)


def enforce_tiered_rotation(
    client: ClassifyClient,
    track_db: TrackDB,
    new_tracks_count: int,
    pending_keys: set[str] | None = None,
) -> int:
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
        pending_keys: Track keys of tonight's batch (and carryovers) still
            on disk — not yet in track_db, but their fresh CLAP embeddings
            must survive the prune below.

    Returns:
        Number of tracks deleted.
    """
    max_tracks = ROTATION.max_tracks
    fresh_days = ROTATION.fresh_days
    current_days = ROTATION.current_days
    max_age_days = ROTATION.max_age_days
    fading_max_pct = ROTATION.fading_max_pct
    min_plays = ROTATION.min_plays_before_delete

    # --- Phase 0 : réconcilier avant toute lecture d'état ---
    # AzuraCast fait autorité. On retire les fantômes, on enregistre les
    # fichiers inconnus et on répare les clés ayant dérivé AVANT la synchro
    # des passages : sync_play_counts matche l'historique par clé, et une
    # clé encore fausse fait perdre ces passages définitivement (le curseur
    # last_history_sync avance qu'une ligne ait matché ou non).
    files = client.get_all_files()
    settings = get_settings()
    media_dir = Path(settings.azuracast_media_dir) if settings.azuracast_media_dir else None
    report = reconcile(files, track_db, media_dir=media_dir)
    _write_reconcile_report(report)

    # --- Phase 1: Sync play counts ---
    last_sync = track_db.get_last_sync_timestamp()
    history = client.get_history_since(last_sync)  # returns [] on API error
    if history:
        track_db.sync_play_counts(history)

    # --- Phase 2: Classify tracks into tiers ---
    current_count = len(files)
    now = time.time()

    logger.info("\n=== Tiered Rotation Check ===")
    logger.info("Library: %s tracks | New to add: %s | Max: %s", current_count, new_tracks_count, max_tracks)
    logger.info("Tiers: FRESH<%sd | CURRENT<%sd | FADING<%sd | EXPIRED", fresh_days, current_days, max_age_days)

    tiers: dict[str, list[dict[str, Any]]] = {
        "FRESH": [], "CURRENT": [], "FADING": [], "EXPIRED": [], "GOLD": [],
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
            # reconcile() vient d'enregistrer tout fichier inconnu : ne peut
            # rester ici que le cas des fichiers sans métadonnées, qu'on
            # ne peut pas cléer. Ils traversent la rotation en DISCOVERY.
            uploaded_at = f.get("uploaded_at") or f.get("mtime") or now
            play_count = 0
            track_key = normalize_track_key(artist, title) if artist and title else ""
            mood = None
            tier_stored = "DISCOVERY"

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

        if tier_stored == "GOLD":
            # Permanent catalogue: immune to age expiry and re-tiering.
            tiers["GOLD"].append(entry)
        elif age_days <= fresh_days:
            tiers["FRESH"].append(entry)
        elif age_days <= current_days:
            tiers["CURRENT"].append(entry)
        elif age_days <= max_age_days:
            tiers["FADING"].append(entry)
        else:
            tiers["EXPIRED"].append(entry)

    for tier_name, tracks in tiers.items():
        logger.info("  %s: %s tracks", tier_name, len(tracks))

    # Measured library play rate (replaces the hardcoded constant for
    # tiering + GOLD graduation; falls back to config on thin signal).
    all_entries = [e for bucket in tiers.values() for e in bucket]
    expected_rate = measure_expected_plays_per_day(all_entries)
    logger.info("  Expected plays/day: %.2f (measured; config fallback %.2f)",
                expected_rate, ROTATION_CATEGORIES.expected_plays_per_day)

    # Playlist name→id map, shared by the zero-play remediation below and
    # the GOLD graduation shrink. Non-fatal on failure: both consumers
    # degrade to their previous warn-only behaviour.
    playlist_map: dict[str, int] = {}
    try:
        playlist_map = client.get_playlists_map()
    except (ClientError, ServerError, HTTPConnectionError) as e:
        logger.warning("  Playlist map unavailable (remediation/GOLD shrink skipped): %s", e)

    # --- Monitor: active tracks with 0 plays after 7+ days ---
    # A track AutoDJ can never pick has two known causes: it lost its
    # playlist assignment (remediable here), or its play counts don't sync
    # (metadata key drift — fixed at the normalize_track_key level).
    zero_play_old = [
        e for e in (tiers["CURRENT"] + tiers["FADING"])
        if e["play_count"] == 0 and e["age_days"] > 7
    ]
    if zero_play_old:
        logger.warning("  ⚠ %s tracks actives > 7j avec 0 plays:", len(zero_play_old))
        for e in zero_play_old[:10]:
            logger.warning("    - %s - %s (%.0fj)", e["artist"], e["title"], e["age_days"])
        if len(zero_play_old) > 10:
            logger.warning("    ... et %s autres", len(zero_play_old) - 10)

        # Remediation: re-assert the expected assignment for tracks that
        # have no playlist at all — those can never be scheduled by AutoDJ.
        if playlist_map:
            for e in zero_play_old:
                if e["playlist_names"] or not e["mood"]:
                    continue
                ids = [
                    playlist_map[name]
                    for name in target_playlist_names(e["mood"], e["tier_stored"])
                    if name in playlist_map
                ]
                if ids and client.assign_playlists(e["file_id"], ids):
                    logger.info("    Réassigné (aucune playlist): %s - %s",
                                e["artist"], e["title"])

    deleted_count = 0

    # --- Phase 3: EXPIRED (>max_age_days) — graduate to GOLD or delete ---
    # Radio practice: proven, on-color tracks become permanent catalogue
    # instead of dying at max_age_days. Everything else is deleted.
    gold_cap = int(current_count * ROTATION.gold_max_pct / 100)
    gold_count = len(tiers["GOLD"])
    graduated = 0
    # Best candidates graduate first while the cap has room.
    expired_ranked = sorted(
        tiers["EXPIRED"], key=lambda e: e["play_count"], reverse=True)
    for entry in expired_ranked:
        taste_score = check_taste(entry["track_key"]) if entry["track_key"] else None
        if (
            gold_count < gold_cap
            and qualifies_for_gold(
                entry["play_count"], entry["age_days"], taste_score, expected_rate)
        ):
            track_db.update_tier(entry["track_key"], "GOLD")
            gold_count += 1
            graduated += 1
            logger.info(
                "  GOLD: %s - %s (%.0fd, %s plays, taste %.2f)",
                entry["artist"], entry["title"], entry["age_days"],
                entry["play_count"], taste_score,
            )
            # Soft rotation: shrink to LIGHT-style daypart membership.
            mood = entry["mood"]
            if mood and playlist_map:
                ids = [
                    playlist_map[n]
                    for n in target_playlist_names(mood, "GOLD")
                    if n in playlist_map
                ]
                if ids:
                    client.assign_playlists(entry["file_id"], ids)
            continue
        if client.delete_file(entry["file_id"]):
            logger.info("  EXPIRED: %s - %s (%.0fd, %s plays)", entry['artist'], entry['title'], entry['age_days'], entry['play_count'])
            if entry["track_key"]:
                track_db.record_deletion(entry["track_key"])
            deleted_count += 1
    if graduated:
        logger.info("  GOLD catalogue: %d graduated, %d/%d total", graduated, gold_count, gold_cap)

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
    # new_tracks_count is the *staged* file count, but at most
    # max_uploads_per_night of them are actually uploaded (quota curation);
    # capping on the staged count over-deleted on carryover-heavy nights,
    # leaving the library floating at 680-690 instead of max_tracks.
    expected_additions = min(new_tracks_count, ROTATION.max_uploads_per_night)
    remaining = current_count - deleted_count + expected_additions
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
                new_tier = compute_rotation_tier(
                    entry["play_count"], entry["age_days"], expected=expected_rate)

                target_names = set(target_playlist_names(mood, new_tier))
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
        valid_keys |= pending_keys or set()
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

    # Get existing track keys (with retry logic)
    try:
        existing = client.get_existing_track_keys()
    except (ClientError, ServerError, HTTPConnectionError) as e:
        logger.error("Cannot fetch existing files: %s", e)
        logger.error("Aborting to prevent duplicates.")
        return 1

    logger.info("Existing files: %s", len(existing))

    # Enforce tiered rotation ALWAYS (even with 0 new files)
    try:
        pending_keys = {k for k in (_track_key_of_file(f) for f in files) if k}
        rotation_deleted = enforce_tiered_rotation(
            client, track_db, len(files), pending_keys)
    except (ClientError, ServerError, HTTPConnectionError) as e:
        logger.warning("Rotation check failed: %s", e)
        # Continue anyway - rotation is not critical

    # Refresh existing track keys after rotation
    try:
        existing = client.get_existing_track_keys()
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
        "quota": 0,
    }
    playlist_counts: dict[str, int] = {}

    # Nightly curation: rank the batch by taste score (best first) so the
    # upload quota keeps the tracks closest to Victor's colour. Leftovers
    # that are still on-color stay on disk and re-compete tomorrow (gem
    # safety net); the rest goes to cooldown — airtime is the scarce
    # resource: beyond max_uploads_per_night adds, a discovery can no
    # longer get its 15-20 weekly heavy-rotation plays.
    quota = ROTATION.max_uploads_per_night
    ranked = _rank_by_taste(files)
    results["carryover"] = 0

    # Process files (best-scoring first)
    for filepath, track_key, taste_score in ranked:
        if quota and results["uploaded"] >= quota:
            if _should_carry_over(filepath, taste_score, results["carryover"]):
                results["carryover"] += 1  # stays in place for next night
                continue
            if track_key:
                track_db.record_deletion(track_key)  # cooldown: no re-download
            filepath.unlink()
            results["quota"] += 1
            continue
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
    if results["quota"]:
        logger.info("  Quota curation (cooldown, non retenus): %s", results["quota"])
    if results["carryover"]:
        logger.info("  Carryover (pépites en attente pour demain): %s", results["carryover"])

    # Persist classify stats for the daily recap (same pattern as
    # last_discover/download/analyze_stats.json).
    try:
        classify_stats = dict(results)
        classify_stats["rotation_deleted"] = rotation_deleted
        (Path(__file__).parent.parent / "data" / "last_classify_stats.json").write_text(
            json.dumps(classify_stats), encoding="utf-8")
    except OSError as e:
        logger.warning("Could not write classify stats: %s", e)

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
