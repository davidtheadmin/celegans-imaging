#!/usr/bin/env bash
set -euo pipefail

echo "==> Pushing to GitHub..."
git push

echo "==> Pulling on Pi..."
ssh celegans "cd celegans-imaging && git pull"

echo "==> Checking requirements on Pi..."
ssh celegans "cd celegans-imaging && source .venv/bin/activate && pip install -q -r capture/requirements.txt"

echo "==> Restarting service..."
ssh celegans "sudo systemctl restart celegans-capture"

echo "==> Service status:"
ssh celegans "sudo systemctl status celegans-capture --no-pager"
