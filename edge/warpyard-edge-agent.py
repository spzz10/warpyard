#!/usr/bin/env python3
"""Warpyard edge agent — runs ON the edge box (a small public VPS).

Polls the control plane's GET /edge/routes over the WireGuard tunnel, renders one
Caddy snippet per HTTP route into /etc/caddy/routes.d/, and reloads Caddy only when
the rendered set changes. The on-demand-TLS authorizer reads the same snippets, so a
host can get a cert exactly when it has a route (no wildcard cert, no DNS token here).

Config via env:
  WARPYARD_CP_URL    e.g. http://10.66.0.2:8000   (control plane, reachable over WG)
  WARPYARD_EDGE_TOKEN  shared bearer (matches control plane EDGE_SYNC_TOKEN)
  WARPYARD_POLL       seconds between polls (default 15)
"""

import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.request

ROUTES_DIR = "/etc/caddy/routes.d"  # edge-terminated hosts (reverse_proxy to VM:80)
L4_DIR = "/etc/caddy/layer4.d"  # passthrough hosts (SNI -> VM:443, edge never decrypts)
CP_URL = os.environ.get("WARPYARD_CP_URL", "http://10.66.0.2:8000")
TOKEN = os.environ.get("WARPYARD_EDGE_TOKEN", "")
POLL = int(os.environ.get("WARPYARD_POLL", "15"))
# where Caddy reaches the control plane for the default-favicon fallback (same upstream
# the dashboard's Caddy block proxies to; defaults to the CP_URL host:port)
CP_PROXY = os.environ.get("WARPYARD_CP_PROXY", CP_URL.split("://", 1)[-1].rstrip("/"))


def fetch():
    req = urllib.request.Request(f"{CP_URL}/edge/routes", headers={"Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def render_snippet(route: dict) -> str:
    host = route["hostname"]
    safe = "".join(c if c.isalnum() else "_" for c in host)
    label = host.split(".")[0]
    # /favicon.ico tries the tenant first; a miss (no favicon / site down) falls back to
    # the control plane's generated monogram so every site gets a tab icon by default
    return (
        f"@{safe} host {host}\n"
        f"handle @{safe} {{\n"
        f"\t@{safe}_fav path /favicon.ico\n"
        f"\thandle @{safe}_fav {{\n"
        f"\t\treverse_proxy {route['upstream']} {{\n"
        f"\t\t\t@favmiss status 403 404 405 500 502 503\n"
        f"\t\t\thandle_response @favmiss {{\n"
        f"\t\t\t\trewrite * /fav/{label}.png\n"
        f"\t\t\t\treverse_proxy {CP_PROXY}\n"
        f"\t\t\t}}\n"
        f"\t\t}}\n"
        f"\t}}\n"
        f"\thandle {{\n"
        f"\t\treverse_proxy {route['upstream']}\n"
        f"\t}}\n"
        f"}}\n"
    )


def render_l4(route: dict) -> str:
    """A caddy-l4 SNI-passthrough snippet: forward this host's encrypted :443 straight to the
    VM's own Caddy (which holds the cert). Imported inside the global `layer4 { :443 { ... } }`
    block, ahead of the default route that hands everything else to the local terminator."""
    host = route["hostname"]
    safe = "".join(c if c.isalnum() else "_" for c in host)
    return f"@{safe} tls sni {host}\nroute @{safe} {{\n\tproxy {route['https_upstream']}\n}}\n"


def _sync_dir(directory: str, suffix: str, desired: dict) -> bool:
    """Reconcile `directory` to exactly `desired` ({filename: content}); True if anything changed."""
    os.makedirs(directory, exist_ok=True)
    changed = False
    existing = set(os.listdir(directory)) if os.path.isdir(directory) else set()
    for fname in existing - desired.keys():
        if fname.endswith(suffix):
            os.remove(os.path.join(directory, fname))
            changed = True
    for fname, content in desired.items():
        path = os.path.join(directory, fname)
        old = open(path).read() if os.path.exists(path) else None
        if old != content:
            with open(path, "w") as f:
                f.write(content)
            changed = True
    return changed


def sync(state: dict) -> bool:
    """Write route snippets; return True if anything changed on disk. Each HTTP route goes to
    exactly one dir: passthrough hosts -> layer4.d (SNI->VM:443), the rest -> routes.d (edge
    terminates, reverse_proxy VM:80)."""
    routes = state.get("http", [])
    terminate = {f"{r['hostname']}.caddy": render_snippet(r) for r in routes if not r.get("passthrough")}
    passthrough = {f"{r['hostname']}.l4": render_l4(r) for r in routes if r.get("passthrough")}
    changed_t = _sync_dir(ROUTES_DIR, ".caddy", terminate)
    changed_l4 = _sync_dir(L4_DIR, ".l4", passthrough)
    return changed_t or changed_l4


def reload_caddy():
    subprocess.run(["caddy", "reload", "--config", "/etc/caddy/Caddyfile", "--force"], check=False)


# ---- L4 (TCP) forwards: one socat systemd unit per mapping, edge:public -> tenant:target
# over WG. socat originates the inner connection from the edge, so return routing needs no
# DNAT/masquerade and UFW just needs the public port opened. ----
UNIT_PREFIX = "warpyard-fwd-"


def _sh(*args):
    subprocess.run(args, check=False, capture_output=True)


def _unit_proto_port(unit: str) -> tuple[str, str]:
    # 'warpyard-fwd-tcp-25084' -> ('tcp','25084'); legacy 'warpyard-fwd-2201' -> ('tcp','2201')
    parts = unit[len(UNIT_PREFIX) :].split("-")
    if len(parts) == 2 and parts[0] in ("tcp", "udp"):
        return parts[0], parts[1]
    return "tcp", parts[-1]


def sync_l4(mappings: list) -> None:
    want = {}  # unit name -> (proto, public_port, upstream)
    for m in mappings:
        proto = m.get("protocol")
        if proto not in ("tcp", "udp"):
            continue
        port = int(m["public_port"])
        want[f"{UNIT_PREFIX}{proto}-{port}"] = (proto, port, m["upstream"])

    have = set()
    for f in os.listdir("/etc/systemd/system"):
        if f.startswith(UNIT_PREFIX) and f.endswith(".service"):
            have.add(f[:-8])

    changed = False
    # remove stale forwards (incl. legacy un-protoed unit names)
    for unit in have - want.keys():
        proto, port = _unit_proto_port(unit)
        _sh("systemctl", "disable", "--now", f"{unit}.service")
        try:
            os.remove(f"/etc/systemd/system/{unit}.service")
        except FileNotFoundError:
            pass
        _sh("ufw", "delete", "allow", f"{port}/{proto}")
        changed = True

    # add/refresh wanted forwards
    for unit, (proto, port, upstream) in want.items():
        listen = "UDP-LISTEN" if proto == "udp" else "TCP-LISTEN"
        upstream_spec = "UDP" if proto == "udp" else "TCP"
        path = f"/etc/systemd/system/{unit}.service"
        content = (
            f"[Unit]\nDescription=Warpyard {proto.upper()} forward {port}\nAfter=network.target\n\n"
            f"[Service]\nExecStart=/usr/bin/socat {listen}:{port},fork,reuseaddr {upstream_spec}:{upstream}\n"
            f"Restart=always\n\n[Install]\nWantedBy=multi-user.target\n"
        )
        old = open(path).read() if os.path.exists(path) else None
        if old != content:
            with open(path, "w") as f:
                f.write(content)
            _sh("ufw", "allow", f"{port}/{proto}")
            changed = True
            if old is not None:
                _sh("systemctl", "daemon-reload")
            _sh("systemctl", "enable", "--now", f"{unit}.service")
            _sh("systemctl", "restart", f"{unit}.service")

    if changed:
        _sh("systemctl", "daemon-reload")
        print(f"synced {len(want)} L4 forwards", flush=True)


def main():
    last_hash = None
    while True:
        try:
            state = fetch()
            h = hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()
            if h != last_hash:
                if sync(state):
                    reload_caddy()
                    print(f"synced {len(state.get('http', []))} routes, reloaded caddy", flush=True)
                sync_l4(state.get("l4", []))
                last_hash = h
        except Exception as e:  # noqa: BLE001 — keep the agent alive across control-plane blips
            print(f"poll error: {e}", file=sys.stderr, flush=True)
        time.sleep(POLL)


if __name__ == "__main__":
    main()
