#!/bin/bash
# Audio Analysis Script using Essentia-TensorFlow
# Uses pre-trained MTG mood classifiers + BPM for intelligent mood detection

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIPELINE_DIR="$(dirname "$SCRIPT_DIR")"
DOWNLOAD_DIR="$PIPELINE_DIR/downloads"

echo "=== Audio Analysis (Essentia-TensorFlow) ==="

if [ ! -d "$DOWNLOAD_DIR" ] || [ -z "$(ls -A "$DOWNLOAD_DIR"/*.mp3 2>/dev/null)" ]; then
    echo "No MP3 files in downloads folder."
    exit 0
fi

# Run Python analysis script (files stay in downloads/)
python3 "$SCRIPT_DIR/analyze.py"

COUNT=$(ls -1 "$DOWNLOAD_DIR"/*.mp3 2>/dev/null | wc -l)
echo "Ready for upload: $COUNT file(s) in downloads/"
