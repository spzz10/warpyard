#!/usr/bin/env bash
# Single source of truth for the one-click app catalog. Emits SQL that upserts the
# images rows — pipe into psql on the control plane:
#   ./catalog.sh | sudo -u warpyard psql warpyard
# Rows are registered with STATUS (default 'deprecated' = hidden from the picker);
# the verification pass flips each app to 'active' only after it's proven E2E.
set -euo pipefail
STATUS="${STATUS:-deprecated}"

# slug|vmid|name|plan|min_disk|blurb|guidance
CAT=$(
  cat <<'EOF'
wordpress|9020|WordPress|wy-1-1|10|The web's favorite blog & site builder|Your WordPress site lives at your server's URL. The first visit runs the 5-minute installer — you pick the admin login there. Give it a few minutes after creation before the site appears.
nextcloud|9021|Nextcloud|wy-2-2|10|Your own private cloud drive|Your Nextcloud is at your server's URL. Sign in with the admin login from /root/CREDENTIALS.txt (open the Console and run: cat /root/CREDENTIALS.txt). First boot takes a few minutes.
gitea|9022|Gitea|wy-1-1|10|Self-hosted Git with issues & wiki|Gitea is at your server's URL. The first visit shows the install page — confirm the defaults and create your admin account. Use HTTPS remotes (container SSH is off).
uptime-kuma|9023|Uptime Kuma|wy-1-1|10|Pretty uptime monitoring dashboards|Uptime Kuma is at your server's URL. The first visit creates the admin account — do it right away, the page is public.
vaultwarden|9024|Vaultwarden|wy-1-1|10|Bitwarden-compatible password vault|Vaultwarden is at your server's URL — create your account on the first visit (signups are open, so do it promptly). The admin-panel token is in /root/CREDENTIALS.txt (Console → cat /root/CREDENTIALS.txt). Use the official Bitwarden apps with your server URL.
n8n|9025|n8n|wy-1-1|10|Drag-and-drop workflow automation|n8n is at your server's URL. The first visit creates the owner account. Webhook URLs are generated with your https address automatically.
jellyfin|9026|Jellyfin|wy-2-2|10|Stream your own movies & music|Jellyfin is at your server's URL — the first visit runs the setup wizard. Upload media to /srv/media on the server (scp/sftp over your SSH address); add it as a library at /media.
ghost|9027|Ghost|wy-1-1|10|Sleek publishing & newsletters|Ghost is at your server's URL. Create the admin account at /ghost on your first visit. First boot takes a few minutes while the database initializes.
code-server|9028|code-server|wy-1-1|10|VS Code in your browser|code-server is at your server's URL. The password is in /root/CREDENTIALS.txt (open the Console and run: cat /root/CREDENTIALS.txt).
ollama-webui|9029|Ollama + Open WebUI|wy-2-4|10|Chat with local AI models (CPU)|Open WebUI is at your server's URL — the first visit creates the admin account. Pull a SMALL model from the model panel (llama3.2:3b, qwen2.5:3b, phi3) — this host runs models on CPU, so big ones will crawl.
EOF
)

sqlq() { printf %s "$1" | sed "s/'/''/g"; } # escape single quotes for SQL

echo "BEGIN;"
while IFS='|' read -r slug vmid name plan disk blurb guidance; do
  [ -n "$slug" ] || continue
  cat <<SQL
INSERT INTO images (slug, name, distro, version, template_vmid, min_disk_gb, status, category, lgsm_game, ports, default_plan, blurb, guidance, created_at)
VALUES ('$(sqlq "$slug")', '$(sqlq "$name")', 'ubuntu', '24.04', $vmid, $disk, '$STATUS', 'app', '', '', '$(sqlq "$plan")', '$(sqlq "$blurb")', '$(sqlq "$guidance")', now())
ON CONFLICT (slug) DO UPDATE SET
  name = EXCLUDED.name, template_vmid = EXCLUDED.template_vmid, min_disk_gb = EXCLUDED.min_disk_gb,
  category = 'app', default_plan = EXCLUDED.default_plan, blurb = EXCLUDED.blurb, guidance = EXCLUDED.guidance;
SQL
done <<<"$CAT"
echo "COMMIT;"
