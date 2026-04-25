#!/usr/bin/env bash
# Migrate on-disk data folders from the old names to the new names.
#
# STOP the celegans-capture service before running this script:
#   sudo systemctl stop celegans-capture
#
# Safe to re-run: each mv only executes when the source exists AND the
# destination does NOT exist, so a partial run can be resumed cleanly.
# If the destination already exists the script exits with an error rather
# than mv-ing into it (which would create a nested sub-folder).

set -euo pipefail

DATA_ROOT="${CELEGANS_DATA_ROOT:-/home/pi/celegans-data}"

move_if_safe() {
    local src="$1"
    local dst="$2"
    if [ ! -e "$src" ]; then
        echo "SKIP  (source absent)  $src"
        return
    fi
    if [ -e "$dst" ]; then
        echo "ERROR destination already exists — resolve manually: $dst" >&2
        exit 1
    fi
    mv "$src" "$dst"
    echo "DONE  $src  ->  $dst"
}

move_if_safe "$DATA_ROOT/sessions"           "$DATA_ROOT/experiments"
move_if_safe "$DATA_ROOT/freecapture"        "$DATA_ROOT/pictures"
move_if_safe "$DATA_ROOT/.trash/sessions"    "$DATA_ROOT/.trash/experiments"
move_if_safe "$DATA_ROOT/.trash/freecapture" "$DATA_ROOT/.trash/pictures"

echo "Migration complete. Start the service: sudo systemctl start celegans-capture"
