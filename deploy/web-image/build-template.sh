#!/usr/bin/env bash
# Build the push-to-deploy golden template (vmid 9002, tpl-web-2404) on the tenant host.
# MANUAL / host-side — run this on the tenant host as root, NOT from CI. It bakes Caddy + Node + a
# bare git repo with the deploy hook into a copy of the wy-ubuntu-2404 base image, then
# registers it as a template. Idempotent-ish: it refuses if 9002 already exists.
#
# Usage (from a dir containing post-receive, Caddyfile, caddy.service, index.html):
#   ./build-template.sh
set -euo pipefail

VMID=9002
NAME=tpl-web-2404
ISO=/var/lib/vz/template/iso
BASE="$ISO/wy-ubuntu-2404.img"      # already has console autologin + quiet cloud-init
IMG="$ISO/wy-web-2404.img"
STORAGE="${STORAGE:-local-zfs}"   # tenant-disk storage (must have the WY ACLs)
HERE="$(cd "$(dirname "$0")" && pwd)"

qm status "$VMID" >/dev/null 2>&1 && { echo "vmid $VMID already exists — aborting"; exit 1; }

echo "→ copy base image (no partition resize — it breaks GRUB; cloud-init growpart grows the"
echo "  disk to the plan size on first boot, same as the base template)"
cp -f "$BASE" "$IMG"

echo "→ virt-customize: install caddy + node + git, bake repo/hook/webroot"
# Node from NodeSource (one package that bundles npm) — the Debian 'npm' package pulls 240+
# node-* debs and blows the small root fs. Caddy is the static official binary. This trio is
# ~200MB and fits the base image's free space.
virt-customize -a "$IMG" \
  --run-command 'apt-get update' \
  --install git,curl,ca-certificates \
  --run-command 'curl -fsSL https://deb.nodesource.com/setup_20.x | bash -' \
  --install nodejs \
  --run-command 'curl -fsSL "https://caddyserver.com/api/download?os=linux&arch=amd64" -o /usr/bin/caddy && chmod +x /usr/bin/caddy' \
  --mkdir /srv --mkdir /etc/caddy --mkdir /var/www/releases/initial \
  --run-command 'git init --bare /srv/site.git' \
  --copy-in "$HERE/post-receive:/srv/site.git/hooks/" \
  --run-command 'chmod +x /srv/site.git/hooks/post-receive' \
  --copy-in "$HERE/Caddyfile:/etc/caddy/" \
  --copy-in "$HERE/caddy.service:/etc/systemd/system/" \
  --copy-in "$HERE/index.html:/var/www/releases/initial/" \
  --run-command 'ln -sfnT /var/www/releases/initial /var/www/site' \
  --run-command 'systemctl enable caddy' \
  --run-command 'apt-get clean && rm -rf /var/lib/apt/lists/*'

echo "→ create VM $VMID and import disk (mirrors tpl 9012)"
qm create "$VMID" --name "$NAME" --memory 1024 --cores 1 --cpu host \
  --net0 "virtio,bridge=${BRIDGE:-vmbr0},firewall=1" --scsihw virtio-scsi-single \
  --serial0 socket --vga serial0 --agent enabled=1 --ostype l26
qm importdisk "$VMID" "$IMG" "$STORAGE"
qm set "$VMID" --scsi0 "$STORAGE:vm-$VMID-disk-0,cache=writeback,discard=on,ssd=0" \
  --ide2 "$STORAGE:cloudinit" --boot order=scsi0
qm resize "$VMID" scsi0 10G
qm template "$VMID"

echo "→ per-template clone ACLs (user + config token)"
pveum acl modify "/vms/$VMID" --users warpyard@pve --roles WYConfig
pveum acl modify "/vms/$VMID" --tokens "warpyard@pve!config" --roles WYConfig

echo "→ storage ACL so clones can allocate on $STORAGE (mirrors the tenant-storage grant in docs/PVE-SETUP.md)"
pveum acl modify "/storage/$STORAGE" --users warpyard@pve --roles WYConfig
pveum acl modify "/storage/$STORAGE" --tokens "warpyard@pve!config" --roles WYConfig

echo "✓ template $VMID ($NAME) built. Register the image row with template_vmid=$VMID, slug web-2404."
