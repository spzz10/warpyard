# Installing Warpyard from scratch

This walks a brand-new deployment end-to-end. Expect a weekend the first time: most of
the work is infrastructure (Proxmox roles, a VLAN, WireGuard), not the app itself.

## What you need

| Piece | Requirement |
|---|---|
| **Domain** | one you control, with API-less DNS fine (a wildcard A record is enough) |
| **Tenant hypervisor** | a Proxmox VE host, ideally standalone (not clustered with other workloads), with a VLAN-aware bridge and ZFS storage for tenant disks |
| **Router/firewall** | VLAN-capable + WireGuard-capable (pfSense/OPNsense/VyOS/Linux). Must be able to firewall the tenant VLAN |
| **Control plane** | a small VM (2GB is fine) on a machine **other than** the tenant hypervisor — Ubuntu 24.04 assumed by the provision script |
| **Edge VPS** | any $5-ish public VPS (1GB). Gets your DNS, Caddy, WireGuard |
| Optional | Proxmox Backup Server (off-host backups) · a [Resend](https://resend.com) token (invite/alert email) · a [PoppaPing](https://poppaping.com) account (uptime monitoring) |

Pick your numbers up front (defaults shown, all set in `.env`):

- Tenant VLAN tag (`66`) and subnet (`10.66.0.0/24`, gateway `10.66.0.1`)
- WireGuard transfer net (`10.10.66.0/30`: firewall `.1`, edge `.2`)
- Tenant vmid range (`90000–99999`)
- Base domain (`BASE_DOMAIN`), dashboard host (`app.<domain>`), MCP host
  (`mcp.<domain>`), edge host (`edge.<domain>`)

## 1. DNS

Point at the edge VPS's public IP:

```
<domain>            A    <edge-ip>
*.<domain>          A    <edge-ip>
```

(`app.`, `mcp.`, `edge.`, and every tenant `<name>.` all ride the wildcard.)

## 2. Proxmox host

Follow [`PVE-SETUP.md`](PVE-SETUP.md) top to bottom:

1. Create the `tenants` pool, the `WY*` roles, the `warpyard@pve` user and its four
   tokens; grant the ACLs (pool, storage, SDN, node). Save the four token secrets.
2. Build at least one golden template (Ubuntu 24.04 recommended) with console
   autologin + quiet cloud-init, and grant its `/vms/<vmid>` ACLs.
3. Make sure the tenant bridge is VLAN-aware and reaches your router as a trunk.
4. Run the E2E proof at the bottom of that doc **via the API tokens** before moving on
   — it catches ACL mistakes in minutes instead of during your first real provision.

Optional now or later: the encrypted dataset, the TLS-passthrough snippet, PBS.

## 3. Network

Follow [`NETWORK.md`](NETWORK.md):

1. Tenant VLAN on the router: gateway + subnet, **no DHCP**, the four firewall rules
   (block self, block RFC1918, block tcp/25, pass to internet).
2. WireGuard tunnel: router ↔ edge VPS, with the pass rule from the WG /30 into the
   tenant subnet, and `AllowedIPs` on the edge covering the tenant subnet **and the
   control-plane IP**.
3. Verify with a throwaway VM using the table in that doc.

## 4. Control plane

On the CP VM, as root:

```bash
rsync -a <this repo>/ /opt/warpyard/          # or git clone
cd /opt/warpyard
cp .env.example .env.local                     # fill EVERYTHING in — see the comments
deploy/provision-cp.sh                         # postgres, venv, migrations, systemd units, ufw
```

`provision-cp.sh` is idempotent and refuses to run without `.env.local`. It installs
three units: `warpyard-api` (uvicorn :8000), `warpyard-worker`, and
`warpyard-reconciler.timer`.

Seed plans, images, the tenant IP pool, and your admin account:

```bash
# edit scripts/seed_dev.py first: admin email/password, template vmids, IP pool range
sudo -u warpyard env $(grep -v '^#' .env.local | xargs) \
  .venv/bin/python scripts/seed_dev.py /path/to/your_ssh_key.pub
```

Sanity check: `curl http://<cp-ip>:8000/healthz` → `{"status":"ok"}`, and log into
`http://<cp-ip>:8000` with the seeded admin.

## 5. Edge VPS

1. Harden it: non-root sudo user, key-only SSH, UFW (allow 22/80/443 tcp, 443 udp,
   your WG port udp), fail2ban, unattended-upgrades.
2. Bring up WireGuard (`wg-quick@wg0`) and confirm you can `curl <cp-ip>:8000/healthz`
   across the tunnel.
3. Generate the edge sync secret once — `openssl rand -hex 24` — and put the **same
   value** in the CP's `.env.local` (`EDGE_SYNC_TOKEN=`) and the agent unit below.
4. Install Caddy **with the caddy-l4 module** (build via `xcaddy` or download with the
   module from Caddy's build service), then:

   ```bash
   # config (replace example.com with your domain, <control-plane-ip> with the CP)
   sudo cp edge/Caddyfile /etc/caddy/Caddyfile
   sudo mkdir -p /etc/caddy/routes.d /etc/caddy/layer4.d /etc/caddy/welcome
   sudo cp edge/welcome/index.html /etc/caddy/welcome/   # same substitutions

   # the two platform services (paths match the units' ExecStart)
   sudo apt install -y socat
   sudo cp edge/warpyard-authorizer edge/warpyard-edge-agent.py /usr/local/bin/
   sudo mv /usr/local/bin/warpyard-edge-agent.py /usr/local/bin/warpyard-edge-agent
   sudo chmod 755 /usr/local/bin/warpyard-authorizer /usr/local/bin/warpyard-edge-agent
   sudo cp edge/systemd/*.service /etc/systemd/system/
   # edit both units: WARPYARD_BASE_DOMAIN, WARPYARD_CP_URL, WARPYARD_EDGE_TOKEN
   sudo systemctl daemon-reload
   sudo systemctl enable --now warpyard-authorizer warpyard-edge-agent caddy
   ```

   The authorizer gates on-demand cert issuance (only hosts with a live route);
   the agent renders routes, reloads Caddy, and manages the per-server socat
   forward units + their UFW rules.
5. `https://app.<domain>` should now serve the dashboard through the edge (the apex
   redirects there; swap in your own landing page in the Caddyfile if you want one).

## 6. First server

Create a server in the dashboard. Watch it go `provisioning → booting → running`, then:

- `https://<name>.<domain>` shows the Warpyard welcome page (nothing listening on :80
  yet — that's the branded 502 fallback).
- `ssh -p <shown port> root@edge.<domain>` gets you in with your seeded key.
- The browser console opens straight to a root shell.

If provisioning sticks, read the server's Activity feed and
`journalctl -u warpyard-worker` on the CP — job errors are explicit (`403` = a missing
Proxmox ACL; `got timeout` on clone = see PVE gotcha #3).

## 7. Optional pieces

- **Backups**: PBS datastore + `warpyard@pbs` token (`PVE-SETUP.md`), then
  `PBS_*`/`BACKUP_*` in `.env.local`. Do one restore drill before trusting it.
- **Email**: `WARPYARD_RESEND_TOKEN` + `MAIL_FROM` on a Resend-verified domain —
  invites, password resets, and down-alerts start sending; without it, links render
  in the UI instead.
- **At-rest encryption + TLS passthrough**: `PVE-SETUP.md` optional sections.
- **App/game images**: `APP-IMAGES.md` / `GAME-IMAGES.md`. Register images
  `deprecated`, flip `active` only after each verifies end-to-end.
- **MCP**: works as soon as `mcp.<domain>` resolves (it rides the edge Caddyfile) —
  members add `https://mcp.<domain>/mcp` to their AI client and sign in via OAuth.

## Updating

`git pull` (or rsync), then re-run `deploy/provision-cp.sh` — it re-installs deps,
runs `alembic upgrade head`, and restarts the units. The edge (Caddyfile, agent,
welcome page) is deployed manually — it changes rarely.
