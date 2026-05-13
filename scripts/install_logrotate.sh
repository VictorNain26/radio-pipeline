#!/bin/bash
# Install the logrotate config (requires sudo).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$SCRIPT_DIR/logrotate.conf"
DEST="/etc/logrotate.d/radio-pipeline"

if [ ! -f "$SRC" ]; then
    echo "Missing $SRC" >&2
    exit 1
fi

echo "Installing $SRC -> $DEST"
sudo install -m 0644 "$SRC" "$DEST"
echo "Done. Dry-run check:"
sudo logrotate -d "$DEST"
