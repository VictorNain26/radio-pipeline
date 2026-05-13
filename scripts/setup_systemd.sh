#!/bin/bash
# Install (or refresh) the user-scoped systemd units that schedule the
# AubeSonore radio pipeline.
#
#   - radio-pipeline.timer        : runs the full pipeline daily at 03:00
#   - radio-pipeline-ytdlp.timer  : refreshes yt-dlp weekly on Sundays 02:00
#
# The units live under ~/.config/systemd/user/ (no sudo required). They
# are templated with @HOME@ and @PIPELINE_DIR@ placeholders so the repo
# stays portable across users / paths.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIPELINE_DIR="$(dirname "$SCRIPT_DIR")"
TEMPLATE_DIR="$SCRIPT_DIR/systemd"
USER_UNITS="${HOME}/.config/systemd/user"

mkdir -p "$USER_UNITS"

UNITS=(
    radio-pipeline.service
    radio-pipeline.timer
    radio-pipeline-ytdlp.service
    radio-pipeline-ytdlp.timer
)

echo "Installing user-scoped systemd units to $USER_UNITS ..."
for unit in "${UNITS[@]}"; do
    src="$TEMPLATE_DIR/$unit"
    dest="$USER_UNITS/$unit"
    if [ ! -f "$src" ]; then
        echo "  MISSING template: $src" >&2
        exit 1
    fi
    sed \
        -e "s|@PIPELINE_DIR@|$PIPELINE_DIR|g" \
        -e "s|@HOME@|$HOME|g" \
        "$src" > "$dest"
    echo "  installed $dest"
done

# Enable user lingering so the timers fire even when no shell session
# is open (required for headless servers). No-op if already enabled.
if ! loginctl show-user "$USER" --property=Linger 2>/dev/null | grep -q "Linger=yes"; then
    echo "Enabling user lingering (requires sudo) so timers run without an active session..."
    sudo loginctl enable-linger "$USER"
fi

systemctl --user daemon-reload
systemctl --user enable --now radio-pipeline.timer
systemctl --user enable --now radio-pipeline-ytdlp.timer

echo
echo "Active timers:"
systemctl --user list-timers 'radio-pipeline*' --no-pager
