#!/usr/bin/env bash
# Build the Minecraft (LinuxGSM) golden template (vmid 9003, tpl-mc-lgsm) on the tenant host.
# MANUAL / host-side — run on the tenant host as root, NOT from CI. Bakes LinuxGSM + Java + the mcserver
# scripts into a copy of wy-ubuntu-2404.img; the server itself downloads + accepts EULA on FIRST
# boot (via warpyard-mc.service), so the template stays small and boots fast.
#
# Adding another game later = same pattern with a different LGSM server code (vhserver, etc.).
set -euo pipefail

VMID=9003
NAME=tpl-mc-lgsm
ISO=/var/lib/vz/template/iso
BASE="$ISO/wy-ubuntu-2404.img"     # console autologin + quiet cloud-init already baked
IMG="$ISO/wy-mc-lgsm.img"
STORAGE="${STORAGE:-local-zfs}"   # tenant-disk storage (must have the WY ACLs)
HERE="$(cd "$(dirname "$0")" && pwd)"

qm status "$VMID" >/dev/null 2>&1 && { echo "vmid $VMID already exists — aborting"; exit 1; }

echo "→ copy base image (no resize — NodeSource-style small footprint; cloud-init grows disk on boot)"
cp -f "$BASE" "$IMG"

echo "→ virt-customize: LinuxGSM deps + Java, create mcserver user, fetch the mcserver scripts"
virt-customize -a "$IMG" \
  --run-command 'apt-get update' \
  --install curl,wget,ca-certificates,file,bzip2,gzip,unzip,binutils,xz-utils,tmux,netcat-openbsd,bc,jq,lsb-release,distro-info-data,openjdk-25-jre-headless \
  --run-command 'useradd -m -s /bin/bash mcserver' \
  --run-command 'su - mcserver -c "wget -qO linuxgsm.sh https://linuxgsm.sh && chmod +x linuxgsm.sh && ./linuxgsm.sh mcserver"' \
  --mkdir /home/mcserver/lgsm/config-lgsm/mcserver \
  --copy-in "$HERE/common.cfg:/home/mcserver/lgsm/config-lgsm/mcserver/" \
  --run-command 'chown -R mcserver:mcserver /home/mcserver' \
  --copy-in "$HERE/warpyard-mc-install:/usr/local/bin/" \
  --run-command 'chmod 755 /usr/local/bin/warpyard-mc-install' \
  --copy-in "$HERE/warpyard-mc.service:/etc/systemd/system/" \
  --run-command 'systemctl enable warpyard-mc' \
  --run-command 'apt-get clean && rm -rf /var/lib/apt/lists/*'

echo "→ create VM $VMID + import disk (2 GB / 2 cores default; the plan overrides at provision)"
qm create "$VMID" --name "$NAME" --memory 2048 --cores 2 --cpu host \
  --net0 "virtio,bridge=${BRIDGE:-vmbr0},firewall=1" --scsihw virtio-scsi-single \
  --serial0 socket --vga serial0 --agent enabled=1 --ostype l26
qm importdisk "$VMID" "$IMG" "$STORAGE"
qm set "$VMID" --scsi0 "$STORAGE:vm-$VMID-disk-0,cache=writeback,discard=on,ssd=0" \
  --ide2 "$STORAGE:cloudinit" --boot order=scsi0
qm resize "$VMID" scsi0 10G
qm template "$VMID"

echo "→ per-template clone ACLs (user + config token)"
pveum acl modify "/vms/$VMID" --users  warpyard@pve         --roles WYConfig
pveum acl modify "/vms/$VMID" --tokens "warpyard@pve!config" --roles WYConfig

echo "✓ template $VMID ($NAME) built. Register image slug 'minecraft' with template_vmid=$VMID,"
echo "  category=game, lgsm_game=mcserver, ports=tcp:25565."
