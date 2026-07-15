#!/bin/bash
# ⚠️ EMERGENCY-ONLY manual deploy. The normal path is GitLab CI/CD: push to main,
# the pipeline tests and deploys the exact SHA (see .gitlab-ci.yml). This script
# rsyncs the LOCAL WORKING TREE — anything it ships that isn't committed+pushed
# will be REVERTED by the next CI deploy. If you use it for a live hotfix, commit
# and push the same change through CI immediately after.
# Usage: deploy/redeploy.sh [cp_host]
set -euo pipefail
echo "⚠️  EMERGENCY-ONLY: normal deploys go through GitLab CI (push to main)." >&2
read -r -p "Deploy the local working tree anyway? [y/N] " ok
[[ "${ok,,}" == y* ]] || exit 1

CP="${1:?usage: redeploy.sh <control-plane-ip>}"
KEY="${WY_SSH_KEY:-$HOME/.ssh/id_ed25519}"
SSH="ssh -i $KEY ${WY_SSH_USER:-root}@$CP"

echo "→ syncing code to $CP"
rsync -az -e "ssh -i $KEY" \
  --exclude '.venv' --exclude '*.db' --exclude '.git' --exclude '__pycache__' \
  --exclude '.ruff_cache' --exclude '.pytest_cache' --exclude '.env' --exclude 'seed.pub' \
  ./ "${WY_SSH_USER:-root}@$CP:/tmp/wy-sync/"

$SSH 'sudo rsync -a --delete --exclude .env.local --exclude .venv /tmp/wy-sync/ /opt/warpyard/ \
  && sudo chown -R warpyard:warpyard /opt/warpyard && sudo chmod 755 /opt/warpyard && rm -rf /tmp/wy-sync'

echo "→ deps + migrate"
$SSH 'sudo /opt/warpyard/.venv/bin/pip install -q -r /opt/warpyard/requirements.txt
sudo bash -c "cd /opt/warpyard && set -a && . /opt/warpyard/.env.local && set +a \
  && sudo -u warpyard env DATABASE_URL=\$DATABASE_URL /opt/warpyard/.venv/bin/alembic upgrade head"'

echo "→ restart services"
$SSH 'sudo systemctl reset-failed warpyard-api warpyard-worker 2>/dev/null || true
sudo systemctl restart warpyard-api warpyard-worker'
sleep 3
echo "→ health"
$SSH 'systemctl is-active warpyard-api warpyard-worker; curl -s -o /dev/null -w "login: %{http_code}\n" \
  -X POST http://localhost:8000/login --data "email=<admin-email>&password=<password>"'
echo "✓ redeployed"
