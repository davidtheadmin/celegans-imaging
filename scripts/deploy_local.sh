#!/usr/bin/env bash
#
# deploy_local.sh — push code to the Pi over SSH only. No GitHub involved.
#
# WHY THIS EXISTS, alongside deploy.sh
# -----------------------------------
# deploy.sh does `git push` and then `ssh celegans "git pull"`, which requires
# the Pi to reach github.com. THE PI HAS NO INTERNET. That path cannot work
# here; this script copies the files straight from the laptop instead, so the
# laptop's network access is the only one needed.
#
# Use deploy.sh only if the Pi is ever given outbound internet. Otherwise this
# is the deploy.
#
# USAGE
#   scripts/deploy_local.sh                 # sync capture/ + deploy/ (the Pi's code)
#   scripts/deploy_local.sh FILE [FILE...]  # push only these repo-relative paths
#   DRY=1 scripts/deploy_local.sh           # show what would transfer, change nothing
#   RESTART=0 scripts/deploy_local.sh       # copy only, don't restart the service
#
# Examples
#   # just the web-UI half of a change
#   scripts/deploy_local.sh capture/app/routers/analyze.py \
#                           capture/app/static/index.html \
#                           capture/app/static/app.js
#
# NOTES
#   * Only capture/ and deploy/ ever run on the Pi. launcher/, dev/ and viewers/
#     are laptop-side and are deliberately NOT synced — copying them would just
#     fill the Pi's card.
#   * capture/.env is NEVER touched. It holds the auth token and is per-device;
#     overwriting it would lock the laptop out of its own Pi.
#   * NO pip install step, on purpose. The Pi cannot reach PyPI, so a dependency
#     change cannot be resolved by this script — it warns loudly instead and
#     tells you what to do about it.
#
set -euo pipefail

PI="${PI_HOST:-celegans}"                       # ssh alias from ~/.ssh/config
REMOTE="${PI_PATH:-/home/pi/celegans-imaging}"
SERVICE="${PI_SERVICE:-celegans-capture}"
DRY="${DRY:-0}"
RESTART="${RESTART:-1}"

cd "$(dirname "$0")/.."
REPO="$(pwd)"

say() { printf '==> %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- reachability
say "Checking $PI is reachable..."
ssh -o BatchMode=yes -o ConnectTimeout=8 "$PI" true 2>/dev/null || die \
"cannot ssh to '$PI'.

  - Is the Pi powered on and on the same network?
  - Does ~/.ssh/config define a Host entry named '$PI'?
  - Override with:  PI_HOST=pi@192.168.50.2 scripts/deploy_local.sh"

ssh "$PI" "test -d '$REMOTE'" || die \
"'$REMOTE' does not exist on the Pi. Override with PI_PATH=... if it moved."

# ------------------------------------------------------------------ file lists
EXCLUDES=(
  --exclude '.env'            # per-device auth token; never overwrite
  --exclude '__pycache__'
  --exclude '*.pyc'
  --exclude '.venv'
  --exclude '.git'
)

if [ "$#" -gt 0 ]; then
  MODE="explicit"
  PATHS=("$@")
  for f in "${PATHS[@]}"; do
    # .env is refused before the existence check on purpose: the answer is "no"
    # whether or not a local copy happens to exist.
    case "$f" in
      */.env|.env) die "refusing to push '$f': that is the Pi's auth token." ;;
    esac
    case "$f" in
      capture/*|deploy/*) : ;;
      *) die "refusing to push '$f': only capture/ and deploy/ run on the Pi." ;;
    esac
    [ -e "$REPO/$f" ] || die "no such file in the repo: $f"
  done
else
  MODE="full"
  PATHS=(capture deploy)
fi

# --------------------------------------------------- warn on dependency change
# The Pi cannot pip install, so a requirements change needs a human. Say so
# before the copy, not after the service fails to come back up.
REQ_CHANGED=0
if git -C "$REPO" rev-parse HEAD >/dev/null 2>&1; then
  git -C "$REPO" diff --quiet HEAD -- capture/requirements.txt 2>/dev/null || REQ_CHANGED=1
fi
if [ "$REQ_CHANGED" = "1" ]; then
  cat <<'WARN'

  !! capture/requirements.txt has uncommitted changes.

     The Pi has no internet, so this script CANNOT install new packages. If you
     added a dependency you must get the wheel onto the Pi yourself, e.g.

         pip download -d /tmp/wheels -r capture/requirements.txt
         scp -r /tmp/wheels celegans:/tmp/
         ssh celegans "cd celegans-imaging && source .venv/bin/activate \
                       && pip install --no-index --find-links=/tmp/wheels -r capture/requirements.txt"

     Continuing with the file copy anyway.

WARN
fi

# ------------------------------------------------------------------- transfer
# shellcheck disable=SC2054  # "stats1,name1" is one rsync argument, not two
RSYNC_FLAGS=(-az --info=stats1,name1 "${EXCLUDES[@]}")
[ "$DRY" = "1" ] && RSYNC_FLAGS+=(--dry-run)

if command -v rsync >/dev/null 2>&1 && ssh "$PI" "command -v rsync >/dev/null 2>&1"; then
  say "Syncing (rsync) ${PATHS[*]} -> $PI:$REMOTE"
  # Trailing-slash-free source names keep the directory name on the remote, so
  # capture/ lands at $REMOTE/capture and a single file keeps its subpath below.
  rsync "${RSYNC_FLAGS[@]}" --relative "${PATHS[@]}" "$PI:$REMOTE/"
else
  say "rsync unavailable on one end; falling back to tar over ssh"
  TAR_EX=(--exclude=.env --exclude=__pycache__ --exclude='*.pyc'
          --exclude=.venv --exclude=.git)
  if [ "$DRY" = "1" ]; then
    tar czf - "${TAR_EX[@]}" "${PATHS[@]}" | tar tzf - | sed 's/^/  would send: /'
  else
    tar czf - "${TAR_EX[@]}" "${PATHS[@]}" | ssh "$PI" "tar xzf - -C '$REMOTE'"
  fi
fi

if [ "$DRY" = "1" ]; then
  say "DRY run — nothing was written, service not restarted."
  exit 0
fi

# ------------------------------------------------------- systemd unit refresh
# Only when the whole tree went over; an explicit file list is a code push.
if [ "$MODE" = "full" ]; then
  say "Installing changed systemd units..."
  ssh "$PI" "
    changed=0
    for f in $REMOTE/deploy/*.service $REMOTE/deploy/*.timer; do
      dest=\"/etc/systemd/system/\$(basename \"\$f\")\"
      if ! diff -q \"\$f\" \"\$dest\" >/dev/null 2>&1; then
        sudo cp \"\$f\" \"\$dest\"; echo \"  installed \$(basename \$f)\"; changed=1
      fi
    done
    [ \$changed -eq 1 ] && { sudo systemctl daemon-reload; echo '  daemon-reload done'; } \
                        || echo '  all unit files up to date'
  "
fi

# ----------------------------------------------------------------- restart
if [ "$RESTART" != "1" ]; then
  say "RESTART=0 — files copied, service left alone."
  exit 0
fi

say "Restarting $SERVICE..."
ssh "$PI" "sudo systemctl restart $SERVICE"

# Static files are served from disk and need no restart; Python routers do. The
# health check is what actually proves the new code imported cleanly — a syntax
# error in a router leaves the unit 'active' briefly and then dead, so check the
# endpoint rather than trusting systemctl alone.
say "Waiting for the service to answer..."
ok=0
for _ in $(seq 1 15); do
  sleep 1
  if ssh "$PI" "curl -fsS --max-time 3 http://127.0.0.1:8000/health >/dev/null 2>&1"; then
    ok=1; break
  fi
done

if [ "$ok" = "1" ]; then
  say "Deployed. /health is answering."
else
  printf '\n'
  say "Service did NOT come back on /health. Last 40 log lines:"
  ssh "$PI" "sudo journalctl -u $SERVICE -n 40 --no-pager"
  exit 1
fi
