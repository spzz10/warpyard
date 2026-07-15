#!/bin/bash
# Provision the Warpyard control plane on its VM (Ubuntu 24.04). Idempotent.
# Run as root ON the CP VM. App code is rsynced to /opt/warpyard before this runs.
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip postgresql postgresql-client rsync ufw >/dev/null

# --- Postgres ---
systemctl enable --now postgresql
sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='warpyard'" | grep -q 1 || \
  sudo -u postgres psql -qc "CREATE USER warpyard PASSWORD 'wy-local-only';"
sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='warpyard'" | grep -q 1 || \
  sudo -u postgres psql -qc "CREATE DATABASE warpyard OWNER warpyard;"

# --- app user + venv ---
id warpyard >/dev/null 2>&1 || useradd --system --home /opt/warpyard --shell /usr/sbin/nologin warpyard
cd /opt/warpyard
python3 -m venv .venv
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet -r requirements.txt
chown -R warpyard:warpyard /opt/warpyard
chmod 755 /opt/warpyard   # traversable so systemd WorkingDirectory + venv resolve

# --- env file: the deploy driver installs /opt/warpyard/.env.local (with secrets)
# BEFORE running this script. Fail loudly if it's missing rather than booting blank.
if [ ! -s /opt/warpyard/.env.local ]; then
  echo "ERROR: /opt/warpyard/.env.local is missing or empty — install it first." >&2
  exit 1
fi
chown warpyard:warpyard /opt/warpyard/.env.local
chmod 600 /opt/warpyard/.env.local

# --- migrations ---
set -a; . /opt/warpyard/.env.local; set +a
sudo -u warpyard env DATABASE_URL="$DATABASE_URL" ./.venv/bin/alembic upgrade head

# --- systemd units ---
cat > /etc/systemd/system/warpyard-api.service <<'EOF'
[Unit]
Description=Warpyard control plane API
After=network.target postgresql.service
[Service]
User=warpyard
WorkingDirectory=/opt/warpyard
EnvironmentFile=/opt/warpyard/.env.local
ExecStart=/opt/warpyard/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=on-failure
[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/warpyard-worker.service <<'EOF'
[Unit]
Description=Warpyard provisioning worker
After=network.target postgresql.service
[Service]
User=warpyard
WorkingDirectory=/opt/warpyard
EnvironmentFile=/opt/warpyard/.env.local
ExecStart=/opt/warpyard/.venv/bin/python -m app.jobs.worker
Restart=always
[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/warpyard-reconciler.service <<'EOF'
[Unit]
Description=Warpyard reconciler (one pass)
After=network.target postgresql.service
[Service]
Type=oneshot
User=warpyard
WorkingDirectory=/opt/warpyard
EnvironmentFile=/opt/warpyard/.env.local
ExecStart=/opt/warpyard/.venv/bin/python -c "from app.database import SessionLocal; from app.reconciler import reconcile; reconcile(SessionLocal())"
EOF

cat > /etc/systemd/system/warpyard-reconciler.timer <<'EOF'
[Unit]
Description=Run the Warpyard reconciler every 2 minutes
[Timer]
OnBootSec=120
OnUnitActiveSec=120
[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now warpyard-api warpyard-worker warpyard-reconciler.timer

# --- firewall: API reachable on LAN; SSH ---
ufw allow 22/tcp >/dev/null
# LAN_SUBNET = the management subnet allowed to reach the API/dashboard directly
ufw allow from "${LAN_SUBNET:-10.0.0.0/8}" to any port 8000 proto tcp >/dev/null
ufw --force enable >/dev/null

echo "provisioned. services:"
systemctl is-active warpyard-api warpyard-worker postgresql
