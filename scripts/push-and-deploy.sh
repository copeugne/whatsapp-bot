#!/usr/bin/env bash
# Run from the repository root. SSH authentication uses your SSH config.
set -euo pipefail
VPS="${VPS:-oci-main}"
APP_DIR=/opt/whatsapp-bot
[[ -f pyproject.toml ]] || { echo 'Run from the repository root' >&2; exit 1; }
printf '[0%%] [0/3] Checking deployment directory\n'
ssh "$VPS" "sudo test -d $APP_DIR"
printf '[33%%] [1/3] Syncing source without secrets or runtime data\n'
# Root privileges are required to write the service-owned deployment directory.
# No --delete: preserve remote environment files, state and local evidence.
rsync -avz --rsync-path='sudo -n rsync' \
  --exclude=.venv --exclude=.git --exclude=__pycache__ --exclude=.pytest_cache \
  --exclude=.env --exclude=evolution.env --exclude=logs --exclude=data \
  --exclude=.pi --exclude=.omo --exclude=.omx --exclude=.claude --exclude=.codegraph \
  --exclude='*.key' --exclude='*.pem' --exclude='*.png' --exclude=logs.txt \
  . "${VPS}:${APP_DIR}/"
printf '[67%%] [2/3] Running provisioning script (review before production use)\n'
ssh -t "$VPS" "sudo bash $APP_DIR/scripts/deploy.sh"
printf '[100%%] [3/3] Deployment command finished\n'
