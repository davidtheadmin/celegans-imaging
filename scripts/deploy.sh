#!/usr/bin/env bash
#
# REQUIRES THE PI TO HAVE INTERNET — it pulls from GitHub on the Pi itself.
# If the Pi is offline (it normally is), use scripts/deploy_local.sh instead,
# which copies the files straight over SSH from this laptop.
#
set -euo pipefail

echo "==> Pushing to GitHub..."
git push

echo "==> Pulling on Pi..."
ssh celegans "cd celegans-imaging && git pull"

echo "==> Checking requirements on Pi..."
ssh celegans "cd celegans-imaging && source .venv/bin/activate && pip install -q -r capture/requirements.txt"

echo "==> Installing systemd unit files from deploy/..."
ssh celegans "
  changed=0
  for f in /home/pi/celegans-imaging/deploy/*.service /home/pi/celegans-imaging/deploy/*.timer; do
    dest=\"/etc/systemd/system/\$(basename \"\$f\")\"
    if ! diff -q \"\$f\" \"\$dest\" >/dev/null 2>&1; then
      sudo cp \"\$f\" \"\$dest\"
      echo \"  installed \$(basename \$f)\"
      changed=1
    fi
  done
  if [ \$changed -eq 1 ]; then
    sudo systemctl daemon-reload
    echo '  daemon-reload done'
  else
    echo '  all unit files up to date'
  fi
"

echo "==> Restarting service..."
ssh celegans "sudo systemctl restart celegans-capture"

echo "==> Service status:"
ssh celegans "sudo systemctl status celegans-capture --no-pager"
