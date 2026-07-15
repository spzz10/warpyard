# Proxmox setup runbook

Everything Warpyard needs on the tenant hypervisor: a resource pool, a service user
with privilege-separated API tokens, golden templates, and (optionally) an encrypted
tenant dataset, a cloud-init TLS snippet, and Proxmox Backup Server grants.

Assumptions: a **standalone** PVE host (not clustered with anything else you run — see
`docs/ARCHITECTURE.md`), a VLAN-aware bridge for tenants (`docs/NETWORK.md`), and a
ZFS storage for tenant disks (linked clones make provisioning near-instant).

## Access model

Service user `warpyard@pve` with FOUR privilege-separated tokens. **PVE gotcha #1:**
privsep tokens get the *intersection* of user ACLs and token ACLs — the user must hold
the union of all roles on each path or every token call 403s.

```
pveum pool add tenants --comment "Warpyard tenant VMs"
pveum role add WYPower   -privs "VM.PowerMgmt,VM.Audit"
pveum role add WYConfig  -privs "VM.Allocate,VM.Clone,VM.Config.CPU,VM.Config.Memory,VM.Config.Disk,VM.Config.Network,VM.Config.Cloudinit,VM.Config.Options,VM.Config.HWType,VM.Audit,VM.Snapshot,VM.Snapshot.Rollback,Datastore.AllocateSpace,Datastore.Audit,SDN.Use"
pveum role add WYConsole -privs "VM.Console,VM.Audit"
pveum role add WYBackup  -privs "VM.Backup,VM.Audit,Datastore.AllocateSpace,Datastore.Audit,SDN.Use"
                                # SDN.Use: restore re-creates net0 and 403s without it
pveum role add WYPool    -privs "Pool.Allocate,Pool.Audit"   # gotcha #2: without this,
                                # clone's pool= param is SILENTLY dropped
pveum role add WYNodeAudit -privs "Sys.Audit,Datastore.Audit"  # read-only, for the Host page

pveum user add warpyard@pve --comment "Warpyard control plane"
pveum user token add warpyard@pve power   --privsep 1
pveum user token add warpyard@pve config  --privsep 1
pveum user token add warpyard@pve console --privsep 1
pveum user token add warpyard@pve backup  --privsep 1

# user = union of roles; tokens = their single role
pveum acl modify /pool/tenants --users warpyard@pve --roles WYPower,WYConfig,WYConsole,WYBackup,WYPool
pveum acl modify /pool/tenants --tokens "warpyard@pve!power"   --roles WYPower
pveum acl modify /pool/tenants --tokens "warpyard@pve!config"  --roles WYConfig,WYPool
pveum acl modify /pool/tenants --tokens "warpyard@pve!console" --roles WYConsole
pveum acl modify /pool/tenants --tokens "warpyard@pve!backup"  --roles WYBackup

# storage grants — repeat for EVERY storage tenant disks can land on (gotcha #4:
# a new storage entry, e.g. an encrypted dataset, 403s clones until granted here)
pveum acl modify /storage/<tenant-storage> --users warpyard@pve --roles WYConfig,WYBackup
pveum acl modify /storage/<tenant-storage> --tokens "warpyard@pve!config" --roles WYConfig
pveum acl modify /storage/<tenant-storage> --tokens "warpyard@pve!backup" --roles WYBackup

pveum acl modify /sdn/zones/localnetwork --users warpyard@pve --roles WYConfig
pveum acl modify /sdn/zones/localnetwork --tokens "warpyard@pve!config" --roles WYConfig
pveum acl modify /sdn/zones/localnetwork --tokens "warpyard@pve!backup" --roles WYBackup

# Host page (live node metrics for members) — read-only Sys.Audit
pveum acl modify /nodes/<node> --users warpyard@pve --roles WYNodeAudit
pveum acl modify /nodes/<node> --tokens "warpyard@pve!power" --roles WYNodeAudit
pveum acl modify /storage      --users warpyard@pve --roles WYNodeAudit
pveum acl modify /storage      --tokens "warpyard@pve!power" --roles WYNodeAudit

# per-template clone rights (repeat for each new template)
pveum acl modify /vms/9000 --users warpyard@pve --roles WYConfig
pveum acl modify /vms/9000 --tokens "warpyard@pve!config" --roles WYConfig
```

Token secrets go in the control plane's `.env` (never in git) as
`PROXMOX_TOKEN_POWER/CONFIG/CONSOLE/BACKUP` in `user@pve!tokenid=uuid` form.

## Golden templates

Start from stock cloud images (Ubuntu `noble-server-cloudimg-amd64.img`, Debian
`debian-12-genericcloud-amd64.qcow2`). Two quality-of-life bakes are worth doing with
`virt-customize` before templating (the app's console experience expects them):

- **serial console autologin** (`serial-getty@ttyS0` `--autologin root`) — the browser
  console is already ownership-gated, so a VM password is redundant; this makes
  "open console" = instant shell.
- **quiet cloud-init**: `/etc/cloud/cloud.cfg.d/99-warpyard-quiet.cfg` with
  `output: {all: '>> /var/log/cloud-init-output.log'}` so boot output doesn't spam the
  console.

```
qm create <vmid> --name <n> --memory 1024 --cores 1 --cpu host \
  --net0 virtio,bridge=<tenant-bridge>,firewall=1 --scsihw virtio-scsi-single \
  --serial0 socket --vga serial0 --agent enabled=1 --ostype l26
qm importdisk <vmid> <image> <tenant-storage>
qm set <vmid> --scsi0 <tenant-storage>:vm-<vmid>-disk-0 --ide2 <tenant-storage>:cloudinit --boot order=scsi0
qm resize <vmid> scsi0 10G
qm template <vmid>
# + the two /vms/<vmid> ACLs above
```

Use `--cpu host` (or at least x86-64-v2): the qemu64 default breaks modern binaries
(anything built for x86-64-v2+ — mysql:8, numpy, some Go/Rust releases).

Set each image row's `min_disk_gb` to the template's **baked** disk size (10 above) —
provisioning only grows the disk when the plan is larger; equal sizes skip the resize.

## Optional: at-rest encryption dataset

```
zfs create -o encryption=aes-256-gcm -o keyformat=raw \
  -o keylocation=file:///etc/zfs/keys/<pool>.key <pool>/enc
```

Register it as a storage entry, point `TENANT_STORAGE` at it, and **grant the storage
ACLs above on it**. Keep the key file on the boot disk (NOT on the encrypted pool),
back it up offline, and enable a `zfs load-key` unit ordered before `pve-guests` so
VMs unlock on boot. Encrypted tenants require full clones (slower creates) — the
create form treats it as an opt-in per server.

## Optional: TLS-passthrough cloud-init snippet

Place the vendor snippet (self-installs Caddy on web VMs; see `docs/NETWORK.md`) on a
snippet-enabled storage, e.g. `/var/lib/vz/snippets/wy-tls.yml` on `local`, and leave
`TLS_SNIPPET=vendor=local:snippets/wy-tls.yml`.

## Optional: Proxmox Backup Server

On PBS: a dedicated datastore whose size IS the tenant backup quota, plus PBS-side
prune/GC/verify jobs. Create `warpyard@pbs` + a token with **DatastoreBackup +
DatastoreAudit on that datastore only** — PBS token permissions are ALSO the
intersection with the user's ACLs, so grant both. On PVE: register the PBS storage
(`BACKUP_STORAGE`) and give the backup role the storage grants above. The same
auth-id that creates backup groups can forget them, which is how destroyed servers'
restore points get cleaned up.

## E2E proof (run this before pointing the app at the host)

clone(pool=tenants) → **wait for the task** (gotcha #3: the clone holds the VM config
lock until the full copy finishes — config calls before that hit `got timeout`; always
poll `/nodes/.../tasks/<upid>/status`) → set cloud-init (ciuser/sshkeys/ipconfig0) →
ipfilter-net0 ipset + `ipfilter=1,macfilter=1` → start via power token → SSH as root
with the injected key at the static IP → stop → delete(purge) via config token.

Negative checks: the power token must NOT be able to delete (403 VM.Allocate), and no
token may touch VMs outside the pool.
