#!/bin/bash
# =============================================================================
# AubeSonore Radio Pipeline
# Multi-source discovery → yt-dlp → Essentia/MTG → AzuraCast
#
# Features:
# - Lock file to prevent concurrent execution
# - Health check before starting
# - Proper error handling and logging
# - Automatic cleanup on exit
# =============================================================================

set -euo pipefail

# Restrict file permissions: owner rw, group/other none
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Configuration
LOCK_FILE="/tmp/radio-pipeline.lock"
LOCK_ACQUIRED=0
LOG_FILE="$SCRIPT_DIR/pipeline.log"
STATS_FILE="$SCRIPT_DIR/data/pipeline_stats.json"

# ntfy.sh configuration (free push notifications)
# Set NTFY_TOPIC in .env to enable (e.g. NTFY_TOPIC=aubesonore-pipeline)
# Notifications sent to https://ntfy.sh/$NTFY_TOPIC
NTFY_URL="${NTFY_URL:-https://ntfy.sh}"

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
log() {
    local level="$1"
    shift
    local timestamp
    timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[$timestamp] [$level] $*" | tee -a "$LOG_FILE"
}

log_info() { log "INFO" "$@"; }
log_warn() { log "WARN" "$@"; }
log_error() { log "ERROR" "$@"; }

# -----------------------------------------------------------------------------
# Notifications (ntfy.sh - free push notifications)
# -----------------------------------------------------------------------------
notify() {
    local title="$1"
    local message="$2"
    local priority="${3:-default}"
    local tags="${4:-}"

    # Only send if NTFY_TOPIC is configured
    if [ -z "${NTFY_TOPIC:-}" ]; then
        return 0
    fi

    curl -s \
        -H "Title: $title" \
        -H "Priority: $priority" \
        -H "Tags: $tags" \
        -d "$message" \
        "$NTFY_URL/$NTFY_TOPIC" >/dev/null 2>&1 || true
}

write_stats() {
    local status="$1"
    local downloads="${2:-0}"
    local uploads="${3:-0}"
    local errors="${4:-}"

    mkdir -p "$(dirname "$STATS_FILE")"
    python3 - "$status" "$downloads" "$uploads" "$errors" "$STATS_FILE" "$SCRIPT_DIR/data" <<'PYEOF'
import json, sys, time
from datetime import datetime
from pathlib import Path

status, downloads, uploads, errors, stats_file, data_dir = sys.argv[1:7]
data_path = Path(data_dir)

def _read(name):
    try:
        return json.loads((data_path / name).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None

discover = _read("last_discover_stats.json")
dl_breakdown = _read("last_download_stats.json")
analyze_breakdown = _read("last_analyze_stats.json")

stats = {
    "timestamp": datetime.now().isoformat(),
    "epoch": time.time(),
    "status": status,
    "downloads": int(downloads),
    "uploads": int(uploads),
    "errors": errors or None,
    "discover": discover,                # raw_total, deduped_total, per_source
    "download_breakdown": dl_breakdown,    # downloaded/skipped/.../loudnorm_failed
    "analyze_breakdown": analyze_breakdown, # analyzed_ok/rejected_speech/clap_succeeded/clap_cached/clap_failed
}
try:
    with open(stats_file) as f:
        history = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    history = []
history.append(stats)
history = history[-30:]
with open(stats_file, "w") as f:
    json.dump(history, f, indent=2)
PYEOF
}

# -----------------------------------------------------------------------------
# Cleanup function
# -----------------------------------------------------------------------------
cleanup() {
    local exit_code=$?

    # If we never acquired the lock, another instance is running: touching
    # temp dirs, stats or notifications here would sabotage that instance.
    # The lock file itself is never deleted — flock on the fd is the lock;
    # deleting the file would let a third instance lock a fresh inode while
    # the first still holds the old one (two concurrent pipelines).
    if [ "${LOCK_ACQUIRED:-0}" -ne 1 ]; then
        exit $exit_code
    fi

    # Clean all temp directories
    for d in "$SCRIPT_DIR/temp" "$SCRIPT_DIR/temp_reanalyze" "$SCRIPT_DIR/temp_reanalyze_server" "$SCRIPT_DIR/temp_playlist_setup"; do
        if [ -d "$d" ]; then
            rm -rf "$d"
        fi
    done

    if [ $exit_code -ne 0 ]; then
        log_error "Pipeline failed with exit code $exit_code"
        write_stats "failed" 0 0 "Exit code $exit_code"
        notify "AubeSonore Pipeline FAILED" "Pipeline crashed with exit code $exit_code. Check logs." "urgent" "rotating_light"
    fi

    exit $exit_code
}

trap cleanup EXIT

# -----------------------------------------------------------------------------
# Lock file management (atomic via flock)
# -----------------------------------------------------------------------------
acquire_lock() {
    # Use flock for atomic locking (avoids TOCTOU race condition)
    exec 200>"$LOCK_FILE"
    if ! flock -n 200; then
        log_error "Pipeline already running (lock held by another process)"
        exit 1
    fi
    # Write PID for info (lock is held via fd 200)
    echo $$ >&200
    LOCK_ACQUIRED=1
    log_info "Lock acquired (PID: $$)"
}

# -----------------------------------------------------------------------------
# Health checks
# -----------------------------------------------------------------------------
check_dependencies() {
    local missing=()

    for cmd in python3 curl ffprobe ffmpeg yt-dlp; do
        if ! command -v "$cmd" &>/dev/null; then
            missing+=("$cmd")
        fi
    done

    if [ ${#missing[@]} -gt 0 ]; then
        log_error "Missing dependencies: ${missing[*]}"
        exit 1
    fi
}

check_azuracast() {
    log_info "Checking AzuraCast connectivity..."

    # Use Python for reliable HTTPS health check
    if ! python3 -c "
import sys
sys.path.insert(0, '$SCRIPT_DIR/scripts')
from settings import get_settings, validate_environment
from http_client import AzuraCastClient

is_valid, errors = validate_environment()
if not is_valid:
    for e in errors:
        print(f'Config error: {e}')
    sys.exit(1)

settings = get_settings()
client = AzuraCastClient(
    base_url=settings.azuracast_url,
    api_key=settings.azuracast_api_key,
    station_id=settings.azuracast_station_id,
    timeout=settings.http_timeout,
)

if not client.health_check():
    print('AzuraCast health check failed')
    sys.exit(1)

print('AzuraCast OK')
" 2>&1; then
        log_error "AzuraCast health check failed"
        exit 1
    fi
}

# -----------------------------------------------------------------------------
# Main pipeline
# -----------------------------------------------------------------------------
main() {
    echo ""
    echo "╔═══════════════════════════════════════════════════════════════╗"
    echo "║           AUBESONORE RADIO PIPELINE                           ║"
    echo "║    Multi-source → yt-dlp → Essentia/MTG → AzuraCast           ║"
    echo "╚═══════════════════════════════════════════════════════════════╝"
    echo ""

    log_info "Pipeline started"

    # Load .env FIRST so ntfy notifications work for every failure below
    # (NTFY_TOPIC used to be unset until this point, making early failures
    # silently unnotified).
    if [ ! -f ".env" ]; then
        log_error ".env file not found"
        exit 1
    fi
    set -a
    # shellcheck source=/dev/null
    source .env
    set +a

    # Acquire exclusive lock
    acquire_lock

    # Check dependencies
    check_dependencies

    # Verify required keys
    : "${AZURACAST_API_KEY:?Error: AZURACAST_API_KEY not set}"
    : "${AZURACAST_URL:?Error: AZURACAST_URL not set}"

    log_info "AzuraCast: $AZURACAST_URL"

    # Health check
    check_azuracast

    # Step 1: Discover tracks (multi-source : HypeMachine + RSS + Last.fm + manual picks)
    echo ""
    echo "┌─────────────────────────────────────────────────────────────────┐"
    echo "│ STEP 1/4: DISCOVER (multi-source)                              │"
    echo "└─────────────────────────────────────────────────────────────────┘"

    if ! python3 scripts/discover.py; then
        log_error "Discover step failed"
        exit 1
    fi

    DOWNLOAD_COUNT=0
    HAS_TRACKS=false
    if [ -f "tracks-to-download.json" ]; then
        HAS_TRACKS=true
    else
        log_info "No tracks-to-download.json (all sources empty)"
    fi

    # Step 2: Download (yt-dlp, multi-quality-gates: AcoustID dedup + speech filter + loudnorm)
    if [ "$HAS_TRACKS" = true ]; then
        echo ""
        echo "┌─────────────────────────────────────────────────────────────────┐"
        echo "│ STEP 2/4: DOWNLOAD (yt-dlp + quality gates)                    │"
        echo "└─────────────────────────────────────────────────────────────────┘"

        if ! python3 scripts/download.py; then
            log_warn "Download step failed, continuing for rotation..."
        fi
    fi

    # New downloads this run — from stats JSON (single source of truth),
    # not a directory listing that also counts retries kept by classify.
    DOWNLOAD_COUNT=$(python3 -c "
import json
try:
    print(json.load(open('data/last_download_stats.json')).get('downloaded', 0))
except Exception:
    print(0)
" 2>/dev/null || echo 0)

    # Files awaiting analysis/upload — includes retries from failed runs,
    # so this runs even when today's discovery produced nothing new.
    FILES_TO_PROCESS=$(find downloads -name "*.mp3" 2>/dev/null | wc -l)
    log_info "Downloaded $DOWNLOAD_COUNT new files ($FILES_TO_PROCESS to process)"

    # Step 3: Analyze (Essentia + optional CLAP)
    if [ "$FILES_TO_PROCESS" -gt 0 ]; then
        echo ""
        echo "┌─────────────────────────────────────────────────────────────────┐"
        echo "│ STEP 3/4: ANALYZE (Essentia/MTG + CLAP)                        │"
        echo "└─────────────────────────────────────────────────────────────────┘"

        if ! python3 scripts/analyze.py; then
            log_warn "Analyze step had issues, continuing..."
        fi
    fi

    # Step 4: Classify and Upload to AzuraCast (ALWAYS runs for rotation)
    echo ""
    echo "┌─────────────────────────────────────────────────────────────────┐"
    echo "│ STEP 4/4: CLASSIFY + UPLOAD + ROTATE                           │"
    echo "└─────────────────────────────────────────────────────────────────┘"

    if ! python3 scripts/classify.py; then
        log_error "Upload step failed"
        exit 1
    fi

    echo ""
    echo "╔═══════════════════════════════════════════════════════════════╗"
    echo "║                    PIPELINE COMPLETE                          ║"
    echo "╚═══════════════════════════════════════════════════════════════╝"

    # Upload count as written by classify.py (single source of truth).
    UPLOAD_COUNT=$(cat data/last_upload_count.txt 2>/dev/null || echo 0)

    write_stats "success" "${DOWNLOAD_COUNT:-0}" "${UPLOAD_COUNT:-0}"

    # Recap quotidien WhatsApp (CallMeBot) — best-effort, jamais bloquant.
    python3 scripts/send_daily_recap.py || log_warn "Recap WhatsApp failed (non-blocking)"

    # Silent-fallback regression alerts.
    # These counters were added 2026-05-14 after a 37/37 silent loudnorm
    # failure that uploaded un-normalised audio to the radio. Any non-zero
    # loudnorm_failed → urgent ntfy. fingerprint_failed >2 → default ntfy
    # (small numbers are normal — SC extractor flakiness).
    LOUDNORM_FAIL=$(python3 -c "
import json
try:
    print(json.load(open('data/last_download_stats.json')).get('loudnorm_failed', 0))
except Exception:
    print(0)
" 2>/dev/null || echo 0)
    FINGERPRINT_FAIL=$(python3 -c "
import json
try:
    print(json.load(open('data/last_download_stats.json')).get('fingerprint_failed', 0))
except Exception:
    print(0)
" 2>/dev/null || echo 0)
    CLAP_FAIL=$(python3 -c "
import json
try:
    print(json.load(open('data/last_analyze_stats.json')).get('clap_failed', 0))
except Exception:
    print(0)
" 2>/dev/null || echo 0)

    if [ "${LOUDNORM_FAIL:-0}" -gt 0 ]; then
        log_warn "Loudnorm regression: $LOUDNORM_FAIL tracks uploaded un-normalised"
        notify "AubeSonore: loudnorm regression" \
            "$LOUDNORM_FAIL tracks uploaded un-normalised (ffmpeg loudnorm failed). Check cron.log for 'loudnorm failed'." \
            "urgent" "warning"
    fi

    if [ "${FINGERPRINT_FAIL:-0}" -gt 2 ]; then
        log_warn "Fingerprint failures: $FINGERPRINT_FAIL (>2 unusual — check fpcalc)"
        notify "AubeSonore: fingerprint failures" \
            "$FINGERPRINT_FAIL Chromaprint fingerprint failures. Dedup is skipped for those tracks. Check fpcalc availability." \
            "default" "warning"
    fi

    if [ "${CLAP_FAIL:-0}" -gt 0 ]; then
        log_warn "CLAP embedding failures: $CLAP_FAIL (non-blocking, smart_queue coverage drops)"
    fi

    notify "AubeSonore Pipeline OK" \
        "Pipeline OK. DL:${DOWNLOAD_COUNT:-0} UL:${UPLOAD_COUNT:-0} loudnorm_fail:${LOUDNORM_FAIL:-0} fp_fail:${FINGERPRINT_FAIL:-0}" \
        "low" "white_check_mark"

    log_info "Pipeline completed successfully"
}

# Run main
main "$@"
