#!/usr/bin/env bash
# Build a one-click app golden template. MANUAL / host-side on the tenant host as root.
#   BASE_DOMAIN=example.com [STORAGE=...] [BRIDGE=...] ./build-app-template.sh <vmid> <slug> [files-dir]
# e.g. BASE_DOMAIN=example.com ./build-app-template.sh 9020 wordpress
# Starts from wy-docker.img (Docker + Compose already baked by build-docker-template.sh)
# and bakes the app's compose template + the first-boot installer. The chosen app is fixed
# per template (/etc/warpyard-app.env) — per-VM choice isn't possible at provision time.
set -euo pipefail
VMID="$1"
SLUG="$2"
HERE="$(cd "$(dirname "$0")" && pwd)"
FILES="${3:-$HERE}"
ISO=/var/lib/vz/template/iso
BASE="$ISO/wy-docker.img"
IMG="$ISO/wy-app-$SLUG.img"
NAME="tpl-app-$SLUG"
STORAGE="${STORAGE:-local-zfs}"   # storage tenant disks live on (must have the WY ACLs)
BRIDGE="${BRIDGE:-vmbr0}"         # the VLAN-aware tenant bridge
BASE_DOMAIN="${BASE_DOMAIN:?set BASE_DOMAIN to your base domain — it becomes the app public URL}"

[ -f "$BASE" ] || { echo "missing $BASE — run build-docker-template.sh first (it creates the source img)"; exit 1; }
[ -f "$FILES/apps/$SLUG/compose.yml" ] || { echo "unknown app '$SLUG' (no $FILES/apps/$SLUG/compose.yml)"; exit 1; }
qm status "$VMID" >/dev/null 2>&1 && { echo "vmid $VMID already exists — aborting"; exit 1; }

echo "→ $NAME: copy docker base + bake app '$SLUG'"
cp -f "$BASE" "$IMG"
virt-customize -a "$IMG" \
  --mkdir /opt/warpyard-app \
  --copy-in "$FILES/apps/$SLUG/compose.yml:/opt/warpyard-app/" \
  --run-command 'mv /opt/warpyard-app/compose.yml /opt/warpyard-app/compose.tpl.yml' \
  --copy-in "$FILES/warpyard-app-install:/usr/local/bin/" \
  --run-command 'chmod 755 /usr/local/bin/warpyard-app-install' \
  --copy-in "$FILES/warpyard-app.service:/etc/systemd/system/" \
  --run-command "printf 'APP=%s\nBASE_DOMAIN=%s\n' '$SLUG' '$BASE_DOMAIN' > /etc/warpyard-app.env" \
  --run-command 'systemctl enable warpyard-app'

# --cpu host: modern app images (mysql:8, open-webui/numpy) require x86-64-v2+;
# the qemu64 default masks the host's real flags and they crash on boot
qm create "$VMID" --name "$NAME" --memory 2048 --cores 2 --cpu host \
  --net0 "virtio,bridge=$BRIDGE,firewall=1" --scsihw virtio-scsi-single \
  --serial0 socket --vga serial0 --agent enabled=1 --ostype l26
qm importdisk "$VMID" "$IMG" "$STORAGE"
qm set "$VMID" --scsi0 "$STORAGE:vm-$VMID-disk-0,cache=writeback,discard=on,ssd=0" \
  --ide2 "$STORAGE:cloudinit" --boot order=scsi0
qm resize "$VMID" scsi0 10G
qm template "$VMID"
pveum acl modify "/vms/$VMID" --users warpyard@pve --roles WYConfig
pveum acl modify "/vms/$VMID" --tokens "warpyard@pve!config" --roles WYConfig
echo "✓ template $VMID ($NAME) for app $SLUG built"
