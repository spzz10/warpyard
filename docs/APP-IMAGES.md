# One-click app images

The app library = curated docker-compose stacks baked into golden templates, one template
per app (the same "bake the choice at build time" pattern as game images — per-VM choices
can't be passed at provision). Each app serves its web UI on **host port 80**, so the
server's `https://<name>.<base domain>` URL (and custom domains) just work — the edge
terminates TLS and proxies HTTP with `X-Forwarded-Proto: https`.

## Moving parts (`deploy/app-image/`)

| file | role |
|------|------|
| `apps/<slug>/compose.yml` | the stack, with `${WY_HOST}` / `${WY_SECRET}` / `${WY_SECRET2}` placeholders |
| `warpyard-app-install` | first-boot: substitutes placeholders, `docker compose up -d`, writes `/root/CREDENTIALS.txt` |
| `warpyard-app.service` | oneshot unit that runs the installer on every boot (idempotent) |
| `build-app-template.sh` | bakes one app template from the `wy-docker.img` source |
| `catalog.sh` | emits the SQL that registers/updates the `images` rows |

## The `${WY_*}` placeholder contract

Substituted by the installer at first boot (plain sed — only these three tokens):

- `${WY_HOST}` → `<label>.<base domain>` (the VM hostname is the short label)
- `${WY_SECRET}` / `${WY_SECRET2}` → 14-char per-server secrets, generated once and
  persisted in `/opt/warpyard-app/.secrets` (never rotated by re-runs)

Compose `$$` escapes (e.g. `$$_SERVER` in WORDPRESS_CONFIG_EXTRA) pass through untouched.

**CREDENTIALS.txt convention:** anything generated (admin passwords, tokens) is written to
`/root/CREDENTIALS.txt` (mode 600) on first boot; apps whose setup is browser-first get a
pointer instead. Image `guidance` must tell the user to read it via the Console.

## Build one app (on the tenant host, as root)

```bash
scp -r deploy/app-image root@<pve-host>:/root/wy-app-image
ssh root@<pve-host> '/root/wy-app-image/build-app-template.sh 9020 wordpress /root/wy-app-image'
```

Prereq: `wy-docker.img` exists in `/var/lib/vz/template/iso` (from
`deploy/game-image/build-docker-template.sh`). Templates land on vmpool as `tpl-app-<slug>`;
vmids 9020–9029 are the app range (see `catalog.sh`).

## Register + activate

```bash
./catalog.sh | sudo -u warpyard psql warpyard          # registers rows as status=deprecated (hidden)
# after an app verifies E2E:
sudo -u warpyard psql warpyard -c "UPDATE images SET status='active' WHERE slug='wordpress';"
```

Never activate an unverified app (see the Terraria row in GAME-IMAGES.md for why).

## Verify an app E2E

1. Create a server from the image (API/MCP/dashboard), wait for `running`.
2. First boot pulls the container images — allow 2–6 minutes (ollama-webui up to 10).
3. `curl -sI https://<label>.<base domain>` → expect the app (200/302), not the
   Warpyard welcome page (502 fallback).
4. Open the URL: the app's setup page (or login) renders over https with no
   mixed-content errors.
5. Where credentials are generated: `ssh` in / Console → `cat /root/CREDENTIALS.txt`
   and log in with them.
6. Reboot the VM once — the stack must come back by itself.

## Adding a new app

1. `apps/<newslug>/compose.yml` — web UI on host port 80, pinned image tags (never a tag
   where a silent major bump can eat data), named volumes, `restart: unless-stopped`,
   public-URL env vars set from `${WY_HOST}`.
2. If it generates a secret: add a case to `warpyard-app-install`'s CREDENTIALS block.
3. Add a row to `catalog.sh` (next free vmid, honest `default_plan` — check the plan's
   disk fits the images + data), build the template, verify, activate.

## Version-pinning notes / apps to watch (for the verifier)

- **nextcloud:31-apache** — if the tag doesn't pull, fall back to `nextcloud:30-apache`.
- **vaultwarden/server:1.33.2** — exact pin; bump patch if the tag is missing.
- **n8nio/n8n:stable** — `stable` is their rolling-stable tag; if missing use a pinned `1.x.y`.
- **jellyfin/jellyfin:latest** + **lscr.io/linuxserver/code-server:latest** +
  **ollama/ollama:latest** / **open-webui:main** — rolling by design (safe forward
  migrations); revisit if a bad release ships.
- **ghost:5** — requires MySQL 8 (bundled); Ghost 6 would be a deliberate migration.
- WordPress mixed-content: fixed via `WORDPRESS_CONFIG_EXTRA` X-Forwarded-Proto handling —
  if the admin shows http asset URLs, that snippet didn't land.
