#!/usr/bin/env python3
"""
Audit integrity of audio files on AzuraCast server.

Best practices 2026:
- Downloads and validates each file with ffprobe/ffmpeg
- Detects corrupted, truncated, or invalid audio files
- Generates detailed report with actionable recommendations
- Supports parallel processing for large libraries

Usage:
    python audit_integrity.py [--limit N] [--fix] [--parallel N]
"""

import argparse
import json
import logging
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from http_client import AzuraCastClient, ClientError, HTTPConnectionError, ServerError
from settings import get_settings, validate_environment

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


@dataclass
class AuditResult:
    """Result of a single file audit."""
    file_id: int
    path: str
    artist: str
    title: str
    status: str  # "ok", "corrupted", "truncated", "invalid", "download_failed"
    error: str = ""
    duration: float = 0.0
    size: int = 0
    codec: str = ""


@dataclass
class AuditReport:
    """Complete audit report."""
    total_files: int = 0
    ok_count: int = 0
    corrupted_count: int = 0
    truncated_count: int = 0
    invalid_count: int = 0
    download_failed_count: int = 0
    results: list[AuditResult] = field(default_factory=list)
    duration_seconds: float = 0.0

    def add_result(self, result: AuditResult) -> None:
        """Add a result to the report."""
        self.results.append(result)
        self.total_files += 1

        if result.status == "ok":
            self.ok_count += 1
        elif result.status == "corrupted":
            self.corrupted_count += 1
        elif result.status == "truncated":
            self.truncated_count += 1
        elif result.status == "invalid":
            self.invalid_count += 1
        elif result.status == "download_failed":
            self.download_failed_count += 1

    @property
    def error_count(self) -> int:
        return self.corrupted_count + self.truncated_count + self.invalid_count

    @property
    def success_rate(self) -> float:
        if self.total_files == 0:
            return 0.0
        return (self.ok_count / self.total_files) * 100


class IntegrityAuditor:
    """
    Audits audio file integrity on AzuraCast.

    Downloads files via API and validates with ffprobe/ffmpeg.
    """

    def __init__(self, client: AzuraCastClient, temp_dir: Path | None = None):
        self.client = client
        self.temp_dir = temp_dir or Path(tempfile.gettempdir()) / "azuracast_audit"
        self.temp_dir.mkdir(exist_ok=True)

    def get_all_files(self) -> list[dict[str, Any]]:
        """Fetch all files from AzuraCast."""
        try:
            return self.client.get_station_files()
        except (ClientError, ServerError, HTTPConnectionError) as e:
            logger.error(f"Failed to fetch files: {e}")
            return []

    def download_file(self, file_info: dict[str, Any]) -> Path | None:
        """
        Download a file from AzuraCast for analysis.

        Uses the media download endpoint.
        """
        file_id = file_info.get("id")
        unique_id = file_info.get("unique_id", str(file_id))

        # Determine file extension from path
        original_path = file_info.get("path", "unknown.mp3")
        ext = Path(original_path).suffix or ".mp3"

        temp_file = self.temp_dir / f"audit_{unique_id}{ext}"

        try:
            # Use the file download endpoint
            response = self.client.get(
                f"/api/station/{self.client.station_id}/file/{file_id}/download",
                timeout=120,
            )

            if response.status_code == 200:
                temp_file.write_bytes(response.content)
                return temp_file

            # Fallback: try media endpoint
            response = self.client.get(
                f"/api/station/{self.client.station_id}/file/{file_id}/media",
                timeout=120,
            )

            if response.status_code == 200:
                temp_file.write_bytes(response.content)
                return temp_file

            logger.warning(f"Download failed for {file_id}: HTTP {response.status_code}")
            return None

        except Exception as e:
            logger.debug(f"Download error for {file_id}: {e}")
            return None

    def validate_audio(self, filepath: Path) -> tuple[str, str, dict[str, Any]]:
        """
        Validate audio file integrity.

        Returns:
            Tuple of (status, error_message, metadata).
            Status: "ok", "corrupted", "truncated", "invalid"
        """
        if not filepath.exists():
            return "invalid", "File does not exist", {}

        file_size = filepath.stat().st_size
        if file_size < 1024:  # Less than 1KB
            return "truncated", f"File too small ({file_size} bytes)", {}

        metadata: dict[str, Any] = {"size": file_size}

        try:
            # Probe file metadata
            probe_cmd = [
                "ffprobe",
                "-v", "error",
                "-select_streams", "a:0",
                "-show_entries", "stream=codec_name,duration,sample_rate,channels,bit_rate",
                "-show_entries", "format=duration,size,bit_rate",
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
                return "invalid", f"ffprobe failed: {result.stderr.strip()[:100]}", metadata

            probe_data = json.loads(result.stdout)

            # Check audio stream exists
            streams = probe_data.get("streams", [])
            if not streams:
                return "invalid", "No audio stream found", metadata

            audio_stream = streams[0]
            metadata["codec"] = audio_stream.get("codec_name", "unknown")
            metadata["sample_rate"] = audio_stream.get("sample_rate")
            metadata["channels"] = audio_stream.get("channels")

            # Get duration
            duration = float(probe_data.get("format", {}).get("duration", 0) or 0)
            if duration <= 0:
                duration = float(audio_stream.get("duration", 0) or 0)

            metadata["duration"] = duration

            if duration <= 0:
                return "truncated", "Invalid duration (0 or negative)", metadata

            if duration < 10:  # Less than 10 seconds is suspicious
                return "truncated", f"Suspiciously short ({duration:.1f}s)", metadata

            # Full decode test - decode entire file to null
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
                timeout=300,  # 5 minutes max for long files
            )

            if decode_result.stderr.strip():
                errors = decode_result.stderr.strip().split("\n")
                # Filter for actual errors
                critical_errors = [
                    e for e in errors
                    if any(word in e.lower() for word in ["error", "corrupt", "invalid", "failed"])
                ]
                if critical_errors:
                    return "corrupted", "; ".join(critical_errors[:3]), metadata

            return "ok", "", metadata

        except subprocess.TimeoutExpired:
            return "corrupted", "Decode timeout - possible corruption", metadata
        except json.JSONDecodeError:
            return "invalid", "Invalid ffprobe output", metadata
        except FileNotFoundError:
            return "invalid", "ffprobe/ffmpeg not installed", metadata
        except Exception as e:
            return "corrupted", f"Validation error: {str(e)[:100]}", metadata

    def audit_file(self, file_info: dict[str, Any]) -> AuditResult:
        """Audit a single file."""
        file_id = file_info.get("id", 0)
        path = file_info.get("path", "unknown")
        artist = file_info.get("artist", "Unknown")
        title = file_info.get("title", "Unknown")

        # Download file
        temp_file = self.download_file(file_info)
        if not temp_file:
            return AuditResult(
                file_id=file_id,
                path=path,
                artist=artist,
                title=title,
                status="download_failed",
                error="Could not download file from server",
            )

        try:
            # Validate
            status, error, metadata = self.validate_audio(temp_file)

            return AuditResult(
                file_id=file_id,
                path=path,
                artist=artist,
                title=title,
                status=status,
                error=error,
                duration=metadata.get("duration", 0),
                size=metadata.get("size", 0),
                codec=metadata.get("codec", ""),
            )
        finally:
            # Cleanup temp file
            if temp_file.exists():
                temp_file.unlink()

    def run_audit(
        self,
        limit: int | None = None,
        parallel: int = 1,
    ) -> AuditReport:
        """
        Run full audit on all files.

        Args:
            limit: Maximum files to audit (None for all).
            parallel: Number of parallel workers.

        Returns:
            Complete audit report.
        """
        start_time = time.time()
        report = AuditReport()

        # Get all files
        files = self.get_all_files()
        if not files:
            logger.error("No files found or connection failed")
            return report

        if limit:
            files = files[:limit]

        total = len(files)
        logger.info(f"Auditing {total} files...")

        if parallel > 1:
            # Parallel processing
            with ThreadPoolExecutor(max_workers=parallel) as executor:
                futures = {
                    executor.submit(self.audit_file, f): f
                    for f in files
                }

                for i, future in enumerate(as_completed(futures), 1):
                    result = future.result()
                    report.add_result(result)

                    # Progress log
                    if result.status != "ok":
                        logger.warning(
                            f"[{i}/{total}] {result.status.upper()}: "
                            f"{result.artist} - {result.title} ({result.error})"
                        )
                    elif i % 10 == 0:
                        logger.info(f"[{i}/{total}] Progress: {report.ok_count} OK, {report.error_count} errors")
        else:
            # Sequential processing
            for i, file_info in enumerate(files, 1):
                result = self.audit_file(file_info)
                report.add_result(result)

                if result.status != "ok":
                    logger.warning(
                        f"[{i}/{total}] {result.status.upper()}: "
                        f"{result.artist} - {result.title} ({result.error})"
                    )
                elif i % 10 == 0:
                    logger.info(f"[{i}/{total}] Progress: {report.ok_count} OK, {report.error_count} errors")

        report.duration_seconds = time.time() - start_time
        return report

    def cleanup(self) -> None:
        """Clean up temporary files."""
        if self.temp_dir.exists():
            for f in self.temp_dir.glob("audit_*"):
                try:
                    f.unlink()
                except OSError:
                    pass


def delete_corrupted_files(client: AzuraCastClient, report: AuditReport) -> int:
    """
    Delete corrupted files from AzuraCast.

    Args:
        client: AzuraCast client.
        report: Audit report with results.

    Returns:
        Number of files deleted.
    """
    deleted = 0
    corrupted = [r for r in report.results if r.status in ("corrupted", "truncated", "invalid")]

    for result in corrupted:
        try:
            response = client.delete(f"/api/station/{client.station_id}/file/{result.file_id}")
            if response.status_code in (200, 204):
                logger.info(f"Deleted: {result.artist} - {result.title}")
                deleted += 1
            else:
                logger.warning(f"Failed to delete {result.file_id}: HTTP {response.status_code}")
        except Exception as e:
            logger.warning(f"Failed to delete {result.file_id}: {e}")

    return deleted


def print_report(report: AuditReport) -> None:
    """Print audit report summary."""
    print("\n" + "=" * 60)
    print("AUDIT REPORT - AzuraCast Library Integrity")
    print("=" * 60)

    print(f"\nTotal files audited: {report.total_files}")
    print(f"Duration: {report.duration_seconds:.1f} seconds")
    print(f"\nResults:")
    print(f"  OK:              {report.ok_count:4d} ({report.success_rate:.1f}%)")
    print(f"  Corrupted:       {report.corrupted_count:4d}")
    print(f"  Truncated:       {report.truncated_count:4d}")
    print(f"  Invalid:         {report.invalid_count:4d}")
    print(f"  Download failed: {report.download_failed_count:4d}")

    # List problematic files
    problems = [r for r in report.results if r.status != "ok"]
    if problems:
        print(f"\n{'=' * 60}")
        print("PROBLEMATIC FILES:")
        print("=" * 60)

        for r in problems:
            status_label = {
                "corrupted": "CORRUPTED",
                "truncated": "TRUNCATED",
                "invalid": "INVALID",
                "download_failed": "DL FAILED",
            }.get(r.status, r.status.upper())

            print(f"\n[{status_label}] {r.artist} - {r.title}")
            print(f"   Path: {r.path}")
            print(f"   ID: {r.file_id}")
            if r.error:
                print(f"   Error: {r.error}")

    if report.error_count == 0:
        print("\nAll files passed integrity check!")
    else:
        print(f"\nRECOMMENDATION: {report.error_count} files need attention.")
        print("Run with --fix to delete corrupted files.")


def save_report_json(report: AuditReport, output_path: Path) -> None:
    """Save report as JSON."""
    data = {
        "summary": {
            "total_files": report.total_files,
            "ok_count": report.ok_count,
            "corrupted_count": report.corrupted_count,
            "truncated_count": report.truncated_count,
            "invalid_count": report.invalid_count,
            "download_failed_count": report.download_failed_count,
            "success_rate": report.success_rate,
            "duration_seconds": report.duration_seconds,
        },
        "problems": [
            {
                "file_id": r.file_id,
                "path": r.path,
                "artist": r.artist,
                "title": r.title,
                "status": r.status,
                "error": r.error,
            }
            for r in report.results if r.status != "ok"
        ],
    }

    output_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    logger.info(f"Report saved to {output_path}")


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Audit integrity of audio files on AzuraCast"
    )
    parser.add_argument(
        "--limit", "-l",
        type=int,
        default=None,
        help="Maximum number of files to audit (default: all)",
    )
    parser.add_argument(
        "--parallel", "-p",
        type=int,
        default=1,
        help="Number of parallel workers (default: 1)",
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Delete corrupted files from server",
    )
    parser.add_argument(
        "--output", "-o",
        type=str,
        default=None,
        help="Save report to JSON file",
    )
    parser.add_argument(
        "--quiet", "-q",
        action="store_true",
        help="Reduce output verbosity",
    )

    args = parser.parse_args()

    if args.quiet:
        logging.getLogger().setLevel(logging.WARNING)

    logger.info("=== AzuraCast Library Integrity Audit ===")

    # Validate configuration
    is_valid, errors = validate_environment()
    if not is_valid:
        for error in errors:
            logger.error(f"Config error: {error}")
        return 1

    settings = get_settings()

    # Create client
    client = AzuraCastClient(
        base_url=settings.azuracast_url,
        api_key=settings.azuracast_api_key,
        station_id=settings.azuracast_station_id,
        timeout=settings.http_timeout,
    )

    # Health check
    if not client.health_check():
        logger.error("AzuraCast is not reachable. Aborting.")
        return 1

    logger.info(f"Server: {settings.azuracast_url}")

    # Run audit
    auditor = IntegrityAuditor(client)

    try:
        report = auditor.run_audit(limit=args.limit, parallel=args.parallel)
    finally:
        auditor.cleanup()

    # Print report
    print_report(report)

    # Save JSON report if requested
    if args.output:
        save_report_json(report, Path(args.output))

    # Fix corrupted files if requested
    if args.fix and report.error_count > 0:
        print(f"\nDeleting {report.error_count} corrupted files...")
        deleted = delete_corrupted_files(client, report)
        print(f"Deleted {deleted} files.")

    return 0 if report.error_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
