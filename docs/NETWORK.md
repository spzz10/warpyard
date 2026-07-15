# Warpyard network model

Two networks matter: an **isolated tenant VLAN** behind your router/firewall, and a
**WireGuard tunnel** from a small public edge VPS into that VLAN. Values below
(VLAN 66, `10.66.0.0/24`, `10.10.66.0/30`) are the defaults in `app/config.py` — use
anything you like, just keep config and firewall in sync.

The reference implementation uses **pfSense**; any VLAN-capable firewall (OPNsense,
VyOS, MikroTik, plain Linux) can enforce the same four rules.

## Tenant VLAN

Tenant VMs are fully isolated from your LAN. Path:

```
tenant VM --(bridge, VLAN tag 66)--> Proxmox host --(trunk)--> firewall
   VLAN-66 interface 10.66.0.1/24  --(NAT)-->  WAN
```

- **Proxmox**: every tenant NIC is `virtio=<mac>,bridge=<TENANT_BRIDGE>,tag=<TENANT_VLAN_TAG>,firewall=1`
  (set by `ProxmoxClient.set_nic`). The bridge must be VLAN-aware. Templates keep an
  untagged NIC — the tag is applied per-instance at clone time.
- **Firewall**: the VLAN interface is gateway + subnet only; **no DHCP** (Warpyard's
  IPAM assigns static IPs via cloud-init). Make sure the tenant subnet is covered by
  your outbound NAT.

### Firewall rules on the tenant VLAN (order matters — first match wins)

1. block → the firewall itself — tenants cannot reach its web UI, SSH, or DNS.
2. block → all RFC1918 (10/8, 172.16/12, 192.168/16) — no LAN, no DMZ, no VPN nets.
3. block → tcp `:25` — no outbound SMTP (abuse posture).
4. pass → tenant net to any — internet egress only.

Because rule 2 blocks all RFC1918 before the pass, tenants can only reach the public
internet. DNS: tenants get public resolvers via cloud-init (`TENANT_NAMESERVERS`) since
your LAN DNS is unreachable by design.

### Verify your deployment (from a test tenant VM)

| Check | Expected |
|---|---|
| `curl https://example.com` | works |
| `ping 8.8.8.8` | works |
| reach any LAN address | blocked |
| reach the firewall (gateway IP) | blocked |
| reach another tenant's IP | blocked (per-VM firewall) |
| outbound tcp/25 | blocked |

Take a firewall config backup before you start; re-run this table after any firewall change.

## Ingress (edge VPS)

A small public VPS (any provider; 1GB is plenty) fronts ALL public tenant traffic — your
own IP never serves tenants. DNS: point the apex, `*.<base domain>`, and
`edge.<base domain>` at the edge's public IP.

Harden it first: key-only SSH, a non-root sudo user, UFW (deny in; allow 22/80/443 tcp +
443 udp + your WG port udp), fail2ban, unattended upgrades.

### Path

```
internet → edge :443 → Caddy → WireGuard → firewall → tenant VLAN → tenant VM
```

### WireGuard

- Firewall side: a `/30` (default `10.10.66.1` local), listening on a UDP port your WAN
  allows from the edge. Add a pass rule `src <WG /30> → dst <tenant subnet>`.
  (pfSense gotcha: assign the WG tunnel as an interface **with a static ipaddr** —
  assigning without one strips the WG-managed address. Rule fields for literal CIDRs
  must be `address`, not `network`.)
- Edge side: `wg0` (default `10.10.66.2` = `EDGE_WG_IP`), `AllowedIPs` = the WG /30 +
  the tenant subnet + the control-plane IP (the agent pulls routes from it), endpoint =
  your WAN IP:port, `wg-quick@wg0`. Return traffic is stateful (edge-initiated).

### Caddy (edge)

- Build: Caddy v2.11+ **with the `caddy-l4` module** (for TLS-passthrough servers).
  `edge/Caddyfile` is the template.
- Ingress TLS is **on-demand over HTTP-01** — no DNS-provider API token ever sits on
  the exposed edge box. Per-host routes are imported from `/etc/caddy/routes.d/*.caddy`
  (edge-terminated) and `/etc/caddy/layer4.d/*.l4` (SNI passthrough).
- **On-demand cert issuance is gated** by `warpyard-authorizer` (a tiny local HTTP
  service on 127.0.0.1:9280): Caddy asks before issuing; the authorizer approves only
  hosts that have a live route snippet (+ the apex). This stops randoms pointing DNS at
  your edge and exhausting ACME rate limits.
- **Edge agent** (`edge/warpyard-edge-agent.py`, systemd): polls the control plane's
  `GET /edge/routes` over WG (authenticated with `EDGE_SYNC_TOKEN`), renders one
  snippet per route, reloads Caddy only on change. It also reconciles per-port
  **socat forward units** (SSH = `SSH_FORWARD_BASE + instance id`; game ports from
  `GAME_FORWARD_BASE`) and their UFW allowances — install `socat` on the edge.
- Operational gotcha: `systemctl restart caddy` can hang draining live connections —
  set a short `grace_period` in the Caddyfile. `https_port` ≠ 443 (used by the
  passthrough layout) requires `auto_https disable_redirects` plus an explicit
  `:80` → `:443` redirect block; HTTP-01 challenges still pass.

### End-to-end TLS (SNI passthrough)

Web servers created with `tls_passthrough` terminate HTTPS **on the tenant VM** with
their own Let's Encrypt cert; the edge's `layer4` block routes by SNI and forwards
ciphertext. The VM self-installs Caddy on first boot via a cloud-init **vendor
snippet** (`TLS_SNIPPET`, a file you place on the Proxmox node's snippet-enabled
storage) — see `docs/PVE-SETUP.md`. Servers without passthrough (games, legacy) are
edge-terminated as plain reverse-proxy routes.
