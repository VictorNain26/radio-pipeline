"""
Audio fingerprinting via Chromaprint (fpcalc) — content-based dedup.

The previous artist/title dedup catches obvious string duplicates but
misses re-uploads of the same recording under different metadata
(remasters, label re-releases, "feat." rewrites). Chromaprint hashes
the *audio waveform* (12 pitch classes × 8 fps × ~120 s of audio),
which makes it robust to any metadata change.

Strategy here:
- `compute_fingerprint(path)` runs `fpcalc -json`, returns
  (fingerprint_b64, duration_seconds).
- `fingerprint_hash(fp)` shortens it to a 20-char SHA-1 prefix that we
  store in SQLite (`audio_fingerprints.fingerprint_hash`, indexed).
- Lookup is **exact-match on the hash**. Two re-encodings of the same
  YouTube source typically produce identical fingerprints (Chromaprint
  is designed to be insensitive to bitrate/format), so the hash will
  match. Re-recordings or live versions won't match — that's the right
  behaviour: they're different performances.

Future: AcoustID web API for fuzzy similarity across different
recordings — out of scope here, the local exact-match path covers the
common case at zero API cost.
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


def compute_fingerprint(filepath: Path, timeout: int = 30) -> tuple[str, int] | None:
    """
    Run `fpcalc -json` and return (fingerprint, duration_seconds).

    Returns None on any failure (missing binary, decode error, timeout).
    """
    if not Path(filepath).exists():
        return None
    try:
        result = subprocess.run(
            ["fpcalc", "-json", str(filepath)],
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        logger.warning("fpcalc unavailable or timed out: %s", e)
        return None
    if result.returncode != 0:
        logger.warning("fpcalc rc=%d: %s", result.returncode, result.stderr[:200])
        return None
    try:
        data = json.loads(result.stdout)
        fp = data["fingerprint"]
        dur = int(float(data["duration"]))
        if not fp:
            return None
        return fp, dur
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        logger.warning("fpcalc parse error: %s", e)
        return None


def fingerprint_hash(fingerprint: str) -> str:
    """20-char hex prefix of SHA-1 — short enough for an SQLite index."""
    return hashlib.sha1(fingerprint.encode("ascii")).hexdigest()[:20]
