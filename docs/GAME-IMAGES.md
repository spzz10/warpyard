# Game server images (LinuxGSM)

Warpyard hosts game servers, not just websites. The HTTP reverse-proxy (`<name>.<base domain>`,
port 80) is only for web apps — game servers use **raw TCP/UDP forwards** through the edge (the
same socat/WireGuard path SSH uses), so any port works.

## How it fits together

An image row drives everything (`app/models.Image`):

| field | meaning |
|-------|---------|
| `category` | `os` \| `app` \| `game` — groups the create picker |
| `lgsm_game` | the LinuxGSM server code (`mcserver`, `vhserver`, …) baked into the template |
| `ports` | CSV `proto:port` to forward on the edge, e.g. `tcp:25565` or `udp:2456,udp:2457` |
| `guidance` | connect/usage text shown on the server page (`{endpoint}` → the public address) |

On create, the worker adds an `EdgeMapping` per declared port (public port =
`GAME_FORWARD_BASE + instance.id*4 + idx`, so ≤4 ports/VM). The edge agent renders a socat unit
+ UFW rule per mapping (TCP **and** UDP). `service.connect_info()` turns those mappings into the
public endpoints (`edge.<base domain>:<port>`) shown on the server page under **Connect / Play**,
with the image's guidance rendered underneath.

**Any new image must set `guidance`** so users know how to use it — that's surfaced automatically.

## Add a game

1. Build its LinuxGSM template on the tenant host (see the Minecraft example below / `deploy/game-image/`).
   Bake LGSM + the game's runtime; let the server install itself on first boot via a
   `warpyard-<game>.service` (keeps the template small).
2. Register an image row: `category="game"`, `lgsm_game="<code>"`, `ports="…"`, `guidance="…"`,
   `template_vmid=<the template>`.
3. That's it — it shows up in the "Game servers" group on the create page, gets its ports
   forwarded, and shows a connect address once running.

## Minecraft (the first game) — `slug=minecraft`, template 9003

Built by `deploy/game-image/build-mc-template.sh` on the tenant host. It bakes LinuxGSM `mcserver` +
**OpenJDK 25** (current Minecraft needs Java 25), a `mcserver` user, and `warpyard-mc.service`
which on first boot runs `mcserver auto-install`, accepts the EULA, and starts the server.

- **Port:** `tcp:25565` → players connect to `edge.<base domain>:<forwarded-port>` (shown on the page).
- **RAM:** LGSM `javaram="1024"` (note: LGSM appends the `M` itself — a value like `"1024M"`
  produces the broken `-Xmx1024MM`). Recommend the **wy-2-2** plan or larger.
- **First boot** downloads the server + generates the world — a few minutes before it answers.

## Generic game tooling (`build-game-template.sh`)

Beyond Minecraft (which needs a specific JDK, so it has its own `build-mc-template.sh`), most
LGSM games use a **generic** flow — `deploy/game-image/build-game-template.sh <vmid> <name>
<lgsm_gameserver> [extra_apt_deps]`. It bakes LGSM + a `gameserver` user + a game-agnostic
`warpyard-game.service` that reads the game code from `/etc/warpyard-game.env` and installs +
starts it on first boot. Steam games just add `lib32gcc-s1,lib32stdc++6`.

**⚠️ Always look up the real LGSM code** in the authoritative list before building — guessing
burns a whole template build. The script/gameserver name is `<short>server`:
`curl -s https://raw.githubusercontent.com/GameServerManagers/LinuxGSM/master/lgsm/data/serverlist.csv`
(e.g. Terraria is `terrariaserver`, NOT `tsserver` — `tsserver` is *The Specialists*; Factorio
is `fctrserver`, not `factorioserver`.)

**systemd gotcha:** don't use `ExecStart=/home/gameserver/${GAME} start` — systemd does NOT
expand env vars in the executable-path position (→ exit 203). Use a fixed-path wrapper
(`warpyard-game-ctl`) that reads the env file and execs.

## Example catalog (from the original deployment)

| image | LGSM | status | notes |
|-------|------|--------|-------|
| minecraft | mcserver | ✅ live | verified end-to-end (public server-list ping) |
| factorio | fctrserver | ✅ live | verified (UDP bound) |
| minecraft-bedrock | mcbserver | ✅ live | verified (UDP bound) |
| terraria | terrariaserver | ⏳ deprecated | install leaves `TerrariaServer` missing — needs a per-game pass |
| valheim | vhserver | ⏳ deprecated | Steam download works (~1.7 GB) but service start flaky — needs validation |
| project-zomboid | pzserver | ⏳ deprecated | built, not yet validated (same Steam path as Valheim) |

Non-game **`docker`** image (`category=app`, template 9009): Docker + Compose baked; a container
on port 80 is served at the server's `<name>.<base domain>` URL. Verified (ran nginx).

Deprecated = hidden from the create picker (so no one provisions a broken server); re-activate by
setting `Image.status="active"` once validated.

### Gotchas found while building it
- The install helper must be **readable** by the `mcserver` user (`chmod 755`, not `chmod +x` —
  a script needs read to run, and `751` breaks it with exit 126).
- `javaram` is a bare number (`1024`), not `1024M`.
- Match the JDK to the game: modern Minecraft rejected Java 21 (class version 65 vs 69) → Java 25.
- Verified end-to-end with a real Minecraft server-list ping to the public edge endpoint.
