#!/usr/bin/env bash
# Deploy court-reserve web UI to Synology NAS.
#
# Prerequisites (one-time NAS setup):
#   1. Enable SSH: DSM → Control Panel → Terminal & SNMP
#   2. Add SSH public key: ssh-copy-id hsheng@192.168.68.70
#   3. Add GitHub host key: ssh in and run ssh-keyscan github.com >> ~/.ssh/known_hosts
#   4. Allow passwordless docker: echo 'hsheng ALL=(ALL) NOPASSWD: /usr/local/bin/docker' | sudo tee /etc/sudoers.d/docker-hsheng
#   5. Initial clone (once): ssh in and run git clone git@github.com:hs220/court-reserve.git /volume1/docker/court-reserve/court-reserve
#   6. Copy .env (once): cat .env | ssh hsheng@192.168.68.70 "cat > /volume1/docker/court-reserve/court-reserve/.env"
set -euo pipefail

NAS_HOST="hsheng@192.168.68.70"
NAS_DIR="/volume1/docker/court-reserve/court-reserve"
DOCKER="sudo /usr/local/bin/docker"

echo "==> Pulling latest code on NAS..."
ssh "$NAS_HOST" "cd $NAS_DIR && git pull"

echo "==> Building and starting web service..."
ssh "$NAS_HOST" "cd $NAS_DIR && $DOCKER compose up web --build -d"

echo "==> Done. UI available at http://192.168.68.70:7000"
