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
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock
from typing import Literal, NamedTuple, TypedDict

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from http_client import AzuraCastClient, ClientError, HTTPConnectionError, ServerError
from audio_fingerprint import compute_fingerprint, fingerprint_hash
from genre_client import GenreClient, create_genre_client
from settings import get_settings, validate_environment

try:
    from config import (
        ACOUSTID_DEDUP,
        AUDIO_FILTERS,
        GENRE_FILTER,
        LOUDNORM,
        ROTATION,
        TASTE_FILTER,
        format_duration,
        source_priority,
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
# Output filenames claimed by in-flight downloads (guarded by _download_lock)
_claimed_paths: set[str] = set()

DownloadResult = Literal["downloaded", "skipped", "filtered", "blocked", "failed", "duplicate"]


class DownloadOutcome(NamedTuple):
    """
    Result of a download_track call.

    `status` and `source` are the primary signals consumed by stats.
    The two boolean flags surface failures of post-download steps that
    used to be "logged but uncountable" silent fallbacks :

      - loudnorm_failed   : ffmpeg loudnorm pass returned non-zero.
                            Track is kept but un-normalised (volume drift).
      - fingerprint_failed: fpcalc / Chromaprint failed. Track is kept
                            but absent from the AcoustID dedup index,
                            so a true audio duplicate could slip in
                            on a future run.

    Both default to False so all the pre-download returns
    ('skipped'/'blocked'/'failed-before-probe'/...) keep their concise
    constructor shape.
    """
    status: DownloadResult
    source: str | None = None
    loudnorm_failed: bool = False
    fingerprint_failed: bool = False


def loudnorm_inplace(filepath: Path) -> bool:
    """
    EBU R128 single-pass loudness normalisation, in-place rewrite.

    Re-encodes to MP3 V0 (transparent quality at variable bitrate ~245 kbps),
    keeps ID3 tags via -map_metadata. Target -16 LUFS is the modern
    standard for music streaming. Returns True on success; on failure
    the original file is left untouched.
    """
    # Tmp filename MUST end in .mp3 so ffmpeg infers the muxer from extension.
    # An older version used `.mp3.ln.tmp` which made ffmpeg fail with
    # "Unable to choose an output format" (100% loudnorm fail rate on the
    # 2026-05-14 manual run, fixed here).
    tmp = filepath.with_name(filepath.stem + ".ln.mp3")
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(filepath),
        "-af",
        f"loudnorm=I={LOUDNORM.target_lufs}:LRA={LOUDNORM.loudness_range}:TP={LOUDNORM.true_peak}",
        "-c:a", "libmp3lame", "-q:a", "0",
        "-map_metadata", "0",
        "-id3v2_version", "3",
        "-f", "mp3",  # belt-and-suspenders: force MP3 muxer explicitly
        str(tmp),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        tmp.unlink(missing_ok=True)
        return False
    if result.returncode != 0:
        logger.warning("  loudnorm failed: %s", (result.stderr or "")[:200])
        tmp.unlink(missing_ok=True)
        return False
    tmp.replace(filepath)
    return True


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
    """Multi-source audio search result with metadata."""
    url: str
    title: str
    uploader: str
    channel: str
    duration: float
    score: float
    source: str  # "youtube" / "soundcloud" / ...  (yt-dlp extractor_key, lowercased)


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
    """
    Score duration plausibility (0.0-1.0).

    Hard-zero for clips < 60s : these are SoundCloud previews (the artist
    is signed to a label and only an excerpt is hosted). Downloading
    them is pure waste — AUDIO_FILTERS.duration_min would filter them
    out post-download anyway. Killing them at scoring time means the
    full-length candidate (typically on YouTube) wins instead of being
    silently shadowed by a 30s preview that ranked higher on fuzzy match.
    """
    if duration <= 0:
        return 0.5  # Unknown — fall through, post-download filters catch it
    if duration < 60 or duration > 600:
        return 0.0  # Preview clip OR full album/DJ mix — both useless for the radio
    if 120 <= duration <= 330:
        return 1.0  # Sweet spot 2-5.5 min
    if 60 <= duration < 120 or 330 < duration <= 480:
        return 0.8  # Acceptable
    return 0.5  # 480-600 grey zone


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

    # Phase 2d: duration plausibility.
    # A 0.0 from _score_duration is a *hard reject* signal (preview clip
    # < 60s, or full album/mix > 600s) — bail out before structured
    # metadata can resurrect the candidate via its +0.05 bonus.
    dur_score = _score_duration(float(duration))
    if dur_score == 0.0 and duration and duration > 0:
        return 0.0, f"rejected:duration={duration:.0f}s"
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


def _probe_source(
    search_prefix: str, artist: str, title: str, timeout: int = 45,
) -> list[dict]:
    """
    Run a single yt-dlp metadata probe for a given search prefix.

    `search_prefix` is either:
      - "ytsearch5"  → YouTube top-5 results
      - "scsearch5"  → SoundCloud top-5 results

    Returns a list of candidate `info` dicts as yt-dlp emits them with
    --dump-json (one JSON line per candidate). Empty list on probe failure
    (timeout, non-zero exit, no parseable JSON). Failure is logged loudly
    so we never have a "silent skip".
    """
    cmd = [
        "yt-dlp",
        "--dump-json",
        "--no-download",
        "--no-warnings",
        "--no-playlist",
        "--socket-timeout", "20",
        "--", f'{search_prefix}:"{artist}" "{title}"',
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        logger.warning("  Probe %s timed out (%ds)", search_prefix, timeout)
        return []
    if result.returncode != 0:
        logger.warning(
            "  Probe %s failed (rc=%d): %s",
            search_prefix, result.returncode, (result.stderr or "")[:200],
        )
        return []
    candidates: list[dict] = []
    for line in result.stdout.strip().splitlines():
        try:
            candidates.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return candidates


def find_best_audio_match(
    search: str, artist: str, title: str,
) -> SearchResult | None:
    """
    Find the best audio match across SoundCloud + YouTube.

    Both platforms are probed in parallel (~5-10s total wall time vs
    ~15s sequential). Every candidate is scored with the same algorithm
    — rapidfuzz on artist/title, duration sanity, channel trust,
    negative-keyword filter — and the highest-scoring one wins
    regardless of source.

    Why two sources :
    - YouTube has the broadest catalogue but is increasingly hostile
      (SABR rollout, PoToken requirements). 2026 incidents are frequent.
    - SoundCloud is more stable and stronger for indie / electronic /
      hip-hop (the AubeSonore aesthetic). Many indie artists upload
      full-length tracks directly; preview clips for label-signed
      artists get filtered out by the duration score (<60s → 0.3).
    - The scoring already handles SoundCloud's quirks (remix / slowed
      / sped-up variants are caught by _NEGATIVE_KEYWORDS_REJECT and
      _NEGATIVE_KEYWORDS_PENALTY).

    Returns the best SearchResult (with `source` field) if score
    >= MATCH_SCORE_THRESHOLD, else None.
    """
    queries = [("scsearch5", "soundcloud"), ("ytsearch5", "youtube")]
    all_candidates: list[dict] = []

    with ThreadPoolExecutor(max_workers=len(queries)) as ex:
        futures = {
            ex.submit(_probe_source, q, artist, title): label
            for q, label in queries
        }
        for fut in as_completed(futures):
            label = futures[fut]
            try:
                cands = fut.result()
            except Exception as e:
                logger.warning("  Probe %s crashed: %s", label, e)
                cands = []
            logger.debug("  %s returned %d candidates", label, len(cands))
            all_candidates.extend(cands)

    best: SearchResult | None = None
    best_score = 0.0

    for info in all_candidates:
        score, explanation = _score_candidate(artist, title, info)
        vid_title = info.get("title", "")
        url = info.get("webpage_url", "")
        src = (info.get("extractor_key") or info.get("extractor") or "?").lower()

        logger.debug("  Candidate[%s]: %s | %s | %s",
                     src, vid_title[:60], explanation, url)

        if score > best_score:
            best_score = score
            best = SearchResult(
                url=url,
                title=vid_title,
                uploader=info.get("uploader", ""),
                channel=info.get("channel", ""),
                duration=float(info.get("duration", 0) or 0),
                score=score,
                source=src,
            )

    if best is None or best["score"] < MATCH_SCORE_THRESHOLD:
        logger.warning(
            "  No good match for '%s' across %d candidates (best=%.2f < %.2f)",
            search, len(all_candidates), best_score, MATCH_SCORE_THRESHOLD,
        )
        return None

    logger.info(
        "  Match[%s]: %s (%.2f) %s",
        best["source"], best["title"], best["score"], best["url"],
    )
    return best


def fetch_itunes_cover(artist: str, title: str, output_dir: Path) -> "Path | None":
    """
    iTunes Search API fallback for tracks whose discovery source has no cover.
    Queries the same endpoint as the AubeSonore backend, upgrades 100px → 600px.
    """
    query = urllib.parse.quote(f"{artist} {title}")
    api_url = (
        f"https://itunes.apple.com/search"
        f"?term={query}&media=music&entity=song&limit=1&country=FR"
    )
    req = urllib.request.Request(api_url, headers={"User-Agent": "Mozilla/5.0 (RadioPipeline/2.0)"})
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as e:
        logger.debug("  iTunes lookup failed: %s", e)
        return None

    results = data.get("results") or []
    if not results:
        return None

    artwork_url = results[0].get("artworkUrl100")
    if not artwork_url:
        return None

    artwork_url = artwork_url.replace("100x100bb", "600x600bb")
    cover_path = output_dir / "cover.jpg"
    return cover_path if download_cover(artwork_url, cover_path) else None


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
            # Sniff the actual format — Last.fm/iTunes sometimes serve PNG,
            # and a wrong APIC mime breaks artwork on some players.
            mime = 'image/png' if cover_data.startswith(b'\x89PNG') else 'image/jpeg'
            audio.delall('APIC')
            audio.add(APIC(
                encoding=3,
                mime=mime,
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
) -> DownloadOutcome:
    """
    Download a single track and apply metadata.

    Best practices 2026:
    - Validates audio integrity after download
    - Retries automatically if file is corrupted
    - Computes checksum for tracking

    Library, cooldown and genre filtering now happen in prefilter_candidates,
    before any byte is transferred. What remains here is the per-key dedup
    under _download_lock, which the cold phase cannot cover: two workers can
    still target the same key within a single run.

    Args:
        track: Track data from HypeMachine.
        existing_library: Set of normalized "artist - title" already in AzuraCast.
        genre_client: Optional Last.fm client, read from cache for ID3 tags.
        _retry_count: Internal retry counter.
        track_db: Optional persistent track database for fingerprints and verdicts.

    Returns:
        Download result status.
    """
    artist = track.get('artist', 'Unknown')
    title = track.get('title', 'Unknown')
    cover_url = track.get('cover')
    search = track.get('search', f"{artist} - {title}")

    # Check against AzuraCast library (primary duplicate detection).
    # Skipped on retries: attempt 0 already claimed the key, re-checking
    # here would short-circuit the corruption retry as 'skipped'.
    track_key = normalize_track_key(artist, title)
    if _retry_count == 0:
        with _download_lock:
            if track_key in existing_library:
                return DownloadOutcome('skipped', None)
            existing_library.add(track_key)

    # Tags pour l'ID3 : la phase à froid a déjà rempli le cache de genres,
    # cet appel est servi localement.
    has_lastfm_tags = False
    lastfm_tags_str = ""
    if genre_client and GENRE_FILTER.enabled:
        genre_result = genre_client.check_genre(artist, title)
        has_lastfm_tags = bool(genre_result.tags)
        lastfm_tags_str = ", ".join(genre_result.tags) if genre_result.tags else ""

    # Create safe filename
    safe_name = sanitize_filename(f"{artist} - {title}")
    final_path = DOWNLOAD_DIR / f"{safe_name}.mp3"

    # Claim the output name under the lock: two different tracks can
    # sanitize to the same filename, and the exists() check alone is racy
    # while another worker is still mid-download. Retries keep the name
    # claimed on attempt 0.
    if _retry_count == 0:
        with _download_lock:
            if final_path.exists():
                logger.info("  Already exists locally")
                return DownloadOutcome('skipped', None)
            if str(final_path) in _claimed_paths:
                digest = hashlib.sha1(track_key.encode("utf-8")).hexdigest()[:6]
                final_path = DOWNLOAD_DIR / f"{safe_name} [{digest}].mp3"
            _claimed_paths.add(str(final_path))

    # Phase 1: Probe SoundCloud + YouTube in parallel, pick best across sources
    match = find_best_audio_match(search, artist, title)
    if match is None:
        return DownloadOutcome('failed', None)

    match_source = match["source"]

    # Pre-check duration from probe metadata
    if match["duration"] > 0:
        if AUDIO_FILTERS.duration_min and match["duration"] <= AUDIO_FILTERS.duration_min:
            logger.info("  Filtered (too short: %s)", format_duration(int(match['duration'])))
            # Pas de verdict ici : cette durée est celle du candidat trouvé
            # par la recherche (retenu dès 0,60 de similarité), pas celle du
            # morceau. Un mix ou un live mal apparié bannirait un morceau
            # légitime pour toujours. classify.py inscrit filtered_duration
            # sur le fichier réellement téléchargé, où le signal est solide.
            return DownloadOutcome('filtered', match_source)
        if AUDIO_FILTERS.duration_max and match["duration"] >= AUDIO_FILTERS.duration_max:
            logger.info("  Filtered (too long: %s)", format_duration(int(match['duration'])))
            # Pas de verdict ici : cette durée est celle du candidat trouvé
            # par la recherche (retenu dès 0,60 de similarité), pas celle du
            # morceau. Un mix ou un live mal apparié bannirait un morceau
            # légitime pour toujours. classify.py inscrit filtered_duration
            # sur le fichier réellement téléchargé, où le signal est solide.
            return DownloadOutcome('filtered', match_source)

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
        # Explicit best-audio format selection. We intentionally do NOT
        # pin player_client — YouTube's SABR rollout (2026) makes web
        # clients return streams that can't be downloaded directly, so
        # we let yt-dlp pick the best client (default includes android).
        "--format", "bestaudio/best",
        match["url"],
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        logger.warning("  yt-dlp download timed out (5min)")
        # Only clean this worker's temp dir — other workers are downloading
        # into their own subdirs of TEMP_DIR right now.
        shutil.rmtree(thread_temp, ignore_errors=True)
        return DownloadOutcome('failed', match_source)

    # Find the downloaded file
    temp_files = list(thread_temp.glob("temp_download.mp3"))
    if not temp_files:
        temp_files = list(thread_temp.glob("temp_download.*"))

    if not temp_files:
        if result.returncode != 0:
            logger.warning(
                "  yt-dlp failed (rc=%d): %s",
                result.returncode,
                (result.stderr or "").strip()[-300:],
            )
        else:
            logger.warning("  No file found after download")
        return DownloadOutcome('failed', match_source)

    temp_file = temp_files[0]

    if temp_file.suffix != '.mp3':
        logger.warning("  Unexpected format: %s", temp_file.suffix)
        temp_file.unlink()
        return DownloadOutcome('failed', match_source)

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
        return DownloadOutcome('failed', match_source)

    # Content-based dedup via Chromaprint (catches re-uploads under
    # different metadata: remasters, feat. rewrites, etc.)
    fingerprint_failed = False
    if ACOUSTID_DEDUP.enabled and track_db is not None:
        fp_result = compute_fingerprint(final_path)
        if fp_result is not None:
            fp, dur = fp_result
            fp_h = fingerprint_hash(fp)
            existing = track_db.find_by_fingerprint(fp_h)
            if existing and existing.get("track_key") != track_key:
                logger.info(
                    "  Duplicate audio (Chromaprint match: %s - %s)",
                    existing.get("artist") or "?", existing.get("title") or "?",
                )
                final_path.unlink(missing_ok=True)
                return DownloadOutcome('duplicate', match_source,
                                       fingerprint_failed=False)
            track_db.record_fingerprint(track_key, fp_h, dur)
        else:
            # fpcalc unavailable / decode error / etc. Already logged by
            # compute_fingerprint. Track passes but dedup is skipped.
            fingerprint_failed = True

    # EBU R128 loudness normalisation (broadcast standard -16 LUFS)
    loudnorm_failed = False
    if LOUDNORM.enabled:
        if loudnorm_inplace(final_path):
            logger.debug("  Loudnorm: -> %.1f LUFS target", LOUDNORM.target_lufs)
        else:
            # ffmpeg failure already logged in loudnorm_inplace. Track is
            # kept at native loudness so the radio still works — but the
            # caller can now alert on a non-zero counter.
            loudnorm_failed = True

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

    if cover_path is None:
        cover_path = fetch_itunes_cover(artist, title, thread_temp)
        if cover_path:
            logger.info("  Cover: iTunes fallback OK")

    # Write ID3 tags (including Last.fm tags for multi-signal filter)
    if write_id3_tags(final_path, artist, title, cover_path, has_lastfm_tags, lastfm_tags_str):
        logger.info("  Tags: artist=%s, title=%s", artist, title)
        if not has_lastfm_tags:
            logger.debug("  Note: No Last.fm tags (will use audio analysis for filtering)")

    # Cleanup cover
    if cover_path and cover_path.exists():
        cover_path.unlink()

    return DownloadOutcome(
        'downloaded', match_source,
        loudnorm_failed=loudnorm_failed,
        fingerprint_failed=fingerprint_failed,
    )


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


def compute_budget(carryover_files: int) -> int:
    """
    Nombre de morceaux qu'il est utile de télécharger cette nuit.

    On ne télécharge que ce qu'on peut espérer diffuser : le quota de la
    nuit, majoré d'une marge qui absorbe les rejets et les échecs, moins
    ce qui dort déjà dans downloads/.
    """
    full = int(ROTATION.max_uploads_per_night * ROTATION.download_margin)
    return max(0, full - carryover_files)


def prefilter_candidates(
    tracks: list[Track],
    library_keys: set[str],
    track_db: "TrackDB",
    genre_client: GenreClient | None,
) -> tuple[list[Track], dict[str, int]]:
    """
    Phase à froid : écarter tout ce qui est décidable sans télécharger.

    Aucun octet d'audio n'est transféré ici. Les seuls appels réseau
    possibles sont les recherches de genre, servies par data/genre_cache.json
    dans la grande majorité des cas.

    Le tri final n'écarte rien : il décide de l'ordre dans lequel le budget
    sera dépensé.

    Returns:
        (candidats retenus et ordonnés, compteurs par motif d'exclusion).
    """
    counts = {
        "already_in_library": 0,
        "duplicate_in_batch": 0,
        "cooldown": 0,
        "known_verdict": 0,
        "blocked_genre": 0,
        "no_metadata": 0,
    }
    survivors: list[Track] = []
    seen: set[str] = set()

    for track in tracks:
        artist = track.get("artist") or ""
        title = track.get("title") or ""
        if not (artist and title):
            counts["no_metadata"] += 1
            continue

        key = normalize_track_key(artist, title)

        if key in library_keys:
            counts["already_in_library"] += 1
            continue
        if key in seen:
            # Listé deux fois ce soir : ce n'est pas « déjà à l'antenne ».
            counts["duplicate_in_batch"] += 1
            continue
        if track_db.is_in_cooldown(key, ROTATION.cooldown_days):
            counts["cooldown"] += 1
            continue
        if track_db.has_active_verdict(key, TASTE_FILTER.verdict_ttl_days):
            counts["known_verdict"] += 1
            continue

        if genre_client is not None and GENRE_FILTER.enabled:
            result = genre_client.check_genre(artist, title)
            if result.is_blocked:
                logger.info("  Bloqué [%s - %s] : %s", artist, title, result.blocked_reason)
                track_db.record_verdict(key, "blocked_genre", reason=result.blocked_reason)
                counts["blocked_genre"] += 1
                continue
            if GENRE_FILTER.require_tags and not result.tags:
                # Pas de verdict : une absence de tags est transitoire (le
                # morceau est trop récent, ou les sources n'ont pas répondu).
                # L'inscrire bannirait à vie un morceau pour un silence du
                # réseau. On l'écarte pour cette nuit seulement.
                counts["blocked_genre"] += 1
                continue

        seen.add(key)
        survivors.append(track)

    # Tri stable : à priorité égale, l'ordre de découverte est conservé.
    survivors.sort(key=lambda t: source_priority(t.get("source", "")))
    return survivors, counts


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

    # AzuraCast fait autorité : on réconcilie avant tout, et les clés de
    # déduplication sortent du rapport plutôt que d'un fetch parallèle.
    from library_state import reconcile
    from track_db import TrackDB

    db_path = Path(__file__).parent.parent / "data" / "tracks.db"
    track_db = TrackDB(db_path)
    media_dir = Path(settings.azuracast_media_dir) if settings.azuracast_media_dir else None
    try:
        files = client.get_station_files()
    except ClientError as e:
        logger.error("AzuraCast authentication error: %s", e)
        track_db.close()
        return 1
    except (ServerError, HTTPConnectionError) as e:
        logger.error("Cannot connect to AzuraCast: %s", e)
        logger.error("Aborting to prevent duplicates. Fix connection and retry.")
        track_db.close()
        return 1
    report = reconcile(files, track_db, media_dir=media_dir)
    existing_library = report.library_keys

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
        )
        sources = [
            s for s, on in (
                ("MusicBrainz", True),
                ("Discogs", True),
                ("Last.fm", bool(settings.lastfm_api_key)),
            ) if on
        ]
        logger.info(
            "Genre filter: %d blocked | sources: %s",
            len(GENRE_FILTER.blocked_genres),
            ", ".join(sources),
        )

    DOWNLOAD_DIR.mkdir(exist_ok=True)

    stats = {
        # Primary status counts (one of these is incremented per track)
        "downloaded": 0, "skipped": 0, "filtered": 0, "blocked": 0,
        "failed": 0, "duplicate": 0,
        # Per-source download counts. Filled by download_track via the
        # match["source"] returned from find_best_audio_match.
        "source_youtube": 0,
        "source_soundcloud": 0,
        "source_other": 0,
        # Post-download step failures (logged + counted, no silent skips).
        # These were the 3 silent fallback paths identified in the
        # 2026-05-14 audit. loudnorm_failed > 0 fires an ntfy alert from
        # run.sh because that's broadcast-quality critical.
        "loudnorm_failed": 0,
        "fingerprint_failed": 0,
        # Phase à froid (2026-07) : ce qui a été écarté sans rien télécharger.
        # Préfixe `pre_` pour ne pas collisionner avec les compteurs que la
        # boucle parallèle incrémente (`skipped`, `blocked`, `filtered`).
        "prefiltered": 0,
        "pre_already_known": 0,
        "pre_duplicate_in_batch": 0,
        "pre_known_verdict": 0,
        "pre_blocked_genre": 0,
        "pre_no_metadata": 0,
        "budget": 0,
        "carryover_on_disk": 0,
    }

    # --- Phase à froid : rien n'est téléchargé ---
    # Elle tourne entièrement avant que le budget soit connu : à budget 0 on
    # paie donc les recherches de genre pour rien. C'est assumé — le cache de
    # genres et le registre des verdicts en profitent pour les nuits suivantes.
    candidates, prefilter_counts = prefilter_candidates(
        tracks, existing_library, track_db, genre_client
    )
    # Compteurs de la phase à froid, dans leurs propres clés : la boucle
    # parallèle incrémente `skipped` et `blocked` pour ses propres motifs
    # (fichier déjà sur disque, collision de clé entre workers), donc y
    # écrire ici ferait un double comptage et le récap publierait un
    # chiffre qui ne veut plus rien dire.
    stats["prefiltered"] = sum(prefilter_counts.values())
    stats["pre_already_known"] = (
        prefilter_counts["already_in_library"] + prefilter_counts["cooldown"]
    )
    stats["pre_duplicate_in_batch"] = prefilter_counts["duplicate_in_batch"]
    stats["pre_known_verdict"] = prefilter_counts["known_verdict"]
    stats["pre_blocked_genre"] = prefilter_counts["blocked_genre"]
    stats["pre_no_metadata"] = prefilter_counts["no_metadata"]
    logger.info(
        "Filtrage à froid : %d candidats → %d retenus (%d déjà en librairie, "
        "%d en double dans le lot, %d en cooldown, %d déjà jugés, "
        "%d genre bloqué)",
        len(tracks), len(candidates),
        prefilter_counts["already_in_library"],
        prefilter_counts["duplicate_in_batch"], prefilter_counts["cooldown"],
        prefilter_counts["known_verdict"], prefilter_counts["blocked_genre"],
    )

    # --- Budget : on ne télécharge que ce qu'on peut espérer diffuser ---
    carryover_on_disk = len(list(DOWNLOAD_DIR.glob("*.mp3")))
    budget = compute_budget(carryover_on_disk)
    stats["carryover_on_disk"] = carryover_on_disk
    stats["budget"] = budget
    logger.info(
        "Budget de la nuit : %d (quota %d × marge %.1f − %d en attente sur disque)",
        budget, ROTATION.max_uploads_per_night, ROTATION.download_margin,
        carryover_on_disk,
    )
    if budget == 0:
        logger.info("Stock suffisant : aucun téléchargement cette nuit.")
    tracks_to_download = candidates[:budget]

    def _process_track(idx_track: tuple[int, Track]) -> tuple[str, DownloadOutcome]:
        i, track = idx_track
        artist = track.get('artist', 'Unknown')
        title = track.get('title', 'Unknown')
        logger.info("\n[%d/%d] %s - %s", i, len(tracks_to_download), artist, title)

        outcome = download_track(track, existing_library, genre_client, track_db=track_db)

        if outcome.status == 'downloaded':
            logger.info("  OK (%s)", outcome.source or "?")
        elif outcome.status == 'skipped':
            logger.info("  Skipped (file already on disk, or key claimed by another worker)")

        return f"{artist} - {title}", outcome

    try:
        with ThreadPoolExecutor(max_workers=MAX_PARALLEL_DOWNLOADS) as executor:
            futures = {
                executor.submit(_process_track, (i, track)): track
                for i, track in enumerate(tracks_to_download, 1)
            }
            for future in as_completed(futures):
                try:
                    _name, outcome = future.result()
                    stats[outcome.status] += 1
                    # Per-source tracking: only count when we actually downloaded.
                    if outcome.status == 'downloaded' and outcome.source:
                        key = f"source_{outcome.source}"
                        if key in stats:
                            stats[key] += 1
                        else:
                            stats["source_other"] += 1
                    # Post-download silent fallback counters
                    if outcome.loudnorm_failed:
                        stats["loudnorm_failed"] += 1
                    if outcome.fingerprint_failed:
                        stats["fingerprint_failed"] += 1
                except Exception as e:
                    logger.error("  Unexpected download error: %s", e)
                    stats["failed"] += 1
    finally:
        track_db.close()
        if genre_client is not None:
            genre_client.flush_cache()

    cleanup_temp()

    logger.info("\n=== Results ===")
    logger.info("Candidats : %d → retenus %d → budget %d",
                len(tracks), len(candidates), budget)
    logger.info("Écartés avant téléchargement : %d", stats['prefiltered'])
    logger.info("  → déjà en librairie ou cooldown : %d", stats['pre_already_known'])
    logger.info("  → en double dans le lot         : %d", stats['pre_duplicate_in_batch'])
    logger.info("  → déjà jugés (registre)         : %d", stats['pre_known_verdict'])
    logger.info("  → genre bloqué                  : %d", stats['pre_blocked_genre'])
    logger.info("  → métadonnées illisibles        : %d", stats['pre_no_metadata'])
    logger.info("Téléchargés : %d", stats['downloaded'])
    logger.info("  → depuis YouTube    : %d", stats['source_youtube'])
    logger.info("  → depuis SoundCloud : %d", stats['source_soundcloud'])
    if stats['source_other']:
        logger.info("  → depuis autre      : %d", stats['source_other'])
    logger.info("Sautés dans la boucle (fichier présent, collision) : %d", stats['skipped'])
    logger.info("Doublon audio (empreinte) : %d", stats['duplicate'])
    logger.info("Filtré (durée) : %d", stats['filtered'])
    logger.info("Échecs : %d", stats['failed'])
    if stats['loudnorm_failed']:
        logger.warning("Loudnorm en échec (uploads non normalisés) : %d", stats['loudnorm_failed'])
    if stats['fingerprint_failed']:
        logger.warning("Empreinte en échec (dédup sautée) : %d", stats['fingerprint_failed'])

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
