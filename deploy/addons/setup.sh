#!/usr/bin/env bash
# Warpyard add-on installer. Run it ON your server (you already have SSH):
#   curl -fsSL https://app.<your-domain>/setup.sh | sudo bash -s docker
#   curl -fsSL https://app.<your-domain>/setup.sh | sudo bash -s pushdeploy
#   curl -fsSL https://app.<your-domain>/setup.sh | sudo bash -s docker pushdeploy
# Idempotent — safe to re-run (e.g. after a rebuild). Completion markers in /var/lib/warpyard.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Please run with sudo."; exit 1; }
MARK=/var/lib/warpyard; mkdir -p "$MARK"

apt_lock() {
  local n=0
  while fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1 || fuser /var/lib/apt/lists/lock >/dev/null 2>&1; do
    [ $((n++)) -gt 60 ] && { echo "apt is busy — try again in a minute."; exit 1; }
    echo "waiting for the package manager…"; sleep 3
  done
}
apt_get() { apt_lock; DEBIAN_FRONTEND=noninteractive apt-get "$@"; }

docker_addon() {
  [ -f "$MARK/docker.done" ] && { echo "✓ Docker is already set up."; return; }
  echo "→ Installing Docker + Compose…"
  apt_get update -qq
  apt_get install -y -qq docker.io docker-compose-v2
  systemctl enable --now docker
  touch "$MARK/docker.done"
  echo "✓ Docker + Compose ready. Try:  docker run -d -p 80:80 nginx   (served at your server's URL)"
}

pushdeploy_addon() {
  [ -f "$MARK/pushdeploy.done" ] && { echo "✓ Push-to-deploy is already set up."; return; }
  echo "→ Installing push-to-deploy (Caddy + Node + git hook)…"
  apt_get update -qq
  apt_get install -y -qq git curl ca-certificates
  command -v node >/dev/null 2>&1 || { curl -fsSL https://deb.nodesource.com/setup_20.x | bash - >/dev/null 2>&1 || true; apt_get install -y -qq nodejs; }
  [ -x /usr/bin/caddy ] || { curl -fsSL "https://caddyserver.com/api/download?os=linux&arch=amd64" -o /usr/bin/caddy; chmod +x /usr/bin/caddy; }

  [ -d /srv/site.git ] || git init --bare -q /srv/site.git
  cat > /srv/site.git/hooks/post-receive <<'HOOK'
#!/usr/bin/env bash
set -euo pipefail
export PATH="/usr/local/bin:/usr/bin:/bin"
REPO=/srv/site.git; WEBROOT=/var/www/site
while read -r _old newrev ref; do
  [ "$ref" = "refs/heads/main" ] || { echo "warpyard: '$ref' pushed — only 'main' deploys, skipping."; continue; }
  echo "warpyard: deploying ${newrev:0:8} …"
  WORK=$(mktemp -d)
  git --git-dir="$REPO" --work-tree="$WORK" checkout -f main
  PUB="$WORK"
  if [ -f "$WORK/package.json" ]; then
    echo "warpyard: package.json found → npm ci && npm run build"
    ( cd "$WORK" && npm ci && npm run build )
    for d in dist build out public; do [ -d "$WORK/$d" ] && PUB="$WORK/$d" && break; done
  fi
  REL="/var/www/releases/$(date +%Y%m%d%H%M%S)-${newrev:0:8}"
  mkdir -p /var/www/releases
  cp -aT "$PUB" "$REL"
  ln -sfnT "$REL" "$WEBROOT"
  ls -1dt /var/www/releases/*/ 2>/dev/null | tail -n +3 | xargs -r rm -rf
  rm -rf "$WORK"
  echo "warpyard: live ✓  your site is serving."
done
HOOK
  chmod +x /srv/site.git/hooks/post-receive

  mkdir -p /etc/caddy /var/www/releases/initial
  [ -f /var/www/releases/initial/index.html ] || cat > /var/www/releases/initial/index.html <<'IDX'
<!doctype html><meta charset="utf-8"><title>Ready to deploy — Warpyard</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0a0c10;color:#e7eaf0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,system-ui,sans-serif}h1{font-weight:600}p{color:#97a1b2}</style>
<div style="text-align:center;max-width:460px;padding:2rem"><h1>Ready to deploy</h1><p>Push a git repo to this server (see the <b>Deploy</b> card on your Warpyard dashboard) and your site appears here.</p></div>
IDX
  [ -e /var/www/site ] || ln -sfnT /var/www/releases/initial /var/www/site
  cat > /etc/caddy/Caddyfile <<'CADDY'
:80 {
	root * /var/www/site
	encode gzip
	try_files {path} {path}/ /index.html
	file_server
}
CADDY
  cat > /etc/systemd/system/caddy.service <<'SVC'
[Unit]
Description=Caddy (Warpyard web app)
After=network.target
[Service]
ExecStart=/usr/bin/caddy run --config /etc/caddy/Caddyfile --adapter caddyfile
ExecReload=/usr/bin/caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
Restart=always
LimitNOFILE=1048576
[Install]
WantedBy=multi-user.target
SVC
  systemctl daemon-reload
  systemctl enable --now caddy
  touch "$MARK/pushdeploy.done"
  echo "✓ Push-to-deploy ready. Back on your computer: run the two git commands on your server's Add-ons tab (git remote add warpyard …, then git push warpyard main)."
}

[ $# -gt 0 ] || { echo "Usage: … | sudo bash -s [docker] [pushdeploy]"; exit 1; }
for a in "$@"; do
  case "$a" in
    docker) docker_addon ;;
    pushdeploy|push-to-deploy) pushdeploy_addon ;;
    *) echo "unknown add-on: $a (choices: docker, pushdeploy)" ;;
  esac
done
echo "All done."
