#!/usr/bin/env bash
# Build the Docker host golden template (vmid 9009, tpl-docker) — a general "run any service"
# image. Docker + Compose baked in; a web container on port 80 is served at the server's
# <name>.<base domain> URL (and any custom domain). MANUAL / host-side on the tenant host as root.
set -euo pipefail
VMID=9009
NAME=tpl-docker
ISO=/var/lib/vz/template/iso
BASE="$ISO/wy-ubuntu-2404.img"
IMG="$ISO/wy-docker.img"
STORAGE="${STORAGE:-local-zfs}"   # tenant-disk storage (must have the WY ACLs)

qm status "$VMID" >/dev/null 2>&1 && { echo "vmid $VMID already exists — aborting"; exit 1; }
cp -f "$BASE" "$IMG"
virt-customize -a "$IMG" \
  --run-command 'apt-get update' \
  --install docker.io,docker-compose-v2,ca-certificates \
  --run-command 'systemctl enable docker' \
  --run-command 'apt-get clean && rm -rf /var/lib/apt/lists/*'
qm create "$VMID" --name "$NAME" --memory 2048 --cores 2 --cpu host \
  --net0 "virtio,bridge=${BRIDGE:-vmbr0},firewall=1" --scsihw virtio-scsi-single \
  --serial0 socket --vga serial0 --agent enabled=1 --ostype l26
qm importdisk "$VMID" "$IMG" "$STORAGE"
qm set "$VMID" --scsi0 "$STORAGE:vm-$VMID-disk-0,cache=writeback,discard=on,ssd=0" \
  --ide2 "$STORAGE:cloudinit" --boot order=scsi0
qm resize "$VMID" scsi0 10G
qm template "$VMID"
pveum acl modify "/vms/$VMID" --users  warpyard@pve         --roles WYConfig
pveum acl modify "/vms/$VMID" --tokens "warpyard@pve!config" --roles WYConfig
echo "✓ template $VMID ($NAME) built. Register image slug 'docker' with template_vmid=$VMID, category=app."
