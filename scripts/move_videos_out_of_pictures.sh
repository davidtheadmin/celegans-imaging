#!/usr/bin/env bash
# One-shot migration: move *.mp4 files from pictures/<date>/ to videos/<date>/.
# Idempotent — skips files that already exist at the destination.
# Run on the Pi after stopping the service.
#
# Usage:
#   bash scripts/move_videos_out_of_pictures.sh

set -euo pipefail

DATA_ROOT="${CELEGANS_DATA_ROOT:-/home/pi/celegans-data}"
SRC="$DATA_ROOT/pictures"
DST="$DATA_ROOT/videos"

if [[ ! -d "$SRC" ]]; then
  echo "pictures/ not found at $SRC — nothing to do."
  exit 0
fi

moved=0
skipped=0

while IFS= read -r -d '' src_file; do
  date_dir="$(basename "$(dirname "$src_file")")"
  filename="$(basename "$src_file")"
  dst_dir="$DST/$date_dir"
  dst_file="$dst_dir/$filename"

  if [[ -e "$dst_file" ]]; then
    echo "SKIP (already at destination): $dst_file"
    skipped=$((skipped + 1))
    continue
  fi

  mkdir -p "$dst_dir"
  mv "$src_file" "$dst_file"
  echo "MOVED: $src_file -> $dst_file"
  moved=$((moved + 1))

  # Move sidecars if present
  for ext in .sha256 .acked; do
    sidecar="${src_file}${ext}"
    if [[ -e "$sidecar" ]]; then
      mv "$sidecar" "${dst_file}${ext}"
      echo "  sidecar: $filename$ext"
    fi
  done

done < <(find "$SRC" -name "*.mp4" -print0 2>/dev/null)

echo ""
echo "Done — moved $moved file(s), skipped $skipped file(s)."
