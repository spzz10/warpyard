# Push-to-deploy web image (`web-2404`)

A Netlify-ish "git push → site live" image. The build and serve happen **entirely on the
tenant VM** — there is no control-plane build, no previews, and no per-deploy rollback. It's a
convenience image, not a build service.

## What a user does

A server created from the **web-2404** image shows a **Deploy** entry on its dashboard page
(and in the API/MCP `deploy` field):

```
git remote add warpyard ssh://root@edge.<base domain>:<ssh_port>/srv/site.git
git push warpyard main
```

`<ssh_port>` is the server's SSH forward port (same one shown for SSH access). Auth is the
user's SSH key (installed on the server, like normal SSH). The push runs the deploy hook and
streams its output back to the terminal.

## Hook contract (`/srv/site.git/hooks/post-receive`)

- Only **`main`** deploys; other refs are ignored with a message.
- The pushed tree is checked out to a temp dir.
- If **`package.json`** exists → `npm ci && npm run build`, then the first of
  `dist/ build/ out/ public/` is published. Otherwise the checkout itself is published (static
  sites, plain HTML).
- Publish is an **atomic symlink swap** of `/var/www/site` → a new `/var/www/releases/<ts>-<sha>`
  dir. Only the last two releases are kept (disk hygiene, not a rollback feature).
- **Caddy** (baked in, `/etc/caddy/Caddyfile`) serves `/var/www/site` on `:80` with SPA
  `try_files … /index.html` fallback. The edge terminates TLS and reverse-proxies to `:80`.

Image source of these files: `deploy/web-image/` (`post-receive`, `Caddyfile`, `caddy.service`,
`index.html`).

## Building / rebuilding the golden template (manual, host-side)

The template is **not** built from CI. On the tenant host, as root, from a checkout
that has `deploy/web-image/`:

```bash
cd /path/to/warpyard/deploy/web-image
./build-template.sh          # bakes caddy+node+git+hook into a copy of wy-ubuntu-2404.img → tpl 9002
```

The script (see `build-template.sh`) copies `wy-ubuntu-2404.img` (which already has console
autologin + quiet cloud-init), `virt-customize`s in Caddy + Node + the bare repo & hook, then
`qm create 9002` / `importdisk` / `template`, and adds the per-template clone ACLs:

```
pveum acl modify /vms/9002 --users  warpyard@pve         --roles WYConfig
pveum acl modify /vms/9002 --tokens warpyard@pve!config  --roles WYConfig
```

(the per-template ACL gotcha from `PVE-SETUP.md` — the clone right must be granted to BOTH the
user and the config token).

## Registering the image

Once template 9002 exists, add the image row (already in `scripts/seed_dev.py`; on a live DB):

```python
Image(slug="web-2404", name="Push-to-deploy (Caddy + Node)", distro="ubuntu",
      version="24.04", template_vmid=9002, min_disk_gb=10, status="active")
```

The control plane keys the Deploy surfacing off the slug `web-2404`
(`app/service.DEPLOY_IMAGE_SLUG`), so the slug must match.

## Limits (by design)

No control-plane builds, no preview deploys, no per-deploy rollback, no build caching beyond
`npm ci`. If a build fails, the previous release stays live and the error prints to the pusher's
terminal. For anything heavier, use a plain server image and run your own pipeline.
