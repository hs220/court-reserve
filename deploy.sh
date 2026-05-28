#!/usr/bin/env bash
set -euo pipefail

NAS_HOST="hsheng@192.168.68.70"
NAS_DIR="/volume1/docker/court-reserve"
DOCKER="/usr/local/bin/docker"

echo "==> Syncing files to NAS..."
rsync -av --delete \
  --exclude='.venv/' \
  --exclude='__pycache__/' \
  --exclude='*.pyc' \
  --exclude='.git/' \
  --exclude='*.plist' \
  ./ "$NAS_HOST:$NAS_DIR/"

echo "==> Building and starting web service..."
ssh "$NAS_HOST" "cd $NAS_DIR && $DOCKER compose up web --build -d"

echo "==> Done. UI available at http://192.168.68.70:7000"
