"""Thin Proxmox VE API client with privilege-split tokens.

Three tokens, all scoped to the tenant pool (created on the PVE node at setup — see docs/PVE-SETUP.md):
  power   — VM.PowerMgmt only (start/stop/reboot)
  config  — VM.Clone, VM.Config.*, VM.Allocate (create/rebuild/resize/destroy)
  console — VM.Console only (vncproxy tickets)

A leak of any one token limits the blast radius; none can touch storage/net
definitions or non-pool VMs. The only Sys.* privilege anywhere is the power
token's WYNodeAudit (Sys.Audit + Datastore.Audit, read-only) so the admin Host
page can read node metrics and storage usage.
"""

import httpx

from app.config import get_settings


class ProxmoxError(Exception):
    pass


class ProxmoxClient:
    def __init__(self, role: str = "config"):
        s = get_settings()
        token = {
            "power": s.PROXMOX_TOKEN_POWER,
            "config": s.PROXMOX_TOKEN_CONFIG,
            "console": s.PROXMOX_TOKEN_CONSOLE,
            "backup": s.PROXMOX_TOKEN_BACKUP,
        }[role]
        self.node = s.PROXMOX_NODE
        self.pool = s.PROXMOX_POOL
        self._client = httpx.Client(
            base_url=f"{s.PROXMOX_API_URL}/api2/json",
            headers={"Authorization": f"PVEAPIToken={token}"},
            verify=s.PROXMOX_VERIFY_TLS,
            timeout=30.0,
        )

    def _req(self, method: str, path: str, **kwargs) -> dict:
        try:
            resp = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as e:
            # transport failures (DNS, refused, timeout) surface as ProxmoxError so
            # callers that degrade gracefully on an unreachable hypervisor actually do
            raise ProxmoxError(f"{method} {path} -> {e.__class__.__name__}: {e}") from e
        if resp.status_code >= 400:
            raise ProxmoxError(f"{method} {path} -> {resp.status_code}: {resp.text[:300]}")
        return resp.json().get("data")

    # ── read ────────────────────────────────────────────────────────────
    def vm_status(self, vmid: int) -> dict:
        return self._req("GET", f"/nodes/{self.node}/qemu/{vmid}/status/current")

    def vm_rrddata(self, vmid: int, timeframe: str = "hour") -> list[dict]:
        """Metrics history (cpu/mem/net/disk) for the dashboard graphs. Readable by every
        token (VM.Audit rides along on the shared pool role); callers use 'power' as the
        least dangerous one. timeframe: hour|day|week."""
        return (
            self._req(
                "GET",
                f"/nodes/{self.node}/qemu/{vmid}/rrddata",
                params={"timeframe": timeframe, "cf": "AVERAGE"},
            )
            or []
        )

    def node_status(self) -> dict:
        """Host CPU/memory/load/uptime/versions. Needs Sys.Audit on the node (power token)."""
        return self._req("GET", f"/nodes/{self.node}/status")

    def node_rrddata(self, timeframe: str = "hour") -> list[dict]:
        """Host metrics history (cpu/iowait/load/mem/arc/net/psi). Needs Sys.Audit on the node."""
        return (
            self._req(
                "GET",
                f"/nodes/{self.node}/rrddata",
                params={"timeframe": timeframe, "cf": "AVERAGE"},
            )
            or []
        )

    def node_storage(self) -> list[dict]:
        """Per-storage usage on the node — only storages the token can Datastore.Audit."""
        return self._req("GET", f"/nodes/{self.node}/storage") or []

    def cluster_vms(self) -> list[dict]:
        """Live per-VM stats (cpu/mem/uptime/status) for every VM the token can VM.Audit —
        for the pool-scoped tokens that's exactly the tenant fleet."""
        rows = self._req("GET", "/cluster/resources", params={"type": "vm"}) or []
        return [r for r in rows if not r.get("template")]

    def pool_vmids(self) -> set[int]:
        data = self._req("GET", f"/pools/{self.pool}")
        return {m["vmid"] for m in data.get("members", []) if m.get("type") == "qemu"}

    def task_status(self, upid: str) -> dict:
        return self._req("GET", f"/nodes/{self.node}/tasks/{upid}/status")

    # ── config-token verbs ──────────────────────────────────────────────
    def clone(self, template_vmid: int, new_vmid: int, name: str, full: bool = True, storage: str | None = None) -> str:
        # storage= targets a full clone's disks at a specific storage (e.g. the encrypted-at-rest
        # pool). A full clone is required to cross storages / encryption boundaries.
        data = {"newid": new_vmid, "name": name, "full": int(full), "pool": self.pool}
        if storage:
            data["storage"] = storage
        return self._req("POST", f"/nodes/{self.node}/qemu/{template_vmid}/clone", data=data)

    def set_config(self, vmid: int, **config) -> None:
        self._req("POST", f"/nodes/{self.node}/qemu/{vmid}/config", data=config)

    def set_cloudinit(
        self,
        vmid: int,
        hostname: str,
        ssh_keys: str,
        ipconfig0: str,
        password: str | None = None,
        cicustom: str | None = None,
    ) -> None:
        # sshkeys is double-URL-encoded per PVE API quirk — httpx encodes once more on send
        from urllib.parse import quote

        s = get_settings()
        cfg = {
            "ciuser": "root",
            "sshkeys": quote(ssh_keys, safe=""),
            "ipconfig0": ipconfig0,
            "nameserver": s.TENANT_NAMESERVERS,  # LAN DNS is blocked for tenants by design
            # NO searchdomain: 'search <base domain>' + our wildcard *.<base domain>
            # wildcard made any name that doesn't resolve on the first try (e.g. Steam's CDN
            # host) fall back to <host>.<base domain> → the edge IP. Short OS hostname so cloud-init
            # doesn't re-derive it from the FQDN either. Tenants are standalone; no search domain.
            "name": hostname.split(".")[0],
        }
        if password:
            cfg["cipassword"] = password  # enables root login at the console (SSH stays key-based)
        if cicustom:
            cfg["cicustom"] = cicustom  # vendor snippet: VM self-installs Caddy for its own TLS
        self.set_config(vmid, **cfg)

    def set_nic(self, vmid: int, mac: str, rate_mbps: int) -> None:
        s = get_settings()
        # rate is MB/s in PVE; convert from Mbps. firewall=1 enables the per-VM firewall.
        # tag pins the VM onto the isolated tenant VLAN (router VLAN interface) — never the raw LAN.
        self.set_config(
            vmid,
            net0=f"virtio={mac},bridge={s.TENANT_BRIDGE},tag={s.TENANT_VLAN_TAG},"
            f"rate={max(1, rate_mbps // 8)},firewall=1",
        )

    def resize_disk(self, vmid: int, disk: str, size_gb: int) -> None:
        # grow-only: PVE rejects shrink, and we never offer it
        self._req("PUT", f"/nodes/{self.node}/qemu/{vmid}/resize", data={"disk": disk, "size": f"{size_gb}G"})

    def set_resources(self, vmid: int, vcpus: int, memory_mb: int) -> None:
        # cpu=host passes the node's real CPU through. The PVE default (kvm64) hides
        # SSE4.2/POPCNT/AVX2, so modern native binaries hang or crash on tenant VMs.
        # Safe here: single standalone node, no live-migration compatibility to keep.
        self.set_config(vmid, cores=vcpus, memory=memory_mb, cpu="host")

    def delete(self, vmid: int) -> str:
        return self._req(
            "DELETE",
            f"/nodes/{self.node}/qemu/{vmid}",
            params={"purge": 1, "destroy-unreferenced-disks": 1},
        )

    # ── snapshots (ZFS-backed, instant point-in-time) ───────────────────
    def snapshot(self, vmid: int, name: str, description: str = "") -> str:
        return self._req(
            "POST",
            f"/nodes/{self.node}/qemu/{vmid}/snapshot",
            data={"snapname": name, "description": description},
        )

    def list_snapshots(self, vmid: int) -> list[dict]:
        # PVE always includes a synthetic "current" entry; the caller filters it out.
        return self._req("GET", f"/nodes/{self.node}/qemu/{vmid}/snapshot") or []

    def delete_snapshot(self, vmid: int, name: str) -> str:
        return self._req("DELETE", f"/nodes/{self.node}/qemu/{vmid}/snapshot/{name}")

    def rollback(self, vmid: int, name: str) -> str:
        return self._req("POST", f"/nodes/{self.node}/qemu/{vmid}/snapshot/{name}/rollback")

    # ── backup-token verbs (PBS-backed, off-host — survives losing the hypervisor) ──
    def backup(self, vmid: int, storage: str) -> str:
        """Snapshot-mode vzdump to the PBS storage; works on a running VM."""
        return self._req(
            "POST",
            f"/nodes/{self.node}/vzdump",
            data={"vmid": vmid, "storage": storage, "mode": "snapshot"},
        )

    def list_backups(self, vmid: int, storage: str) -> list[dict]:
        return (
            self._req(
                "GET",
                f"/nodes/{self.node}/storage/{storage}/content",
                params={"content": "backup", "vmid": vmid},
            )
            or []
        )

    def restore_backup(self, vmid: int, archive: str) -> str:
        """Restore the archive OVER the existing VM (disks are replaced; config, MAC and
        firewall come back from the backup). VM.Backup suffices when the vmid exists."""
        return self._req(
            "POST",
            f"/nodes/{self.node}/qemu",
            data={"vmid": vmid, "archive": archive, "force": 1},
        )

    def _ensure_ipset(self, base: str, name: str, cidrs: set[str]) -> None:
        """Make ipset `name` contain exactly `cidrs` (create if missing, add/remove diff)."""
        names = {ips["name"] for ips in self._req("GET", f"{base}/ipset")}
        if name not in names:
            self._req("POST", f"{base}/ipset", data={"name": name})
        have = {e["cidr"] for e in (self._req("GET", f"{base}/ipset/{name}") or [])}
        for cidr in cidrs - have:
            self._req("POST", f"{base}/ipset/{name}", data={"cidr": cidr})
        for cidr in have - cidrs:
            self._req("DELETE", f"{base}/ipset/{name}/{cidr}")

    # ── anti-spoof + tenant isolation (generated — never hand-edited) ───
    def apply_ipfilter(self, vmid: int, ip: str, mac: str, peer_ips: list[str] | None = None) -> None:
        """Pin the VM to its allocated IP+MAC (anti-spoof) AND default-deny inbound so tenants
        on the shared VLAN can't reach each other at L2. Inbound is allowed only from: the edge
        (ingress), the control plane (readiness :22), and this owner's other servers in the
        `wy-peers` ipset (empty unless the owner enabled private networking). Cross-account is
        therefore never reachable. Idempotent."""
        s = get_settings()
        base = f"/nodes/{self.node}/qemu/{vmid}/firewall"
        self._ensure_ipset(base, "ipfilter-net0", {ip})  # anti-spoof: the VM's own source IP
        self._ensure_ipset(base, "wy-peers", set(peer_ips or []))  # same-owner peers (opt-in)

        # static inbound allow rules, idempotent by comment
        want = [
            {"comment": "wy-edge", "source": s.EDGE_WG_IP},
            {"comment": "wy-cp-readiness", "source": s.CONTROL_PLANE_IP, "proto": "tcp", "dport": "22"},
            {"comment": "wy-peers", "source": "+wy-peers"},
        ]
        have = {r.get("comment") for r in (self._req("GET", f"{base}/rules") or [])}
        for rule in want:
            if rule["comment"] not in have:
                self._req("POST", f"{base}/rules", data={"type": "in", "action": "ACCEPT", "enable": 1, **rule})

        # default-deny inbound; outbound stays open (the tenant firewall egress-filters). Established/related
        # return traffic is always allowed by PVE's stateful firewall.
        self._req(
            "PUT",
            f"{base}/options",
            data={"enable": 1, "ipfilter": 1, "macfilter": 1, "policy_in": "DROP", "policy_out": "ACCEPT"},
        )

    def set_peers(self, vmid: int, peer_ips: list[str]) -> None:
        """Update just the same-owner peer allow-list (when the owner's fleet or their private-
        networking setting changes) without touching the rest of the firewall."""
        self._ensure_ipset(f"/nodes/{self.node}/qemu/{vmid}/firewall", "wy-peers", set(peer_ips or []))

    # ── power-token verbs ───────────────────────────────────────────────
    def start(self, vmid: int) -> str:
        return self._req("POST", f"/nodes/{self.node}/qemu/{vmid}/status/start")

    def shutdown(self, vmid: int, timeout: int = 60) -> str:
        return self._req(
            "POST", f"/nodes/{self.node}/qemu/{vmid}/status/shutdown", data={"timeout": timeout, "forceStop": 1}
        )

    def stop(self, vmid: int) -> str:
        return self._req("POST", f"/nodes/{self.node}/qemu/{vmid}/status/stop")

    def reboot(self, vmid: int, timeout: int = 60) -> str:
        return self._req("POST", f"/nodes/{self.node}/qemu/{vmid}/status/reboot", data={"timeout": timeout})

    # ── console-token verbs ─────────────────────────────────────────────
    def vncproxy(self, vmid: int) -> dict:
        """Returns {ticket, port, ...}; the API layer wraps this in its own
        short-lived, user+vmid-bound, one-time ticket before it reaches a browser."""
        return self._req("POST", f"/nodes/{self.node}/qemu/{vmid}/vncproxy", data={"websocket": 1})
