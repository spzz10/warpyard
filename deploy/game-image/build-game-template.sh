#!/usr/bin/env bash
# Build a LinuxGSM game-server golden template. MANUAL / host-side on the tenant host as root.
#   ./build-game-template.sh <vmid> <template-name> <lgsm_game> [extra_apt_deps_csv]
# e.g. ./build-game-template.sh 9004 tpl-terraria tsserver
#      ./build-game-template.sh 9007 tpl-valheim  vhserver  lib32gcc-s1,lib32stdc++6
set -euo pipefail
VMID="$1"; NAME="$2"; GAME="$3"; DEPS="${4:-}"
ISO=/var/lib/vz/template/iso; BASE="$ISO/wy-ubuntu-2404.img"; IMG="$ISO/wy-$GAME.img"; STORAGE="${STORAGE:-local-zfs}"
HERE="$(cd "$(dirname "$0")" && pwd)"
qm status "$VMID" >/dev/null 2>&1 && { echo "vmid $VMID already exists — aborting"; exit 1; }
COMMON="curl,wget,ca-certificates,file,bzip2,gzip,unzip,binutils,xz-utils,tmux,netcat-openbsd,bc,jq,lsb-release,distro-info-data"
ALL="$COMMON${DEPS:+,$DEPS}"
echo "→ $NAME ($GAME): copy base + virt-customize (deps: $ALL)"
cp -f "$BASE" "$IMG"
virt-customize -a "$IMG" \
  --run-command 'dpkg --add-architecture i386 || true' \
  --run-command 'apt-get update' \
  --install "$ALL" \
  --run-command 'useradd -m -s /bin/bash gameserver' \
  --run-command "su - gameserver -c \"wget -qO linuxgsm.sh https://linuxgsm.sh && chmod +x linuxgsm.sh && ./linuxgsm.sh $GAME\"" \
  --run-command "echo GAME=$GAME > /etc/warpyard-game.env" \
  --copy-in "$HERE/warpyard-game-install:/usr/local/bin/" \
  --copy-in "$HERE/warpyard-game-ctl:/usr/local/bin/" \
  --run-command 'chmod 755 /usr/local/bin/warpyard-game-ctl' \
  --run-command 'chmod 755 /usr/local/bin/warpyard-game-install' \
  --copy-in "$HERE/warpyard-game.service:/etc/systemd/system/" \
  --run-command 'systemctl enable warpyard-game' \
  --run-command 'apt-get clean && rm -rf /var/lib/apt/lists/*'
qm create "$VMID" --name "$NAME" --memory 2048 --cores 2 --cpu host \
  --net0 "virtio,bridge=${BRIDGE:-vmbr0},firewall=1" --scsihw virtio-scsi-single \
  --serial0 socket --vga serial0 --agent enabled=1 --ostype l26
qm importdisk "$VMID" "$IMG" "$STORAGE"
qm set "$VMID" --scsi0 "$STORAGE:vm-$VMID-disk-0,cache=writeback,discard=on,ssd=0" --ide2 "$STORAGE:cloudinit" --boot order=scsi0
qm resize "$VMID" scsi0 10G
qm template "$VMID"
pveum acl modify "/vms/$VMID" --users  warpyard@pve         --roles WYConfig
pveum acl modify "/vms/$VMID" --tokens "warpyard@pve!config" --roles WYConfig
echo "✓ template $VMID ($NAME) for $GAME built"
