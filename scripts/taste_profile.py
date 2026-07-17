"""
Personal taste profile — CLAP embeddings of Victor's own music library.

The profile is a matrix of L2-normalised CLAP embeddings sampled from
/media/plex/Musique (2 tracks per artist, spread across albums). A
candidate track's "taste score" is the mean cosine similarity with its
k nearest neighbours in the profile — high when the candidate sounds
like something in the personal library, low when it doesn't.

Storage (data/):
  taste_profile.npy         — (N, 512) float32, L2-normalised rows
  taste_profile_index.json  — {"entries": [{"path", "artist"}...],
                               "artists": [...], "built_at": iso}

The index doubles as the seed-artist list for PersonalArtistsSource
(discovery), so discover.py can read it without numpy.

Failure mode is graceful everywhere: a missing/corrupt profile makes
load_taste_profile() return None and the taste filter becomes a no-op
with a warning — the pipeline never breaks because of it.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

PROFILE_NPY = "taste_profile.npy"
PROFILE_INDEX = "taste_profile_index.json"

AUDIO_EXTENSIONS = {".flac", ".mp3", ".m4a", ".ogg", ".wav", ".aiff", ".opus"}

# Folders in the music root that are not artists.
_SKIP_DIRS = {"$recycle.bin", "system volume information", "recycle", "@eadir"}


def normalize_artist_name(name: str) -> str:
    """Lowercase, strip accents/punctuation — for artist-level dedup."""
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^\w\s]", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def sample_library(
    music_root: Path,
    per_artist: int = 2,
) -> list[tuple[str, Path]]:
    """
    Deterministically sample up to `per_artist` audio files per artist
    folder, spread across albums (first track of distinct albums first,
    then deeper). Returns [(artist_name, file_path), ...] sorted by
    artist for reproducibility.
    """
    samples: list[tuple[str, Path]] = []
    if not music_root.is_dir():
        return samples

    for artist_dir in sorted(music_root.iterdir(), key=lambda p: p.name.lower()):
        if not artist_dir.is_dir() or artist_dir.name.lower() in _SKIP_DIRS:
            continue
        # Group audio files by parent dir (album) — files directly under
        # the artist folder count as one pseudo-album.
        by_album: dict[Path, list[Path]] = {}
        try:
            for f in sorted(artist_dir.rglob("*")):
                if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS:
                    by_album.setdefault(f.parent, []).append(f)
        except OSError as e:
            logger.warning("Cannot scan %s: %s", artist_dir, e)
            continue
        if not by_album:
            continue
        albums = sorted(by_album.keys())
        picked: list[Path] = []
        depth = 0
        # Round-robin across albums: track 0 of each album, then track 1…
        while len(picked) < per_artist:
            advanced = False
            for album in albums:
                tracks = by_album[album]
                if depth < len(tracks):
                    picked.append(tracks[depth])
                    advanced = True
                    if len(picked) >= per_artist:
                        break
            if not advanced:
                break
            depth += 1
        for f in picked:
            samples.append((artist_dir.name, f))

    return samples


@dataclass
class TasteProfile:
    """In-memory taste profile: (N, 512) matrix + artist metadata."""
    embeddings: np.ndarray          # (N, 512) float32, rows L2-normalised
    artists: list[str]              # unique artist names (seed list)
    entry_paths: list[str]          # file path per row (audit/debug)

    @property
    def size(self) -> int:
        return int(self.embeddings.shape[0])

    def score(self, embedding: np.ndarray, k: int = 5) -> float:
        """
        Taste score = mean cosine similarity with the k nearest profile
        rows. Both sides are L2-normalised, so cosine == dot product.
        Returns a value in [-1, 1]; realistically [0, 1] for music.
        """
        if self.size == 0:
            raise ValueError("empty taste profile")
        sims = self.embeddings @ embedding.astype(np.float32)
        k = max(1, min(k, sims.shape[0]))
        top_k = np.partition(sims, -k)[-k:]
        return float(np.mean(top_k))


def load_taste_profile(data_dir: Path) -> TasteProfile | None:
    """Load profile from data_dir. Returns None if absent or corrupt."""
    npy_path = Path(data_dir) / PROFILE_NPY
    idx_path = Path(data_dir) / PROFILE_INDEX
    if not npy_path.exists() or not idx_path.exists():
        return None
    try:
        embeddings = np.load(npy_path)
        idx = json.loads(idx_path.read_text(encoding="utf-8"))
        entries = idx.get("entries", [])
        artists = idx.get("artists", [])
    except (OSError, ValueError, json.JSONDecodeError) as e:
        logger.warning("Taste profile unreadable: %s", e)
        return None
    if embeddings.ndim != 2 or embeddings.shape[0] != len(entries):
        logger.warning(
            "Taste profile out of sync (%d vectors vs %d entries) — ignoring",
            embeddings.shape[0], len(entries),
        )
        return None
    return TasteProfile(
        embeddings=embeddings.astype(np.float32),
        artists=artists,
        entry_paths=[e.get("path", "") for e in entries],
    )


def load_seed_artists(data_dir: Path) -> list[str]:
    """
    Lightweight read of the seed-artist list (no numpy needed) for
    discovery. Returns [] when the profile hasn't been built.
    """
    idx_path = Path(data_dir) / PROFILE_INDEX
    if not idx_path.exists():
        return []
    try:
        idx = json.loads(idx_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Taste profile index unreadable: %s", e)
        return []
    artists = idx.get("artists", [])
    return [a for a in artists if isinstance(a, str) and a.strip()]


def save_taste_profile(
    data_dir: Path,
    embeddings: np.ndarray,
    entries: list[dict[str, str]],
    built_at: str,
) -> None:
    """Atomic write of the profile matrix + index."""
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    npy_path = data_dir / PROFILE_NPY
    idx_path = data_dir / PROFILE_INDEX

    artists_seen: list[str] = []
    seen_norm: set[str] = set()
    for e in entries:
        norm = normalize_artist_name(e["artist"])
        if norm and norm not in seen_norm:
            seen_norm.add(norm)
            artists_seen.append(e["artist"])

    tmp_npy = npy_path.with_name(npy_path.name + ".tmp")
    with open(tmp_npy, "wb") as fh:
        np.save(fh, embeddings.astype(np.float32), allow_pickle=False)
    tmp_npy.replace(npy_path)

    tmp_idx = idx_path.with_name(idx_path.name + ".tmp")
    tmp_idx.write_text(
        json.dumps(
            {"entries": entries, "artists": artists_seen, "built_at": built_at},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    tmp_idx.replace(idx_path)
