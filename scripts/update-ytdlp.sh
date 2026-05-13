#!/bin/bash
# =============================================================================
# yt-dlp Auto-Updater - Best Practices 2026
# Downloads latest binary from GitHub releases
# =============================================================================

set -euo pipefail

LOG_FILE="/home/victormoi/radio-pipeline/ytdlp-update.log"
YTDLP_PATH="/home/victormoi/.local/bin/yt-dlp"
DOWNLOAD_URL="https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

# Get current version
CURRENT_VERSION=$("$YTDLP_PATH" --version 2>/dev/null || echo "unknown")
log "Current version: $CURRENT_VERSION"

# Check latest version from GitHub API
LATEST_VERSION=$(curl -sI "$DOWNLOAD_URL" 2>/dev/null | grep -i "^location:" | grep -oP 'download/\K[^/]+' || echo "unknown")

if [ "$LATEST_VERSION" = "unknown" ]; then
    log "ERROR: Could not determine latest version"
    exit 1
fi

log "Latest version: $LATEST_VERSION"

# Compare versions
if [ "$CURRENT_VERSION" = "$LATEST_VERSION" ]; then
    log "Already up to date"
    exit 0
fi

log "Updating yt-dlp..."

# Download new version
TEMP_FILE=$(mktemp)
if curl -L "$DOWNLOAD_URL" -o "$TEMP_FILE" 2>/dev/null; then
    chmod +x "$TEMP_FILE"

    # Verify the download works
    if "$TEMP_FILE" --version &>/dev/null; then
        mv "$TEMP_FILE" "$YTDLP_PATH"
        NEW_VERSION=$("$YTDLP_PATH" --version)
        log "SUCCESS: Updated to $NEW_VERSION"
    else
        rm -f "$TEMP_FILE"
        log "ERROR: Downloaded binary is invalid"
        exit 1
    fi
else
    rm -f "$TEMP_FILE"
    log "ERROR: Download failed"
    exit 1
fi
