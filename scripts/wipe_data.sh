#!/usr/bin/env bash
# Wipe all experimental data from the Pi data directory.
# Preserves flatfield/ and .trash/. Does NOT touch the repo itself.
#
# Use before a fresh deployment when you want a clean slate.
#
# Usage:
#   bash scripts/wipe_data.sh

set -euo pipefail

DATA_ROOT="${CELEGANS_DATA_ROOT:-/home/pi/celegans-data}"

echo "This will permanently delete:"
echo "  $DATA_ROOT/experiments/*"
echo "  $DATA_ROOT/pictures/*"
echo "  $DATA_ROOT/videos/*"
echo ""
echo "flatfield/ and .trash/ will NOT be touched."
echo ""
read -r -p "Type YES to confirm: " confirm

if [[ "$confirm" != "YES" ]]; then
  echo "Aborted."
  exit 1
fi

for dir in experiments pictures videos; do
  target="$DATA_ROOT/$dir"
  if [[ -d "$target" ]]; then
    rm -rf "${target:?}/"*
    echo "Wiped $target"
  else
    echo "Skipped $target (does not exist)"
  fi
done

echo ""
echo "Done."
