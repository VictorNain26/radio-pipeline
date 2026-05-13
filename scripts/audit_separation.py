#!/usr/bin/env python3
"""
Read-only audit of the AzuraCast AutoDJ duplicate-prevention configuration.

config.py declares SEPARATION rules (artist_min_minutes, title_min_minutes,
mood_min_separation, ...). These rules are NOT applied by the Python
pipeline — they must be reflected by the AzuraCast AutoDJ scheduler.

AzuraCast (Liquidsoap backend) only exposes one native field for this:
    backend_config.duplicate_prevention_time_range  (minutes)

It is a single value used for both same-artist and same-title prevention.
Finer rules (mood, genre, tempo variance) require custom Liquidsoap
scripting and are out of scope of this audit.

This script is READ-ONLY. It never PATCHes the station — it just compares
the live AzuraCast config to config.SEPARATION and prints a diff.

Usage:
    python scripts/audit_separation.py
    python scripts/audit_separation.py --json   # machine-readable output
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from http_client import (  # noqa: E402
    AzuraCastClient,
    ClientError,
    HTTPConnectionError,
    ServerError,
)
from settings import get_settings, validate_environment  # noqa: E402

try:
    from config import SEPARATION  # noqa: E402
except ImportError as e:
    print(f"Cannot import config.SEPARATION: {e}", file=sys.stderr)
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


def fetch_station_config(client: AzuraCastClient) -> dict[str, Any]:
    """GET /api/admin/station/{id}. Requires admin-scoped API key."""
    resp = client.get(f"/api/admin/station/{client.station_id}")
    return resp.json()


def audit(station_cfg: dict[str, Any]) -> dict[str, Any]:
    """Compare config.SEPARATION with the live AzuraCast backend_config."""
    backend = station_cfg.get("backend_config") or {}
    live_dup = backend.get("duplicate_prevention_time_range")

    findings: list[dict[str, str]] = []

    # AzuraCast has a single duplicate_prevention_time_range. Compare it to
    # the strictest of artist_min_minutes / title_min_minutes (whichever is
    # higher) — that's the effective "no repeat within X min" rule.
    python_title = SEPARATION.title_min_minutes
    python_artist = SEPARATION.artist_min_minutes
    expected = max(python_title, python_artist)

    if live_dup is None:
        findings.append({
            "level": "WARN",
            "field": "backend_config.duplicate_prevention_time_range",
            "message": "Not set on AzuraCast — duplicates may slip through.",
            "suggestion": f"Set to {expected} (minutes) to match config.SEPARATION.",
        })
    elif int(live_dup) < python_title:
        findings.append({
            "level": "WARN",
            "field": "backend_config.duplicate_prevention_time_range",
            "message": (
                f"AzuraCast = {live_dup}min, but config.SEPARATION.title_min_minutes = "
                f"{python_title}min. Same title may repeat too soon."
            ),
            "suggestion": f"PATCH /api/admin/station/{{id}} with duplicate_prevention_time_range={python_title}.",
        })
    elif int(live_dup) < python_artist:
        findings.append({
            "level": "INFO",
            "field": "backend_config.duplicate_prevention_time_range",
            "message": (
                f"AzuraCast = {live_dup}min, artist_min_minutes = {python_artist}min. "
                f"Same artist may repeat too soon."
            ),
            "suggestion": f"Consider setting duplicate_prevention_time_range >= {python_artist}.",
        })
    else:
        findings.append({
            "level": "OK",
            "field": "backend_config.duplicate_prevention_time_range",
            "message": f"AzuraCast = {live_dup}min covers SEPARATION (artist={python_artist}, title={python_title}).",
            "suggestion": "",
        })

    # Rules AzuraCast cannot enforce natively
    not_enforceable = []
    if SEPARATION.mood_min_separation:
        not_enforceable.append(f"mood_min_separation={SEPARATION.mood_min_separation}")
    if SEPARATION.genre_min_separation:
        not_enforceable.append(f"genre_min_separation={SEPARATION.genre_min_separation}")
    if SEPARATION.tempo_max_variance:
        not_enforceable.append(f"tempo_max_variance={SEPARATION.tempo_max_variance}")
    if SEPARATION.energy_smooth_transition:
        not_enforceable.append("energy_smooth_transition=True")

    if not_enforceable:
        findings.append({
            "level": "INFO",
            "field": "config.SEPARATION (advanced rules)",
            "message": (
                "These rules are documented in config.py but NOT enforceable "
                "by AzuraCast natively: " + ", ".join(not_enforceable)
            ),
            "suggestion": (
                "Either accept they are documentation only, or write a custom "
                "Liquidsoap script (advanced)."
            ),
        })

    return {
        "live": {
            "duplicate_prevention_time_range": live_dup,
            "autodj_queue_length": backend.get("autodj_queue_length"),
            "backend_type": station_cfg.get("backend_type"),
        },
        "python_separation": {
            "artist_min_minutes": SEPARATION.artist_min_minutes,
            "title_min_minutes": SEPARATION.title_min_minutes,
            "mood_min_separation": SEPARATION.mood_min_separation,
            "genre_min_separation": SEPARATION.genre_min_separation,
            "tempo_max_variance": SEPARATION.tempo_max_variance,
            "energy_smooth_transition": SEPARATION.energy_smooth_transition,
        },
        "findings": findings,
    }


def _print_human(result: dict[str, Any]) -> None:
    print("=== AzuraCast AutoDJ separation audit ===\n")
    live = result["live"]
    print(f"Backend            : {live.get('backend_type')}")
    print(f"duplicate_prevention_time_range : {live.get('duplicate_prevention_time_range')} min")
    print(f"autodj_queue_length             : {live.get('autodj_queue_length')}")
    print()
    print("Python SEPARATION (config.py) :")
    for k, v in result["python_separation"].items():
        print(f"  {k:30s} = {v}")
    print()
    print("Findings :")
    for f in result["findings"]:
        marker = {"OK": "✓", "INFO": "·", "WARN": "!"}.get(f["level"], "?")
        print(f"  {marker} [{f['level']}] {f['field']}")
        print(f"      {f['message']}")
        if f["suggestion"]:
            print(f"      → {f['suggestion']}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Machine-readable JSON output")
    args = parser.parse_args()

    is_valid, errors = validate_environment()
    if not is_valid:
        for e in errors:
            print(f"Config error: {e}", file=sys.stderr)
        return 1

    settings = get_settings()
    client = AzuraCastClient(
        base_url=settings.azuracast_url,
        api_key=settings.azuracast_api_key,
        station_id=settings.azuracast_station_id,
        timeout=settings.http_timeout,
    )

    try:
        station_cfg = fetch_station_config(client)
    except (ClientError, ServerError, HTTPConnectionError) as e:
        print(f"AzuraCast call failed: {e}", file=sys.stderr)
        print(
            "Hint: the API key needs admin scope to read /api/admin/station/{id}. "
            "Check the key permissions in AzuraCast > Profile > API Keys.",
            file=sys.stderr,
        )
        return 1

    result = audit(station_cfg)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        _print_human(result)

    # Exit non-zero only on hard WARN findings.
    has_warn = any(f["level"] == "WARN" for f in result["findings"])
    return 1 if has_warn else 0


if __name__ == "__main__":
    sys.exit(main())
