#!/usr/bin/env python3
"""
Server-side audio integrity audit for AzuraCast.

Run this script DIRECTLY on the AzuraCast server (via SSH or docker exec).
No download needed - scans files locally for maximum speed.

Features:
- Validates all audio files with ffprobe/ffmpeg
- Detects corrupted, truncated, or invalid files
- Extracts metadata before deletion for re-download
- Generates JSON report for pipeline integration

Usage (SSH):
    scp audit_server.py user@azuracast-server:/tmp/
    ssh user@azuracast-server "python3 /tmp/audit_server.py /var/azuracast/stations/radio/media"

Usage (Docker):
    docker cp audit_server.py azuracast:/tmp/
    docker exec azuracast python3 /tmp/audit_server.py /var/azuracast/stations/radio/media

Options:
    --fix           Delete corrupted files and generate re-download list
    --output FILE   Save report to JSON file (default: audit_report.json)
    --parallel N    Number of parallel workers (default: 4)
"""

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any


@dataclass
class FileResult:
    """Result of a single file audit."""
    path: str
    filename: str
    artist: str
    title: str
    status: str  # "ok", "corrupted", "truncated", "invalid"
    error: str
    size: int
    duration: float
    codec: str
    sample_rate: int
    channels: int

    def to_dict(self) -> dict:
        return asdict(self)


class AudioAuditor:
    """Audits audio files directly on the filesystem."""

    AUDIO_EXTENSIONS = {".mp3", ".flac", ".ogg", ".m4a", ".aac", ".wav", ".opus"}

    def __init__(self, media_dir: Path, parallel: int = 4):
        self.media_dir = media_dir
        self.parallel = parallel
        self.results: list[FileResult] = []

    def find_audio_files(self) -> list[Path]:
        """Find all audio files in directory."""
        files = []
        for ext in self.AUDIO_EXTENSIONS:
            files.extend(self.media_dir.rglob(f"*{ext}"))
            files.extend(self.media_dir.rglob(f"*{ext.upper()}"))
        return sorted(set(files))

    def extract_id3_tags(self, filepath: Path) -> tuple[str, str]:
        """
        Extract artist and title from file.

        Uses ffprobe for universal format support.
        """
        try:
            cmd = [
                "ffprobe",
                "-v", "quiet",
                "-print_format", "json",
                "-show_format",
                str(filepath),
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)

            if result.returncode == 0:
                data = json.loads(result.stdout)
                tags = data.get("format", {}).get("tags", {})

                # Try different tag formats (case-insensitive)
                artist = (
                    tags.get("artist") or
                    tags.get("ARTIST") or
                    tags.get("Artist") or
                    tags.get("album_artist") or
                    "Unknown"
                )
                title = (
                    tags.get("title") or
                    tags.get("TITLE") or
                    tags.get("Title") or
                    filepath.stem
                )
                return artist, title
        except Exception:
            pass

        # Fallback: parse filename "Artist - Title.mp3"
        stem = filepath.stem
        if " - " in stem:
            parts = stem.split(" - ", 1)
            return parts[0].strip(), parts[1].strip()
        return "Unknown", stem

    def validate_file(self, filepath: Path) -> FileResult:
        """Validate a single audio file."""
        size = filepath.stat().st_size
        artist, title = self.extract_id3_tags(filepath)

        # Initialize result
        result = FileResult(
            path=str(filepath),
            filename=filepath.name,
            artist=artist,
            title=title,
            status="ok",
            error="",
            size=size,
            duration=0.0,
            codec="",
            sample_rate=0,
            channels=0,
        )

        # Check minimum size
        if size < 1024:
            result.status = "truncated"
            result.error = f"File too small ({size} bytes)"
            return result

        # Probe with ffprobe
        try:
            probe_cmd = [
                "ffprobe",
                "-v", "error",
                "-select_streams", "a:0",
                "-show_entries", "stream=codec_name,duration,sample_rate,channels",
                "-show_entries", "format=duration",
                "-of", "json",
                str(filepath),
            ]

            probe_result = subprocess.run(
                probe_cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )

            if probe_result.returncode != 0:
                result.status = "invalid"
                result.error = probe_result.stderr.strip()[:200]
                return result

            data = json.loads(probe_result.stdout)

            # Check audio stream
            streams = data.get("streams", [])
            if not streams:
                result.status = "invalid"
                result.error = "No audio stream found"
                return result

            stream = streams[0]
            result.codec = stream.get("codec_name", "unknown")
            result.sample_rate = int(stream.get("sample_rate", 0) or 0)
            result.channels = int(stream.get("channels", 0) or 0)

            # Get duration
            duration = float(data.get("format", {}).get("duration", 0) or 0)
            if duration <= 0:
                duration = float(stream.get("duration", 0) or 0)
            result.duration = duration

            if duration <= 0:
                result.status = "truncated"
                result.error = "Invalid duration"
                return result

            # Full decode test
            decode_cmd = [
                "ffmpeg",
                "-v", "error",
                "-i", str(filepath),
                "-f", "null",
                "-",
            ]

            decode_result = subprocess.run(
                decode_cmd,
                capture_output=True,
                text=True,
                timeout=300,
            )

            if decode_result.stderr.strip():
                errors = [
                    line for line in decode_result.stderr.strip().split("\n")
                    if any(w in line.lower() for w in ["error", "corrupt", "invalid", "failed"])
                ]
                if errors:
                    result.status = "corrupted"
                    result.error = "; ".join(errors[:2])
                    return result

            return result

        except subprocess.TimeoutExpired:
            result.status = "corrupted"
            result.error = "Decode timeout"
            return result
        except json.JSONDecodeError:
            result.status = "invalid"
            result.error = "Invalid probe output"
            return result
        except Exception as e:
            result.status = "corrupted"
            result.error = str(e)[:100]
            return result

    def run_audit(self) -> list[FileResult]:
        """Run audit on all files."""
        files = self.find_audio_files()
        total = len(files)

        print(f"Found {total} audio files to audit...")
        print(f"Using {self.parallel} parallel workers\n")

        start_time = time.time()
        completed = 0
        errors = 0

        with ThreadPoolExecutor(max_workers=self.parallel) as executor:
            futures = {executor.submit(self.validate_file, f): f for f in files}

            for future in as_completed(futures):
                result = future.result()
                self.results.append(result)
                completed += 1

                if result.status != "ok":
                    errors += 1
                    print(f"[{completed}/{total}] {result.status.upper()}: {result.artist} - {result.title}")
                    print(f"           Error: {result.error}")
                elif completed % 50 == 0:
                    print(f"[{completed}/{total}] Progress... ({errors} errors so far)")

        elapsed = time.time() - start_time
        print(f"\nCompleted in {elapsed:.1f}s ({total/elapsed:.1f} files/sec)")

        return self.results


def generate_report(results: list[FileResult]) -> dict:
    """Generate summary report."""
    ok = [r for r in results if r.status == "ok"]
    corrupted = [r for r in results if r.status == "corrupted"]
    truncated = [r for r in results if r.status == "truncated"]
    invalid = [r for r in results if r.status == "invalid"]

    return {
        "summary": {
            "total_files": len(results),
            "ok_count": len(ok),
            "corrupted_count": len(corrupted),
            "truncated_count": len(truncated),
            "invalid_count": len(invalid),
            "success_rate": round(len(ok) / len(results) * 100, 1) if results else 0,
        },
        "corrupted_files": [r.to_dict() for r in corrupted],
        "truncated_files": [r.to_dict() for r in truncated],
        "invalid_files": [r.to_dict() for r in invalid],
        "redownload_list": [
            {"artist": r.artist, "title": r.title, "search": f"{r.artist} - {r.title}"}
            for r in corrupted + truncated + invalid
        ],
    }


def print_report(report: dict) -> None:
    """Print report to console."""
    s = report["summary"]

    print("\n" + "=" * 60)
    print("AUDIT REPORT")
    print("=" * 60)
    print(f"Total files:     {s['total_files']}")
    print(f"OK:              {s['ok_count']} ({s['success_rate']}%)")
    print(f"Corrupted:       {s['corrupted_count']}")
    print(f"Truncated:       {s['truncated_count']}")
    print(f"Invalid:         {s['invalid_count']}")

    problem_count = s['corrupted_count'] + s['truncated_count'] + s['invalid_count']

    if problem_count > 0:
        print("\n" + "-" * 60)
        print("PROBLEMATIC FILES:")
        print("-" * 60)

        for category in ["corrupted_files", "truncated_files", "invalid_files"]:
            for f in report[category]:
                print(f"\n[{f['status'].upper()}] {f['artist']} - {f['title']}")
                print(f"   File: {f['filename']}")
                print(f"   Error: {f['error']}")
    else:
        print("\nAll files passed integrity check!")


def delete_corrupted(report: dict) -> int:
    """Delete corrupted files."""
    deleted = 0

    for category in ["corrupted_files", "truncated_files", "invalid_files"]:
        for f in report[category]:
            path = Path(f["path"])
            try:
                if path.exists():
                    path.unlink()
                    print(f"Deleted: {f['filename']}")
                    deleted += 1
            except Exception as e:
                print(f"Failed to delete {f['filename']}: {e}")

    return deleted


def main():
    parser = argparse.ArgumentParser(
        description="Audit audio file integrity on AzuraCast server"
    )
    parser.add_argument(
        "media_dir",
        type=str,
        help="Path to media directory (e.g., /var/azuracast/stations/radio/media)",
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Delete corrupted files and generate re-download list",
    )
    parser.add_argument(
        "--output", "-o",
        type=str,
        default="audit_report.json",
        help="Output JSON report file (default: audit_report.json)",
    )
    parser.add_argument(
        "--parallel", "-p",
        type=int,
        default=4,
        help="Number of parallel workers (default: 4)",
    )
    parser.add_argument(
        "--redownload-file",
        type=str,
        default="tracks-to-redownload.json",
        help="Output file for tracks to re-download (default: tracks-to-redownload.json)",
    )

    args = parser.parse_args()

    media_dir = Path(args.media_dir)
    if not media_dir.exists():
        print(f"Error: Directory not found: {media_dir}")
        sys.exit(1)

    print("=" * 60)
    print("AZURACAST AUDIO INTEGRITY AUDIT")
    print("=" * 60)
    print(f"Media directory: {media_dir}")
    print(f"Fix mode: {'ON' if args.fix else 'OFF'}")
    print()

    # Run audit
    auditor = AudioAuditor(media_dir, parallel=args.parallel)
    results = auditor.run_audit()

    if not results:
        print("No audio files found!")
        sys.exit(0)

    # Generate report
    report = generate_report(results)
    print_report(report)

    # Save JSON report
    output_path = Path(args.output)
    output_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"\nReport saved to: {output_path}")

    # Handle fix mode
    problem_count = (
        report["summary"]["corrupted_count"] +
        report["summary"]["truncated_count"] +
        report["summary"]["invalid_count"]
    )

    if problem_count > 0:
        # Always save re-download list
        redownload_path = Path(args.redownload_file)
        redownload_path.write_text(
            json.dumps(report["redownload_list"], indent=2, ensure_ascii=False)
        )
        print(f"Re-download list saved to: {redownload_path}")

        if args.fix:
            print(f"\nDeleting {problem_count} corrupted files...")
            deleted = delete_corrupted(report)
            print(f"Deleted {deleted} files.")
            print(f"\nCopy {redownload_path} to your pipeline server to re-download.")
        else:
            print(f"\nRun with --fix to delete corrupted files.")

    return 0 if problem_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
