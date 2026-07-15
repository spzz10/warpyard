# Warpyard — architecture

Warpyard is a self-hosted VM-leasing control plane: Proxmox provides the hypervisor,
Warpyard provides everything a small cloud provider needs on top of it — accounts and
invites, plans and images, provisioning, ingress with TLS, consoles, snapshots, backups,
monitoring, an API, and an MCP server so AI agents can drive it.

It is deliberately a **single-hypervisor, community-scale** design: a friends/club/lab
audience on one tenant host, fronted by one small public VPS. It is not (and does not
try to be) multi-region OpenStack.

```
                      internet
                         │
                ┌────────▼────────┐
                │  edge VPS       │  Caddy (on-demand TLS / SNI passthrough)
                │  *.example.com  │  + socat TCP/UDP forwards (SSH, game ports)
                └────────┬────────┘
                         │ WireGuard
                ┌────────▼────────┐
                │ router/firewall │  isolated tenant VLAN, no LAN access
                └────────┬────────┘
              ┌──────────┴────────────┐
    ┌─────────▼──────────┐  ┌─────────▼─────────┐
    │  control plane VM  │  │  Proxmox host     │
    │ FastAPI + Postgres │  │  tenant pool      │
    │ worker + reconciler│  │  golden templates │
    └────────────────────┘  └───────────────────┘
```

## Components

- **Control plane** (`app/`): FastAPI + Postgres. Server-rendered dashboard (Jinja +
  HTMX), REST API (`/api/v1`, Bearer keys), OAuth-protected MCP server (`/mcp`), and a
  curated `/docs` API reference. Runs in its own VM — **never on the hypervisor it
  manages**, so it can still observe/reconcile when that host is down, and a compromise
  of an unrelated service can't reach the Proxmox tokens.
- **Worker** (`app/jobs/`): Postgres-backed job queue (`FOR UPDATE SKIP LOCKED`).
  Every lifecycle action is an idempotent state-machine job with retries; enqueue and
  state transition commit in one transaction. See `docs/VERBS.md`.
- **Reconciler** (`app/reconciler.py`): periodically diffs DB desired-state vs Proxmox
  actual-state. Safe drift (power state) is auto-repaired with an audit event; anything
  billing- or security-affecting is flagged, never auto-fixed.
- **IPAM** (`app/ipam.py`): the control plane owns tenant IP allocation (no DHCP).
  The same source of truth generates each VM's cloud-init network config AND its
  Proxmox per-VM `ipfilter` anti-spoof rules — a tenant re-IPing inside their VM is
  dropped at the vNIC.
- **Edge agent** (`edge/warpyard-edge-agent.py`): runs on the public VPS, polls the
  control plane's `GET /edge/routes` over WireGuard, renders Caddy config per route,
  and manages per-port socat forwards for SSH/game traffic. See `docs/NETWORK.md`.
- **Provisioning**: linked clones of cloud-init golden templates (seconds, ZFS
  copy-on-write) or full clones onto an encrypted dataset when the user opts into
  at-rest encryption. Cloud-init injects hostname, the owner's SSH keys, and a static IP.

## Security model

The platform assumes **hostile tenants** and works outward from that:

1. **Dedicated, standalone hypervisor.** Tenant VMs live on one Proxmox host that is
   not clustered with anything else you run. Pool/token scoping contains the control
   plane; VLAN isolation contains the tenants.
2. **Privilege-split API tokens.** Four tokens (`power`, `config`, `console`,
   `backup`), each holding only the verbs its role needs, all scoped to the tenant
   pool. The power token cannot delete a VM; no token holds `Sys.*` write anywhere.
   Setup commands: `docs/PVE-SETUP.md`.
3. **Isolated tenant VLAN.** Tenants get internet egress only: the firewall blocks all
   RFC1918, the firewall itself, and outbound SMTP (`:25`). Tenants can't see your LAN,
   your other services, or each other's networks. See `docs/NETWORK.md`.
4. **Inter-tenant isolation at L2.** Same-VLAN traffic never crosses the router, so the
   per-VM Proxmox firewall default-denies inbound; only the edge (ingress) and the
   control plane (readiness probe) may reach a tenant. An owner can opt their own
   servers into reaching each other ("private networking"); cross-account is never
   reachable.
5. **No standing platform access to tenants.** Tenant VMs carry only the owner's SSH
   keys. The browser console (noVNC over a one-time, ownership-gated ticket) is the
   platform's only path in, and it's the user's path, not a backdoor.
6. **Public traffic never exposes your LAN.** Only the edge VPS has a public IP.
   Web servers can terminate their own TLS on the VM (SNI passthrough at the edge —
   the edge sees ciphertext only), matching what a commercial provider does.

## Design principles

- **Quotas before Proxmox**: per-user instance/vCPU/disk quotas enforced in the
  control plane, so a runaway user (or agent) can't DoS the hypervisor.
- **Bandwidth is the scarce resource** on a typical home/small uplink — plans carry
  NIC rate limits and monthly transfer accounting.
- **Grow-only resize**: disks grow, never shrink (data loss is not a plan change).
- **Every verb enumerated up front** (`docs/VERBS.md`) — retrofitting resize into a
  create/destroy-only state machine is painful, so the state machine knew all its
  states on day one.
- **Vendor everything**: no CDN scripts. htmx, idiomorph, noVNC and the chart code
  are all served from `app/static/`.
- **Best-effort email**: Resend integration degrades to showing links in the UI when
  unconfigured — email is a convenience, never a hard dependency.

## Feature surface (as built)

Create/start/stop/reboot/rebuild/resize/destroy · browser console (noVNC, auto-login) ·
SSH via per-instance edge port forwards · `<name>.<base domain>` HTTP(S) ingress ·
end-to-end TLS passthrough (opt-in per server) · at-rest disk encryption (opt-in) ·
custom domains with DNS pre-checks · snapshots (ZFS) + nightly off-host backups (PBS) ·
one-click app images (WordPress, Nextcloud, Gitea, …) · LinuxGSM game-server images
with raw TCP/UDP forwards · push-to-deploy web image (`docs/DEPLOY-IMAGE.md`) ·
player counts + metrics charts + host page · invites, per-member quotas, share board ·
REST API + MCP server with OAuth (PKCE + dynamic client registration) ·
optional uptime monitoring via PoppaPing.
