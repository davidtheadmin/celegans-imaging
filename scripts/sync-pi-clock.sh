#!/usr/bin/env bash
# Sync the Pi's clock from the laptop. Run at session start when Pi has no internet.
set -euo pipefail

echo "Syncing Pi clock..."
ssh celegans "sudo date -s '$(date -Iseconds)'"
echo "Pi time after sync:"
ssh celegans "date"
