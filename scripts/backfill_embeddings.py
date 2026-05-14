#!/usr/bin/env python3
"""
Backfill CLAP audio embeddings for the existing AzuraCast library.

For each track in AzuraCast that does not yet have an embedding in
data/embeddings.npy:
  1. SCP the source MP3 from the AzuraCast server to a temp file.
  2. Compute the 512-dim CLAP embedding via scripts/audio_embeddings.py.
  3. Persist to data/embeddings.{npy,_index.json}.
  4. Delete the temp file.

Resumable — re-running picks up where the previous run stopped.
Idempotent — already-embedded tracks are skipped.

Typical runtime: ~2 s/track over LAN (1 s SCP + 0.6 s CLAP inference).
600 tracks ≈ 20 minutes. Use --limit during testing.

Usage:
    python3 scripts/backfill_embeddings.py --host root@116.203.46.203 \
        --media-dir /var/azuracast/stations/aubesonore/media \
        [--limit N] [--dry-run]
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from audio_embeddings import EmbeddingStore, compute_embedding  # noqa: E402
from http_client import AzuraCastClient  # noqa: E402
from settings import get_settings, validate_environment  # noqa: E402
from track_db import normalize_track_key  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="root@116.203.46.203",
                        help="SSH target for the AzuraCast server")
    parser.add_argument("--container", default="azuracast",
                        help="Docker container name on the AzuraCast host")
    parser.add_argument("--media-dir", default="/var/azuracast/stations/aubesonore/media",
                        help="Absolute path of the media directory INSIDE the container")
    parser.add_argument("--limit", type=int, default=0, help="Process at most N tracks (0 = no cap)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    ok, errs = validate_environment()
    if not ok:
        for e in errs:
            logger.error("Config: %s", e)
        return 1

    settings = get_settings()
    client = AzuraCastClient(
        base_url=settings.azuracast_url,
        api_key=settings.azuracast_api_key,
        station_id=settings.azuracast_station_id,
        timeout=settings.http_timeout,
    )

    logger.info("Fetching AzuraCast file list…")
    files = client.get_station_files()
    logger.info("AzuraCast library: %d files", len(files))

    store = EmbeddingStore(Path(__file__).parent.parent / "data")
    already = sum(1 for f in files if store.has(normalize_track_key(
        (f.get("artist") or ""), (f.get("title") or ""),
    )))
    logger.info("Embeddings already cached: %d", already)

    todo = [
        f for f in files
        if not store.has(normalize_track_key(
            (f.get("artist") or ""), (f.get("title") or ""),
        ))
    ]
    logger.info("To process: %d", len(todo))

    if args.limit:
        todo = todo[: args.limit]
        logger.info("Capped to --limit %d", args.limit)

    if args.dry_run:
        for f in todo[:20]:
            logger.info("  %s — %s", f.get("artist", "?"), f.get("title", "?"))
        if len(todo) > 20:
            logger.info("  … and %d more", len(todo) - 20)
        return 0

    done, failed, t0 = 0, 0, time.time()
    for i, f in enumerate(todo, 1):
        artist = (f.get("artist") or "").strip()
        title = (f.get("title") or "").strip()
        if not artist or not title:
            continue
        track_key = normalize_track_key(artist, title)
        rel_path = f.get("path", "")
        if not rel_path:
            logger.warning("  [%d/%d] missing 'path' for %s - %s", i, len(todo), artist, title)
            continue

        # AzuraCast stores its media INSIDE the Docker container, not on
        # the host filesystem. We stream the file out via:
        #   ssh root@host "docker exec <container> cat <path>" > tmp.mp3
        # This avoids docker cp's tar overhead and keeps temp-disk
        # footprint to a single file at a time.
        remote_path = f"{args.media_dir.rstrip('/')}/{rel_path}"
        # Escape single quotes for the remote shell ($-quoting protects every char except ')
        escaped_path = "'" + remote_path.replace("'", "'\\''") + "'"
        ssh_cmd = [
            "ssh", "-o", "StrictHostKeyChecking=accept-new", args.host,
            f"docker exec {args.container} cat -- {escaped_path}",
        ]
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp_path = Path(tmp.name)

        try:
            with tmp_path.open("wb") as out:
                ssh_res = subprocess.run(
                    ssh_cmd, stdout=out, stderr=subprocess.PIPE, timeout=60,
                )
            if ssh_res.returncode != 0 or tmp_path.stat().st_size < 1024:
                stderr_text = (ssh_res.stderr.decode("utf-8", errors="replace")
                               if ssh_res.stderr else "")
                logger.warning("  [%d/%d] fetch failed: %s",
                               i, len(todo), stderr_text[:160] or f"size={tmp_path.stat().st_size}")
                failed += 1
                continue

            emb = compute_embedding(tmp_path)
            if emb is None:
                logger.warning("  [%d/%d] embedding failed for %s", i, len(todo), track_key)
                failed += 1
                continue

            store.add(track_key, emb)
            done += 1

            # Progress every 10 tracks
            if done % 10 == 0:
                rate = done / max(1.0, time.time() - t0)
                eta = (len(todo) - i) / rate if rate > 0 else 0
                logger.info(
                    "  [%d/%d] done=%d failed=%d  rate=%.1f/s  eta≈%dm",
                    i, len(todo), done, failed, rate, int(eta // 60),
                )
        finally:
            tmp_path.unlink(missing_ok=True)

    logger.info("=== Backfill done ===")
    logger.info("Processed: %d  failed: %d  total time: %.1fs", done, failed, time.time() - t0)
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
