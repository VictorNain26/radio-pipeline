#!/usr/bin/env python3
"""
Download tracks from YouTube with proper HypeMachine metadata.

Features:
- Downloads audio with yt-dlp
- Renames to {artist} - {title}.mp3
- Embeds cover from HypeMachine
- Writes ID3 tags from HypeMachine data
- Checks AzuraCast library for duplicates (with robust HTTP client)
"""

import hashlib
import json
import logging
import re
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock
from typing import Literal, TypedDict

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from http_client import AzuraCastClient, ClientError, HTTPConnectionError, ServerError
from genre_client import GenreClient, create_genre_client
from settings import get_settings, validate_environment

try:
    from config import (
        ALLOWED_GENRES,
        AUDIO_FILTERS,
        GENRE_FILTER,
        ROTATION,
        format_duration,
    )
except ImportError as e:
    print(f"Error: config.py not found or invalid in pipeline root: {e}")
    sys.exit(1)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# Constants
SCRIPT_DIR = Path(__file__).parent
PIPELINE_DIR = SCRIPT_DIR.parent
TRACKS_FILE = PIPELINE_DIR / "tracks-to-download.json"
DOWNLOAD_DIR = PIPELINE_DIR / "downloads"
TEMP_DIR = PIPELINE_DIR / "temp"

MAX_FILENAME_LENGTH = 200
REQUEST_TIMEOUT = 30
MATCH_SCORE_THRESHOLD = 0.60
MAX_PARALLEL_DOWNLOADS = 3

# Thread-safe lock for shared state
_download_lock = Lock()

DownloadResult = Literal["downloaded", "skipped", "filtered", "blocked", "failed"]


def compute_sha256(filepath: Path) -> str:
    """
    Compute SHA-256 hash of a file.

    Args:
        filepath: Path to the file.

    Returns:
        Hexadecimal SHA-256 hash string.
    """
    sha256_hash = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256_hash.update(chunk)
    return sha256_hash.hexdigest()


def validate_audio_integrity(filepath: Path) -> tuple[bool, str]:
    """
    Validate audio file integrity using ffprobe (best practice 2026).

    Checks:
    - File is a valid audio container
    - Audio stream exists and is decodable
    - Duration is positive
    - No corruption errors

    Args:
        filepath: Path to the audio file.

    Returns:
        Tuple of (is_valid, error_message).
    """
    if not filepath.exists():
        return False, "File does not exist"

    # Check file size (minimum 10KB for a valid audio file)
    if filepath.stat().st_size < 10240:
        return False, "File too small (< 10KB)"

    try:
        # Probe file metadata with ffprobe
        probe_cmd = [
            "ffprobe",
            "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "stream=codec_name,duration,sample_rate,channels",
            "-show_entries", "format=duration,size",
            "-of", "json",
            str(filepath),
        ]

        result = subprocess.run(
            probe_cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode != 0:
            return False, f"ffprobe failed: {result.stderr.strip()}"

        probe_data = json.loads(result.stdout)

        # Check audio stream exists
        streams = probe_data.get("streams", [])
        if not streams:
            return False, "No audio stream found"

        audio_stream = streams[0]
        codec = audio_stream.get("codec_name", "")
        if codec not in ("mp3", "aac", "flac", "vorbis", "opus", "pcm_s16le"):
            return False, f"Unexpected audio codec: {codec}"

        # Check duration is valid
        duration = float(probe_data.get("format", {}).get("duration", 0) or 0)
        if duration <= 0:
            # Try stream duration as fallback
            duration = float(audio_stream.get("duration", 0) or 0)

        if duration <= 0:
            return False, "Invalid duration (0 or negative)"

        # Full decode test to check for corruption (quick scan)
        decode_cmd = [
            "ffmpeg",
            "-v", "error",
            "-i", str(filepath),
            "-f", "null",
            "-t", "10",  # Only decode first 10 seconds for speed
            "-",
        ]

        decode_result = subprocess.run(
            decode_cmd,
            capture_output=True,
            text=True,
            timeout=60,
        )

        if decode_result.stderr.strip():
            # Filter out non-critical warnings
            errors = [
                line for line in decode_result.stderr.strip().split("\n")
                if "error" in line.lower() or "corrupt" in line.lower()
            ]
            if errors:
                return False, f"Decode errors: {'; '.join(errors[:3])}"

        return True, ""

    except subprocess.TimeoutExpired:
        return False, "Validation timed out"
    except FileNotFoundError:
        return False, "ffprobe/ffmpeg not installed"
    except (OSError, ValueError, json.JSONDecodeError) as e:
        return False, f"Validation error: {e}"


class Track(TypedDict):
    """Track data from HypeMachine."""
    id: str
    artist: str
    title: str
    cover: str | None
    search: str


class SearchResult(TypedDict):
    """YouTube search result with metadata."""
    url: str
    title: str
    uploader: str
    channel: str
    duration: float
    score: float


def normalize_track_key(artist: str, title: str) -> str:
    """
    Create normalized key for track comparison.

    Canonical implementation is in track_db.py. This is kept for backward
    compatibility (other scripts may import from download.py).

    Args:
        artist: Artist name.
        title: Track title.

    Returns:
        Normalized lowercase key "artist - title".
    """
    # Import from canonical source to avoid divergence
    from track_db import normalize_track_key as _normalize
    return _normalize(artist, title)


def fetch_azuracast_library(client: AzuraCastClient) -> set[str]:
    """
    Fetch existing tracks from AzuraCast.

    Args:
        client: Configured AzuraCast client.

    Returns:
        Set of normalized "artist - title" keys.
    """
    try:
        files = client.get_station_files()
        existing = set()

        for f in files:
            artist = f.get("artist", "") or ""
            title = f.get("title", "") or ""

            if artist and title:
                key = normalize_track_key(artist, title)
                existing.add(key)

        logger.info("AzuraCast library: %d tracks", len(existing))
        return existing

    except ClientError as e:
        logger.error("AzuraCast authentication error: %s", e)
        raise SystemExit(1)

    except (ServerError, HTTPConnectionError) as e:
        logger.error("Cannot connect to AzuraCast: %s", e)
        logger.error("Aborting to prevent duplicates. Fix connection and retry.")
        raise SystemExit(1)


def sanitize_filename(name: str) -> str:
    """
    Remove invalid characters from filename.

    Args:
        name: Raw filename string.

    Returns:
        Sanitized filename safe for filesystem.
    """
    name = re.sub(r'[<>:"/\\|?*]', '', name)
    name = re.sub(r'\s+', ' ', name).strip()
    return name[:MAX_FILENAME_LENGTH]


def normalize_for_matching(text: str) -> str:
    """
    Normalize a string for fuzzy music matching.

    Steps: NFKD unicode → strip accents → lowercase → remove YouTube
    noise → normalize feat. variants → keep alphanumeric + spaces.
    """
    import unicodedata

    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    # Remove parenthesized/bracketed YouTube noise
    text = re.sub(
        r"[\(\[]("
        r"official\s*(music\s*)?video|official\s*audio|"
        r"lyric\s*video|lyrics?|audio|visualizer|"
        r"official\s*visualizer|official\s*lyric\s*video|"
        r"hq|hd|4k"
        r")[\)\]]",
        "",
        text,
    )
    # Normalize featuring variations
    text = re.sub(r"\b(feat\.?|ft\.?|featuring)\b", "feat", text)
    # Keep only alphanumeric + spaces
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return " ".join(text.split())


# ---------------------------------------------------------------------------
# Negative keyword filtering
# ---------------------------------------------------------------------------

# Hard reject: never download these
_NEGATIVE_KEYWORDS_REJECT: set[str] = {
    "full album", "full ep", "full mixtape", "complete album",
    "all songs", "playlist",
    "reaction", "review", "explained", "breakdown", "tutorial",
    "how to play", "guitar lesson", "drum cover",
    "first time listening", "first time hearing",
    "karaoke", "instrumental", "backing track", "minus one",
    "nightcore", "daycore", "8d audio", "8d", "bass boosted",
    "top 10", "top 20", "best of", "greatest hits",
    "compilation", "megamix",
}

# Soft penalty: reduce score but don't hard-reject
_NEGATIVE_KEYWORDS_PENALTY: dict[str, float] = {
    "live": 0.7,
    "cover": 0.3,
    "remix": 0.6,
    "acoustic": 0.7,
    "slowed": 0.3,
    "reverb": 0.4,
    "sped up": 0.3,
    "speed up": 0.3,
    "lofi": 0.4,
    "lo-fi": 0.4,
    "extended": 0.6,
    "extended mix": 0.5,
}


def _check_negative_keywords(
    video_title: str, expected_artist: str, expected_title: str,
) -> tuple[bool, float]:
    """
    Check video title for negative keywords (context-aware).

    Keywords that appear in the expected artist/title are ignored
    (e.g. the song "Cover Me" won't be penalized for "cover").

    Returns:
        (should_reject, penalty_multiplier 0.0-1.0).
    """
    title_lower = video_title.lower()
    expected_lower = f"{expected_artist} {expected_title}".lower()

    for kw in _NEGATIVE_KEYWORDS_REJECT:
        if kw in title_lower and kw not in expected_lower:
            return True, 0.0

    penalty = 1.0
    for kw, mult in _NEGATIVE_KEYWORDS_PENALTY.items():
        if kw in expected_lower:
            continue
        if re.search(rf"\b{re.escape(kw)}\b", title_lower):
            penalty = min(penalty, mult)

    return False, penalty


# ---------------------------------------------------------------------------
# Duration scoring
# ---------------------------------------------------------------------------

def _score_duration(duration: float) -> float:
    """Score duration plausibility for a single track (0.0-1.0)."""
    if duration <= 0:
        return 0.5  # Unknown, neutral
    if duration < 30 or duration > 600:
        return 0.0  # Clip or full album
    if 120 <= duration <= 330:
        return 1.0  # Sweet spot 2-5.5 min
    if 60 <= duration < 120 or 330 < duration <= 480:
        return 0.8  # Acceptable
    if duration < 60:
        return 0.3
    return 0.5  # 480-600 range


# ---------------------------------------------------------------------------
# Channel trust scoring
# ---------------------------------------------------------------------------

def _score_channel_trust(expected_artist: str, info: dict) -> float:
    """Score channel trustworthiness (0.0-1.0)."""
    from rapidfuzz import fuzz

    channel = info.get("channel", "")
    uploader = info.get("uploader", "")
    title = info.get("title", "")
    followers = info.get("channel_follower_count") or 0

    norm_artist = normalize_for_matching(expected_artist)

    # Auto-generated "Topic" channels = very strong signal
    for name in (channel, uploader):
        if name.endswith(" - Topic"):
            topic_artist = normalize_for_matching(name.replace(" - Topic", ""))
            if fuzz.token_set_ratio(topic_artist, norm_artist) > 80:
                return 1.0

    # Channel name matches artist
    channel_sim = max(
        fuzz.token_set_ratio(norm_artist, normalize_for_matching(channel)),
        fuzz.token_set_ratio(norm_artist, normalize_for_matching(uploader)),
    )
    score = 0.5
    if channel_sim > 85:
        score = 0.9
    elif channel_sim > 70:
        score = 0.7

    # Subscriber bonus
    if followers > 1_000_000:
        score = max(score, 0.8)
    elif followers > 100_000:
        score = max(score, 0.7)
    elif followers > 10_000:
        score = max(score, 0.6)

    # "Official" in video title
    title_lower = title.lower()
    if any(tag in title_lower for tag in (
        "official audio", "official video", "official music video",
        "official visualizer", "official lyric video",
    )):
        score = min(score + 0.1, 1.0)

    return score


# ---------------------------------------------------------------------------
# Composite scoring
# ---------------------------------------------------------------------------

def _score_candidate(
    expected_artist: str, expected_title: str, info: dict,
) -> tuple[float, str]:
    """
    Score a YouTube candidate using all signals.

    Returns:
        (score 0.0-1.0, explanation string).
    """
    from rapidfuzz import fuzz

    video_title = info.get("title", "")
    duration = info.get("duration") or 0
    yt_artist = info.get("artist")  # Structured YouTube Music metadata
    yt_track = info.get("track")    # Structured YouTube Music metadata

    # Phase 1: negative keywords (early exit)
    reject, kw_penalty = _check_negative_keywords(
        video_title, expected_artist, expected_title,
    )
    if reject:
        return 0.0, "rejected:keyword"

    parts = []

    # Phase 2a: fuzzy match (always available)
    norm_artist = normalize_for_matching(expected_artist)
    norm_title = normalize_for_matching(expected_title)
    norm_video = normalize_for_matching(video_title)
    norm_uploader = normalize_for_matching(info.get("uploader", ""))
    norm_channel = normalize_for_matching(info.get("channel", ""))

    artist_score = max(
        fuzz.token_set_ratio(norm_artist, norm_video) / 100,
        fuzz.token_set_ratio(norm_artist, norm_uploader) / 100,
        fuzz.token_set_ratio(norm_artist, norm_channel) / 100,
    )
    title_score = fuzz.token_sort_ratio(norm_title, norm_video) / 100
    fuzzy = 0.45 * artist_score + 0.55 * title_score
    parts.append("fuzzy=%.2f(a=%.2f,t=%.2f)" % (fuzzy, artist_score, title_score))

    # Phase 2b: structured metadata (when YouTube identified the song)
    structured: float | None = None
    if yt_artist and yt_track:
        sa = fuzz.token_set_ratio(norm_artist, normalize_for_matching(yt_artist)) / 100
        st = fuzz.token_set_ratio(norm_title, normalize_for_matching(yt_track)) / 100
        structured = 0.45 * sa + 0.55 * st
        parts.append("struct=%.2f" % structured)

    # Phase 2c: channel trust
    ch_score = _score_channel_trust(expected_artist, info)
    parts.append("ch=%.2f" % ch_score)

    # Phase 2d: duration plausibility
    dur_score = _score_duration(float(duration))
    parts.append("dur=%.2f" % dur_score)

    # Phase 3: weighted combination
    if structured is not None and structured > 0.7:
        # Structured metadata available and strong → trust it
        final = (
            0.40 * structured
            + 0.25 * fuzzy
            + 0.20 * ch_score
            + 0.10 * dur_score
            + 0.05
        )
    else:
        # Fallback: fuzzy-heavy
        final = (
            0.50 * fuzzy
            + 0.25 * ch_score
            + 0.15 * dur_score
            + 0.10 * kw_penalty
        )

    final *= kw_penalty
    parts.append("final=%.2f" % final)
    return final, " | ".join(parts)


def find_best_youtube_match(
    search: str, artist: str, title: str,
) -> SearchResult | None:
    """
    Find the best YouTube match using comprehensive scoring.

    Uses --dump-json for full metadata (structured artist/track,
    subscriber count, tags). Scores each candidate with rapidfuzz,
    negative keyword filtering, duration validation, and channel trust.

    Returns:
        Best SearchResult if score >= MATCH_SCORE_THRESHOLD, else None.
    """
    cmd = [
        "yt-dlp",
        "--dump-json",
        "--no-download",
        "--no-warnings",
        "--no-playlist",
        # Escape hatch when YouTube ships a player change (yt-dlp 2026 best
        # practice). Probing only needs basic info; web client is enough.
        "--extractor-args", "youtube:player_client=web_safari,web",
        # Short probe; we hit ytsearch5 which already returns 5 candidates.
        "--socket-timeout", "20",
        "--", f'ytsearch5:"{artist}" "{title}"',
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
    except subprocess.TimeoutExpired:
        logger.warning("  YouTube search timed out")
        return None

    if result.returncode != 0:
        logger.warning("  YouTube search failed: %s", result.stderr[:200])
        return None

    best: SearchResult | None = None
    best_score = 0.0

    for line in result.stdout.strip().splitlines():
        try:
            info = json.loads(line)
        except json.JSONDecodeError:
            continue

        score, explanation = _score_candidate(artist, title, info)
        vid_title = info.get("title", "")
        url = info.get("webpage_url", "")

        logger.debug("  Candidate: %s | %s | %s", vid_title[:60], explanation, url)

        if score > best_score:
            best_score = score
            best = SearchResult(
                url=url,
                title=vid_title,
                uploader=info.get("uploader", ""),
                channel=info.get("channel", ""),
                duration=float(info.get("duration", 0) or 0),
                score=score,
            )

    if best is None or best["score"] < MATCH_SCORE_THRESHOLD:
        logger.warning(
            "  No good match for '%s' (best=%.2f, threshold=%.2f)",
            search, best_score, MATCH_SCORE_THRESHOLD,
        )
        return None

    logger.info(
        "  Match: %s (%.2f) %s", best["title"], best["score"], best["url"],
    )
    return best


def download_cover(url: str, output_path: Path) -> bool:
    """
    Download cover image from URL.

    Args:
        url: Cover image URL.
        output_path: Path to save the image.

    Returns:
        True if successful, False otherwise.
    """
    if not url:
        return False

    headers = {"User-Agent": "Mozilla/5.0 (RadioPipeline/2.0)"}
    req = urllib.request.Request(url, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as response:
            output_path.write_bytes(response.read())
        return True
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        logger.debug("  Cover download failed: %s", e)
        return False


def write_id3_tags(
    filepath: Path,
    artist: str,
    title: str,
    cover_path: Path | None = None,
    has_lastfm_tags: bool = False,
    lastfm_tags_str: str = "",
) -> bool:
    """
    Write ID3 tags using mutagen.

    Args:
        filepath: Path to MP3 file.
        artist: Artist name.
        title: Track title.
        cover_path: Optional path to cover image.
        has_lastfm_tags: Whether Last.fm had genre tags for this track.
        lastfm_tags_str: Raw comma-separated Last.fm tags for multi-signal filtering.

    Returns:
        True if successful, False otherwise.
    """
    try:
        from mutagen import MutagenError
        from mutagen.easyid3 import EasyID3
        from mutagen.id3 import APIC, ID3, ID3NoHeaderError, TXXX
        from mutagen.mp3 import MP3

        try:
            audio = EasyID3(str(filepath))
        except ID3NoHeaderError:
            audio = MP3(str(filepath))
            audio.add_tags()
            audio.save()
            audio = EasyID3(str(filepath))

        audio['artist'] = artist
        audio['title'] = title
        audio.save()

        # Write full ID3 tags (cover + custom TXXX)
        audio = ID3(str(filepath))

        # Add cover if provided
        if cover_path and cover_path.exists():
            cover_data = cover_path.read_bytes()
            audio.delall('APIC')
            audio.add(APIC(
                encoding=3,
                mime='image/jpeg',
                type=3,
                desc='Cover',
                data=cover_data
            ))

        # Add HAS_LASTFM_TAGS flag for aggressive filter in classify.py
        audio.delall('TXXX:HAS_LASTFM_TAGS')
        audio.add(TXXX(
            encoding=3,
            desc='HAS_LASTFM_TAGS',
            text=['true' if has_lastfm_tags else 'false']
        ))

        # Add raw Last.fm tags for multi-signal filtering
        audio.delall('TXXX:LASTFM_TAGS')
        if lastfm_tags_str:
            audio.add(TXXX(
                encoding=3,
                desc='LASTFM_TAGS',
                text=[lastfm_tags_str]
            ))

        audio.save()
        return True
    except (MutagenError, OSError) as e:
        logger.warning("  Tag writing failed: %s", e)
        return False


MAX_DOWNLOAD_RETRIES = 2  # Retry up to 2 times if file is corrupted


def fix_mp3_timestamps(filepath: Path) -> bool:
    """
    Fix MP3 timestamp issues using ffmpeg (best practice 2026).

    yt-dlp generates MP3 files with problematic timestamps that cause
    "Could not update timestamps for skipped samples" warnings in Liquidsoap.

    This re-muxes the audio stream to fix timestamps without re-encoding.

    Args:
        filepath: Path to MP3 file.

    Returns:
        True if successful, False otherwise.
    """
    if not filepath.exists():
        return False

    temp_fixed = filepath.with_suffix(".fixed.mp3")

    try:
        # Re-mux audio stream to fix timestamps (no re-encoding = lossless)
        cmd = [
            "ffmpeg",
            "-y",                    # Overwrite output
            "-i", str(filepath),     # Input file
            "-c:a", "copy",          # Copy audio stream (no re-encoding)
            "-fflags", "+genpts",    # Generate proper timestamps
            "-map", "0:a",           # Only audio stream (drop cover art)
            str(temp_fixed),
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
        )

        if result.returncode != 0:
            logger.debug("  ffmpeg fix failed: %s", result.stderr[:200])
            temp_fixed.unlink(missing_ok=True)
            return False

        # Replace original with fixed version
        temp_fixed.replace(filepath)
        logger.debug("  Timestamps fixed")
        return True

    except subprocess.TimeoutExpired:
        temp_fixed.unlink(missing_ok=True)
        return False
    except OSError as e:
        logger.debug("  Fix timestamps failed: %s", e)
        temp_fixed.unlink(missing_ok=True)
        return False


def download_track(
    track: Track,
    existing_library: set[str],
    genre_client: GenreClient | None = None,
    _retry_count: int = 0,
    track_db: "TrackDB | None" = None,
) -> DownloadResult:
    """
    Download a single track and apply metadata.

    Best practices 2026:
    - Validates audio integrity after download
    - Retries automatically if file is corrupted
    - Computes checksum for tracking
    - Checks cooldown via TrackDB (skip recently deleted tracks)

    Args:
        track: Track data from HypeMachine.
        existing_library: Set of normalized "artist - title" already in AzuraCast.
        genre_client: Optional Last.fm client for genre filtering.
        _retry_count: Internal retry counter.
        track_db: Optional persistent track database for cooldown checks.

    Returns:
        Download result status.
    """
    artist = track.get('artist', 'Unknown')
    title = track.get('title', 'Unknown')
    cover_url = track.get('cover')
    search = track.get('search', f"{artist} - {title}")

    # Check against AzuraCast library (primary duplicate detection)
    track_key = normalize_track_key(artist, title)
    with _download_lock:
        if track_key in existing_library:
            return 'skipped'
        existing_library.add(track_key)

    # Check cooldown (skip recently deleted tracks)
    if track_db and track_db.is_in_cooldown(track_key, ROTATION.cooldown_days):
        logger.info("  Cooldown: recently deleted, skipping")
        return 'skipped'

    # Genre filtering via Last.fm (before download to save bandwidth)
    has_lastfm_tags = False
    lastfm_tags_str = ""
    if genre_client and GENRE_FILTER.enabled:
        genre_result = genre_client.check_genre(artist, title)
        has_lastfm_tags = bool(genre_result.tags)
        lastfm_tags_str = ", ".join(genre_result.tags) if genre_result.tags else ""
        if genre_result.is_blocked:
            logger.info("  Blocked: %s", genre_result.blocked_reason)
            return 'blocked'
        if genre_result.top_tag:
            logger.debug("  Genre: %s", genre_result.top_tag)
        if GENRE_FILTER.require_tags and not genre_result.tags:
            logger.info("  Blocked: No genre tags found")
            return 'blocked'

    # Create safe filename
    safe_name = sanitize_filename(f"{artist} - {title}")
    final_path = DOWNLOAD_DIR / f"{safe_name}.mp3"

    # Skip if already exists locally
    if final_path.exists():
        logger.info("  Already exists locally")
        return 'skipped'

    # Phase 1: Probe YouTube for best match (no download)
    match = find_best_youtube_match(search, artist, title)
    if match is None:
        return 'failed'

    # Pre-check duration from probe metadata
    if match["duration"] > 0:
        if AUDIO_FILTERS.duration_min and match["duration"] <= AUDIO_FILTERS.duration_min:
            logger.info("  Filtered (too short: %s)", format_duration(int(match['duration'])))
            return 'filtered'
        if AUDIO_FILTERS.duration_max and match["duration"] >= AUDIO_FILTERS.duration_max:
            logger.info("  Filtered (too long: %s)", format_duration(int(match['duration'])))
            return 'filtered'

    # Phase 2: Download the specific matched URL (thread-safe temp dir)
    thread_temp = TEMP_DIR / f"worker_{threading.current_thread().ident}"
    thread_temp.mkdir(parents=True, exist_ok=True)
    temp_output = thread_temp / "temp_download.%(ext)s"

    cmd = [
        "yt-dlp",
        "--extract-audio",
        "--audio-format", "mp3",
        "--audio-quality", "0",
        "-o", str(temp_output),
        "--no-playlist",
        "--no-warnings",
        # Anti-throttling: with 3 parallel workers from a single IP, hitting
        # YouTube unthrottled gets us 429'd. Random sleep between requests.
        "--sleep-interval", "3",
        "--max-sleep-interval", "8",
        # Retries with internal backoff (yt-dlp 2026 defaults are too low).
        "--retries", "5",
        "--fragment-retries", "5",
        # Player-client escape hatch (same reason as in the probe).
        "--extractor-args", "youtube:player_client=web_safari,web",
        match["url"],
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        logger.warning("  yt-dlp download timed out (5min)")
        cleanup_temp()
        return 'failed'

    # Find the downloaded file
    temp_files = list(thread_temp.glob("temp_download.mp3"))
    if not temp_files:
        temp_files = list(thread_temp.glob("temp_download.*"))

    if not temp_files:
        logger.warning("  No file found after download")
        return 'failed'

    temp_file = temp_files[0]

    if temp_file.suffix != '.mp3':
        logger.warning("  Unexpected format: %s", temp_file.suffix)
        temp_file.unlink()
        return 'failed'

    DOWNLOAD_DIR.mkdir(exist_ok=True)
    shutil.move(str(temp_file), str(final_path))

    # Validate audio integrity (best practice 2026)
    is_valid, error_msg = validate_audio_integrity(final_path)
    if not is_valid:
        logger.warning("  Audio integrity check FAILED: %s", error_msg)
        final_path.unlink(missing_ok=True)

        # Retry download if not exceeded max retries
        if _retry_count < MAX_DOWNLOAD_RETRIES:
            logger.info("  Retrying download (%d/%d)...", _retry_count + 1, MAX_DOWNLOAD_RETRIES)
            return download_track(track, existing_library, genre_client, _retry_count + 1, track_db)

        logger.error("  Failed after %d retries - file corrupted", MAX_DOWNLOAD_RETRIES)
        return 'failed'

    # Fix MP3 timestamps (best practice 2026)
    # Prevents "Could not update timestamps" warnings in Liquidsoap
    fix_mp3_timestamps(final_path)

    # Compute checksum for tracking
    file_hash = compute_sha256(final_path)
    logger.debug("  SHA-256: %s", file_hash)

    # Download and embed cover
    cover_path: Path | None = None
    if cover_url:
        cover_path = thread_temp / "cover.jpg"
        if download_cover(cover_url, cover_path):
            logger.info("  Cover: OK")
        else:
            cover_path = None

    # Write ID3 tags (including Last.fm tags for multi-signal filter)
    if write_id3_tags(final_path, artist, title, cover_path, has_lastfm_tags, lastfm_tags_str):
        logger.info("  Tags: artist=%s, title=%s", artist, title)
        if not has_lastfm_tags:
            logger.debug("  Note: No Last.fm tags (will use audio analysis for filtering)")

    # Cleanup cover
    if cover_path and cover_path.exists():
        cover_path.unlink()

    return 'downloaded'


def cleanup_temp() -> None:
    """Clean up temporary directory."""
    shutil.rmtree(TEMP_DIR, ignore_errors=True)


def load_tracks() -> list[Track]:
    """
    Load tracks from JSON file.

    Returns:
        List of tracks or empty list on error.
    """
    if not TRACKS_FILE.exists():
        return []

    try:
        with open(TRACKS_FILE, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError) as e:
        logger.error("Failed to load tracks: %s", e)
        return []


def main() -> int:
    """
    Main entry point.

    Returns:
        Exit code (0 for success, 1 for error).
    """
    logger.info("=== Download with HypeMachine Metadata ===")

    # Validate configuration
    is_valid, errors = validate_environment()
    if not is_valid:
        for error in errors:
            logger.error("Config error: %s", error)
        return 1

    settings = get_settings()

    # Show duration filter if active
    if AUDIO_FILTERS.duration_min or AUDIO_FILTERS.duration_max:
        filter_parts = []
        if AUDIO_FILTERS.duration_min:
            filter_parts.append(f"> {format_duration(AUDIO_FILTERS.duration_min)}")
        if AUDIO_FILTERS.duration_max:
            filter_parts.append(f"< {format_duration(AUDIO_FILTERS.duration_max)}")
        logger.info("Duration filter: %s", " and ".join(filter_parts))

    tracks = load_tracks()
    if not tracks:
        logger.info("No tracks file found")
        return 0

    logger.info("Tracks to process: %d", len(tracks))

    # Create AzuraCast client with retry logic
    client = AzuraCastClient(
        base_url=settings.azuracast_url,
        api_key=settings.azuracast_api_key,
        station_id=settings.azuracast_station_id,
        timeout=settings.http_timeout,
    )

    # Health check before proceeding
    if not client.health_check():
        logger.error("AzuraCast is not reachable. Aborting.")
        return 1

    # Fetch existing library (REQUIRED to prevent duplicates)
    existing_library = fetch_azuracast_library(client)

    # Create Last.fm client for genre filtering (REQUIRED when enabled)
    genre_client: GenreClient | None = None
    if GENRE_FILTER.enabled:
        # Multi-source genre filter: MusicBrainz + Discogs + Last.fm.
        # None of them is strictly required (each is best-effort), but we want
        # at least one signal — warn loudly if Last.fm is missing since it's the
        # broadest coverage for obscure indie artists.
        if not settings.lastfm_api_key:
            logger.warning(
                "LASTFM_API_KEY not set — relying on MusicBrainz + Discogs only "
                "(coverage may drop for obscure artists)"
            )
        genre_client = create_genre_client(
            lastfm_api_key=settings.lastfm_api_key,
            discogs_token=settings.discogs_token,
            blocked_genres=list(GENRE_FILTER.blocked_genres),
            allowed_genres=list(ALLOWED_GENRES),
        )
        sources = [
            s for s, on in (
                ("MusicBrainz", True),
                ("Discogs", True),
                ("Last.fm", bool(settings.lastfm_api_key)),
            ) if on
        ]
        logger.info(
            "Genre filter: %d blocked / %d allowed | sources: %s",
            len(GENRE_FILTER.blocked_genres),
            len(ALLOWED_GENRES),
            ", ".join(sources),
        )

    DOWNLOAD_DIR.mkdir(exist_ok=True)

    # Initialize TrackDB for cooldown checks
    from track_db import TrackDB
    db_path = Path(__file__).parent.parent / "data" / "tracks.db"
    track_db = TrackDB(db_path)

    stats = {"downloaded": 0, "skipped": 0, "filtered": 0, "blocked": 0, "failed": 0}

    def _process_track(idx_track: tuple[int, Track]) -> tuple[str, DownloadResult]:
        i, track = idx_track
        artist = track.get('artist', 'Unknown')
        title = track.get('title', 'Unknown')
        logger.info("\n[%d/%d] %s - %s", i, len(tracks), artist, title)

        result = download_track(track, existing_library, genre_client, track_db=track_db)

        if result == 'downloaded':
            logger.info("  OK")
        elif result == 'skipped':
            logger.info("  Skipped (duplicate or cooldown)")

        return f"{artist} - {title}", result

    try:
        with ThreadPoolExecutor(max_workers=MAX_PARALLEL_DOWNLOADS) as executor:
            futures = {
                executor.submit(_process_track, (i, track)): track
                for i, track in enumerate(tracks, 1)
            }
            for future in as_completed(futures):
                try:
                    _name, result = future.result()
                    stats[result] += 1
                except Exception as e:
                    logger.error("  Unexpected download error: %s", e)
                    stats["failed"] += 1
    finally:
        track_db.close()
        if genre_client is not None:
            genre_client.flush_cache()

    cleanup_temp()

    logger.info("\n=== Results ===")
    logger.info("Downloaded: %d", stats['downloaded'])
    logger.info("Skipped: %d", stats['skipped'])
    logger.info("Blocked (genre): %d", stats['blocked'])
    logger.info("Filtered (duration): %d", stats['filtered'])
    logger.info("Failed: %d", stats['failed'])

    # Persist for run.sh aggregation.
    stats_path = Path(__file__).parent.parent / "data" / "last_download_stats.json"
    stats_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        stats_path.write_text(json.dumps(stats, indent=2), encoding="utf-8")
    except OSError:
        logger.warning("Could not write last_download_stats.json")

    return 0


if __name__ == "__main__":
    sys.exit(main())
