"""Domain operations shared by the REST API (api_v1) and the MCP server. Framework-free:
takes a db Session + the acting User, returns plain dicts, and raises ServiceError on rule
violations so each caller can map it to its own error shape (HTTP status / MCP text)."""

import base64
import contextlib
import re
import struct
from datetime import UTC

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import dns_check, gamequery, poppaping, states, vmstats
from app.config import get_settings
from app.jobs import queue
from app.models import EdgeMapping, Event, HttpRoute, Image, Instance, Plan, SshKey, User, utcnow

# rough first-boot install time per game (minutes, incl. boot + download) — drives the
# self-clearing "still installing" hint on the connect card so a slow first boot (CS2 pulls
# ~30 GB) doesn't look stuck. A hint, not a readiness check.
GAME_INSTALL_MINUTES = {
    "minecraft": 4,
    "minecraft-bedrock": 3,
    "factorio": 2,
    "valheim": 6,
    "cs": 5,
    "cs2": 30,
}
DEFAULT_INSTALL_MINUTES = 5

LABEL_RE = r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$"
# a single DNS label: letters/digits/hyphen, not starting/ending with a hyphen
_DNS_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
FQDN_RE = re.compile(rf"^{_DNS_LABEL}(?:\.{_DNS_LABEL})+$")
ACTIONS = {"start", "stop", "reboot", "rebuild", "destroy"}
DEPLOY_IMAGE_SLUG = "web-2404"  # push-to-deploy golden image


class ServiceError(Exception):
    """A rule violation. `status` mirrors HTTP semantics (404/409/422)."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def domain_json(r: HttpRoute) -> dict:
    out = {"id": r.id, "hostname": r.hostname, "status": r.status, "url": f"https://{r.hostname}"}
    if r.status == "pending":
        out["dns_record"] = dns_check.required_record(r.hostname)  # what the user must create
    return out


def _ssh_port(i: Instance) -> int | None:
    m = next((m for m in i.edge_mappings if m.protocol == "tcp" and m.target_port == 22), None)
    return m.public_port if m else None


def parse_ports(csv: str) -> list[tuple[str, int]]:
    """'tcp:25565,udp:2456' -> [('tcp',25565),('udp',2456)]. Ignores junk."""
    out = []
    for tok in (csv or "").split(","):
        tok = tok.strip().lower()
        if ":" not in tok:
            continue
        proto, _, port = tok.partition(":")
        if proto in ("tcp", "udp") and port.isdigit():
            out.append((proto, int(port)))
    return out


def connect_info(i: Instance) -> dict | None:
    """Raw-port connection details for game/app servers: the public edge endpoints their
    forwarded ports land on, plus the image's usage guidance ({endpoint} -> first endpoint)."""
    game_maps = [m for m in i.edge_mappings if m.target_port != 22]  # 22 = the SSH forward
    if not game_maps:
        return None
    s = get_settings()
    # the wildcard *.<base domain> resolves to the edge and forwards are per-port, so the
    # server's own name works for raw-port connects too — friendlier than the bare edge host
    host = i.hostname or s.EDGE_HOST
    endpoints = [
        {"proto": m.protocol, "endpoint": f"{host}:{m.public_port}", "internal_port": m.target_port}
        for m in sorted(game_maps, key=lambda m: m.public_port)
    ]
    guidance = i.image.guidance
    if guidance:
        guidance = guidance.replace("{endpoint}", endpoints[0]["endpoint"])
    # live player count (cheap: gamequery caches per endpoint for 30s, misses included)
    players = None
    if i.status == states.RUNNING:
        qm = _players_query_map(i.image.slug, game_maps)
        players = gamequery.players_for_slug(i.image.slug, host, qm.public_port)
    # seconds left in the estimated first-boot install window (0 once it should be ready)
    install_secs_left = 0
    if i.image.lgsm_game and i.created_at:
        mins = GAME_INSTALL_MINUTES.get(i.image.slug, DEFAULT_INSTALL_MINUTES)
        created = i.created_at if i.created_at.tzinfo else i.created_at.replace(tzinfo=UTC)  # sqlite: naive
        elapsed = (utcnow() - created).total_seconds()
        install_secs_left = max(0, int(mins * 60 - elapsed))
    return {
        "endpoints": endpoints,
        "guidance": guidance,
        "game": i.image.lgsm_game or None,
        "install_secs_left": install_secs_left,
        "players": players,
    }


# A2S games that answer status queries on a port other than the join port
_QUERY_TARGET_PORT = {"valheim": 2457}


def _players_query_map(slug: str, game_maps: list) -> EdgeMapping:
    want = _QUERY_TARGET_PORT.get(slug)
    if want:
        for m in game_maps:
            if m.target_port == want:
                return m
    return min(game_maps, key=lambda m: m.public_port)


def deploy_info(i: Instance) -> dict | None:
    """Push-to-deploy git remote, only for servers built from the deploy image."""
    if i.image.slug != DEPLOY_IMAGE_SLUG:
        return None
    port = _ssh_port(i)
    if not port:
        return None
    s = get_settings()
    remote = f"ssh://{s.SSH_LOGIN_USER}@{s.EDGE_HOST}:{port}/srv/site.git"
    return {
        "git_remote": remote,
        "commands": [f"git remote add warpyard {remote}", "git push warpyard main"],
    }


def server_json(i: Instance) -> dict:
    s = get_settings()
    port = _ssh_port(i)
    return {
        "id": i.id,
        "name": i.label,
        "status": i.status,
        "hostname": i.hostname,
        "url": f"https://{i.hostname}" if i.hostname else None,
        "ssh": f"ssh -p {port} {s.SSH_LOGIN_USER}@{s.EDGE_HOST}" if port else None,
        "plan": i.plan.slug,
        "image": i.image.slug,
        "specs": {"vcpus": i.plan.vcpus, "memory_mb": i.plan.memory_mb, "disk_gb": i.plan.disk_gb},
        "private_ip": i.ip.address if i.ip else None,
        "domains": [domain_json(r) for r in i.http_routes if r.kind == "custom"],
        "deploy": deploy_info(i),
        "connect": connect_info(i),
        "image_guidance": i.image.guidance,  # usage note for app images without forwarded ports (e.g. Docker)
        "backups": {
            "enabled": i.backups_enabled,
            "last_backup_at": i.last_backup_at.isoformat() if i.last_backup_at else None,
        },
        "nightly_restart": {"enabled": i.restart_enabled, "at": i.restart_at},
        "shared": {"enabled": i.shared, "note": i.shared_note},
        "monitoring": bool(i.poppaping_monitor_id),
        "tls_passthrough": i.tls_passthrough,
        "encrypted": i.encrypted,
        "created_at": i.created_at.isoformat() if i.created_at else None,
    }


def live_servers(db: Session, user: User) -> list[Instance]:
    return db.scalars(
        select(Instance)
        .where(Instance.user_id == user.id, Instance.status.notin_(states.TERMINAL_STATES))
        .order_by(Instance.id)
    ).all()


def account_info(db: Session, user: User) -> dict:
    return {
        "email": user.email,
        "server_limit": user.max_instances,
        "servers_in_use": len(live_servers(db, user)),
        "private_network": user.private_network,
    }


def list_plans(db: Session) -> list[dict]:
    plans = db.scalars(select(Plan).where(Plan.is_active.is_(True)).order_by(Plan.price_cents)).all()
    return [
        {
            "slug": p.slug,
            "name": p.name,
            "vcpus": p.vcpus,
            "memory_mb": p.memory_mb,
            "disk_gb": p.disk_gb,
        }
        for p in plans
    ]


def list_images(db: Session) -> list[dict]:
    images = db.scalars(select(Image).where(Image.status == "active")).all()
    return [
        {
            "slug": im.slug,
            "name": im.name,
            "category": im.category,
            "default_plan": im.default_plan or None,
            "blurb": im.blurb or None,
        }
        for im in images
    ]


def list_servers(db: Session, user: User) -> list[dict]:
    return [server_json(i) for i in live_servers(db, user)]


def _owned_live(db: Session, user: User, server_id: int) -> Instance:
    i = db.get(Instance, server_id)
    if not i or i.user_id != user.id or i.status in states.TERMINAL_STATES:
        raise ServiceError(404, "No such server.")
    return i


def get_server(db: Session, user: User, server_id: int) -> dict:
    return server_json(_owned_live(db, user, server_id))


def create_server(
    db: Session,
    user: User,
    name: str,
    plan_slug: str,
    image_slug: str,
    tls_passthrough: bool | None = None,
    encrypted: bool = False,
) -> dict:
    name = (name or "").strip().lower()
    if not re.match(LABEL_RE, name):
        raise ServiceError(422, "Name must be lowercase letters, digits and hyphens (max 63).")
    plan = db.scalar(select(Plan).where(Plan.slug == plan_slug, Plan.is_active.is_(True)))
    image = db.scalar(select(Image).where(Image.slug == image_slug, Image.status == "active"))
    if not plan or not image:
        raise ServiceError(422, "Unknown plan or image (see list_plans / list_images).")
    # the name maps to <name>.<base domain>, which must be free — check up front so the
    # user gets a clear error instead of the provision job dying on the unique constraint
    hostname = f"{name}.{get_settings().BASE_DOMAIN}"
    taken = db.scalar(
        select(Instance).where(Instance.label == name, Instance.status.notin_(states.TERMINAL_STATES))
    ) or db.scalar(select(HttpRoute).where(HttpRoute.hostname == hostname))
    if taken:
        raise ServiceError(422, f"The name '{name}' is taken — {hostname} is already in use.")
    live = live_servers(db, user)
    if len(live) >= user.max_instances:
        raise ServiceError(422, f"Server limit reached ({user.max_instances}).")
    if (
        sum(x.plan.vcpus for x in live) + plan.vcpus > user.max_vcpus
        or sum(x.plan.disk_gb for x in live) + plan.disk_gb > user.max_disk_gb
    ):
        raise ServiceError(422, "vCPU or disk quota would be exceeded.")
    # End-to-end TLS: caller's choice, defaulting to on for web-serving images (os/app) and off
    # for games (which don't serve web). At-rest encryption is opt-in (off) — it needs a slower
    # full clone. Both are surfaced as toggles on the create form.
    passthrough = tls_passthrough if tls_passthrough is not None else image.category in ("os", "app")
    i = Instance(
        user_id=user.id,
        plan_id=plan.id,
        image_id=image.id,
        label=name,
        shared=user.share_by_default,
        tls_passthrough=passthrough,
        encrypted=encrypted,
    )
    db.add(i)
    db.flush()
    db.add(Event(user_id=user.id, instance_id=i.id, action="instance.create", status="started", detail={"via": "api"}))
    queue.enqueue(db, "instance.create", instance_id=i.id)
    db.commit()
    return server_json(i)


def server_action(db: Session, user: User, server_id: int, action: str) -> dict:
    if action not in ACTIONS:
        raise ServiceError(404, f"Unknown action. One of: {', '.join(sorted(ACTIONS))}.")
    i = _owned_live(db, user, server_id)
    verb = f"instance.{action}"
    if not states.can_enqueue(verb, i.status):
        raise ServiceError(409, f"Can't {action} a server that's {i.status}.")
    try:
        queue.enqueue(db, verb, instance_id=i.id)
    except queue.JobConflict as e:
        raise ServiceError(409, str(e)) from e
    db.add(Event(user_id=user.id, instance_id=i.id, action=verb, status="started", detail={"via": "api"}))
    db.commit()
    return {"status": "accepted", "action": action, "server_id": i.id}


def resize_server(db: Session, user: User, server_id: int, plan_slug: str) -> dict:
    i = _owned_live(db, user, server_id)
    new_plan = db.scalar(select(Plan).where(Plan.slug == plan_slug, Plan.is_active.is_(True)))
    if not new_plan or new_plan.id == i.plan_id:
        raise ServiceError(422, "Pick a different, valid plan.")
    if new_plan.disk_gb < i.plan.disk_gb:
        raise ServiceError(422, "Disk can only grow, not shrink.")
    if not states.can_enqueue("instance.resize", i.status):
        raise ServiceError(409, f"Can't resize a server that's {i.status}.")
    try:
        queue.enqueue(db, "instance.resize", instance_id=i.id, payload={"plan_id": new_plan.id})
    except queue.JobConflict as e:
        raise ServiceError(409, str(e)) from e
    db.add(
        Event(
            user_id=user.id,
            instance_id=i.id,
            action="instance.resize",
            status="started",
            detail={"via": "api", "to": new_plan.slug},
        )
    )
    db.commit()
    return {"status": "accepted", "server_id": i.id, "plan": new_plan.slug}


# ── backups (nightly vzdump to PBS + on-demand; retention is a PBS-side prune job) ──
def _backup_json(v: dict) -> dict:
    from datetime import datetime

    verify = (v.get("verification") or {}).get("state")  # ok | failed | None (not yet verified)
    return {
        "id": v["volid"],
        "created_at": datetime.fromtimestamp(v["ctime"], tz=UTC).isoformat() if v.get("ctime") else None,
        "size_gb": round(v.get("size", 0) / 1024**3, 2),
        "verified": verify,
    }


def list_backups(db: Session, user: User, server_id: int) -> list[dict]:
    from app.proxmox import ProxmoxClient, ProxmoxError

    i = _owned_live(db, user, server_id)
    if not i.vmid:
        return []
    try:
        vols = ProxmoxClient("backup").list_backups(i.vmid, get_settings().BACKUP_STORAGE)
    except ProxmoxError as e:
        raise ServiceError(502, "Backup storage is unreachable right now — try again shortly.") from e
    return sorted((_backup_json(v) for v in vols), key=lambda b: b["created_at"] or "", reverse=True)


def set_backups(db: Session, user: User, server_id: int, enabled: bool) -> dict:
    i = _owned_live(db, user, server_id)
    i.backups_enabled = bool(enabled)
    db.add(
        Event(
            user_id=user.id,
            instance_id=i.id,
            action="instance.backups_toggle",
            status="succeeded",
            detail={"enabled": i.backups_enabled},
        )
    )
    db.commit()
    return {"server_id": i.id, "backups_enabled": i.backups_enabled}


def backup_now(db: Session, user: User, server_id: int) -> dict:
    i = _owned_live(db, user, server_id)
    if not states.can_enqueue("instance.backup", i.status):
        raise ServiceError(409, f"Can't back up a server that's {i.status}.")
    try:
        queue.enqueue(db, "instance.backup", instance_id=i.id)
    except queue.JobConflict as e:
        raise ServiceError(409, str(e)) from e
    db.add(Event(user_id=user.id, instance_id=i.id, action="instance.backup", status="started", detail={"via": "api"}))
    db.commit()
    return {"status": "accepted", "server_id": i.id}


def restore_backup(db: Session, user: User, server_id: int, backup_id: str) -> dict:
    i = _owned_live(db, user, server_id)
    if not states.can_enqueue("instance.restore_backup", i.status):
        raise ServiceError(409, f"Can't restore a server that's {i.status}.")
    # the volid must be one of THIS server's backups — never trust a raw volid from a client
    if backup_id not in {b["id"] for b in list_backups(db, user, server_id)}:
        raise ServiceError(404, "No such backup on this server.")
    try:
        queue.enqueue(db, "instance.restore_backup", instance_id=i.id, payload={"volid": backup_id})
    except queue.JobConflict as e:
        raise ServiceError(409, str(e)) from e
    db.add(
        Event(
            user_id=user.id,
            instance_id=i.id,
            action="instance.restore_backup",
            status="started",
            detail={"via": "api", "volid": backup_id},
        )
    )
    db.commit()
    return {"status": "accepted", "server_id": i.id, "backup_id": backup_id}


def schedule_due_backups(db: Session) -> int:
    """Enqueue the nightly backup for every backups-enabled server that hasn't had one in
    the last ~20h. Called periodically by the worker; only acts during the configured UTC
    hour so backups land in the quiet window (PBS prune/GC/verify are scheduled after)."""
    from datetime import timedelta

    if utcnow().hour != get_settings().BACKUP_HOUR_UTC:
        return 0
    cutoff = utcnow() - timedelta(hours=20)
    due = db.scalars(
        select(Instance).where(
            Instance.backups_enabled.is_(True),
            Instance.status.in_(tuple(states.VERB_FROM_STATES["instance.backup"])),
            Instance.vmid.isnot(None),
            (Instance.last_backup_at.is_(None)) | (Instance.last_backup_at < cutoff),
        )
    ).all()
    n = 0
    for i in due:
        try:
            queue.enqueue(db, "instance.backup", instance_id=i.id)
            db.add(
                Event(
                    user_id=i.user_id,
                    instance_id=i.id,
                    action="instance.backup",
                    status="started",
                    detail={"via": "schedule"},
                )
            )
            n += 1
        except queue.JobConflict:
            continue  # busy with another job; tomorrow's run (or the 20h window) catches it
    if n:
        db.commit()
    return n


def schedule_due_restarts(db: Session) -> int:
    """Enqueue the opted-in nightly reboot — but never while anyone is playing:
    an occupied game server simply stays due and restarts as soon as it empties (the
    player query is cached ~30s and the worker re-checks every tick). A failed or
    unsupported player query must not wedge the schedule, so None proceeds.

    Due = past today's restart_at (UTC) and not yet fired since that moment. Anchoring
    to the scheduled time (rather than a rolling cooldown since the last fire) keeps
    the reboot at the same wall-clock time every day instead of drifting earlier."""
    now = utcnow()
    due = db.scalars(
        select(Instance).where(
            Instance.restart_enabled.is_(True),
            Instance.restart_at.isnot(None),
            Instance.status == states.RUNNING,
        )
    ).all()
    n = 0
    for i in due:
        try:
            hh, mm = (int(p) for p in i.restart_at.split(":"))
            sched_today = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        except (ValueError, AttributeError):
            continue  # a malformed schedule must never break the worker tick
        last = i.last_auto_restart_at
        if last is not None and last.tzinfo is None:
            last = last.replace(tzinfo=UTC)  # sqlite round-trips naive
        if now < sched_today or (last is not None and last >= sched_today):
            continue
        if not states.can_enqueue("instance.reboot", i.status):
            continue
        game_maps = [m for m in i.edge_mappings if m.target_port != 22]
        if game_maps:
            host = i.hostname or get_settings().EDGE_HOST
            qm = _players_query_map(i.image.slug, game_maps)
            players = gamequery.players_for_slug(i.image.slug, host, qm.public_port)
            if players and players.get("online", 0) > 0:
                continue  # hold while occupied; stays due for the next tick
        try:
            queue.enqueue(db, "instance.reboot", instance_id=i.id)
        except queue.JobConflict:
            continue  # something else is already happening to it
        i.last_auto_restart_at = now
        db.add(
            Event(
                user_id=i.user_id,
                instance_id=i.id,
                action="instance.auto_restart",
                status="started",
                detail={"via": "schedule", "at": i.restart_at},
            )
        )
        n += 1
    if n:
        db.commit()
    return n


# ── snapshots (local ZFS point-in-time; create/delete are synchronous, rollback is a job) ──
def _snapshot_name(name: str) -> str:
    # PVE snapshot names: letters/digits/_/-, must start with a letter
    return "snap" + "".join(c if (c.isalnum() or c == "-") else "-" for c in (name or "").strip().lower())[:36]


def list_snapshots(db: Session, user: User, server_id: int) -> list[dict]:
    from app.proxmox import ProxmoxClient, ProxmoxError

    i = _owned_live(db, user, server_id)
    if not i.vmid:
        return []
    try:
        snaps = ProxmoxClient("config").list_snapshots(i.vmid)
    except ProxmoxError as e:
        raise ServiceError(502, "Snapshots are unreachable right now — try again shortly.") from e
    return [{"name": s["name"], "description": s.get("description", "")} for s in snaps if s.get("name") != "current"]


def create_snapshot(db: Session, user: User, server_id: int, name: str = "") -> dict:
    from app.proxmox import ProxmoxClient, ProxmoxError

    i = _owned_live(db, user, server_id)
    if not i.vmid:
        raise ServiceError(409, "The server isn't provisioned yet.")
    safe = _snapshot_name(name)
    try:
        ProxmoxClient("config").snapshot(i.vmid, safe, description="via Warpyard")
    except ProxmoxError as e:
        raise ServiceError(502, "Couldn't take the snapshot — try again shortly.") from e
    db.add(
        Event(
            user_id=user.id,
            instance_id=i.id,
            action="instance.snapshot",
            status="succeeded",
            detail={"snapshot": safe},
        )
    )
    db.commit()
    return {"server_id": i.id, "snapshot": safe}


def delete_snapshot(db: Session, user: User, server_id: int, name: str) -> dict:
    from app.proxmox import ProxmoxClient, ProxmoxError

    i = _owned_live(db, user, server_id)
    # the name must be one of THIS server's snapshots — never pass a raw client string to PVE
    if name not in {s["name"] for s in list_snapshots(db, user, server_id)}:
        raise ServiceError(404, "No such snapshot on this server.")
    try:
        ProxmoxClient("config").delete_snapshot(i.vmid, name)
    except ProxmoxError as e:
        raise ServiceError(502, "Couldn't delete the snapshot — try again shortly.") from e
    return {"server_id": i.id, "deleted": name}


def rollback_snapshot(db: Session, user: User, server_id: int, name: str) -> dict:
    i = _owned_live(db, user, server_id)
    if name not in {s["name"] for s in list_snapshots(db, user, server_id)}:
        raise ServiceError(404, "No such snapshot on this server.")
    if not states.can_enqueue("instance.rollback", i.status):
        raise ServiceError(409, f"Can't roll back a server that's {i.status}.")
    try:
        queue.enqueue(db, "instance.rollback", instance_id=i.id, payload={"snapshot": name})
    except queue.JobConflict as e:
        raise ServiceError(409, str(e)) from e
    db.add(
        Event(
            user_id=user.id,
            instance_id=i.id,
            action="instance.rollback",
            status="started",
            detail={"snapshot": name},
        )
    )
    db.commit()
    return {"status": "accepted", "server_id": i.id, "snapshot": name}


# ── per-server settings (nightly restart window, share board) ──────────────────
RESTART_AT_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def set_restart_schedule(db: Session, user: User, server_id: int, enabled: bool, at: str | None = None) -> dict:
    i = _owned_live(db, user, server_id)
    if at:
        if not RESTART_AT_RE.match(at):
            raise ServiceError(422, "Time must be HH:MM (24-hour, UTC).")
        i.restart_at = at
    if enabled and not i.restart_at:
        i.restart_at = "09:00"  # sane default: early-morning US
    i.restart_enabled = bool(enabled)
    # count from now: the first reboot happens at the next occurrence of restart_at,
    # never immediately on enabling (or moving) a schedule whose time is already past
    i.last_auto_restart_at = utcnow()
    db.commit()
    return {"server_id": i.id, "nightly_restart": {"enabled": i.restart_enabled, "at": i.restart_at}}


def set_share(db: Session, user: User, server_id: int, enabled: bool, note: str | None = None) -> dict:
    """Show (or hide) the server on the members-only share board while it's running."""
    i = _owned_live(db, user, server_id)
    i.shared = bool(enabled)
    if note is not None:
        i.shared_note = note.strip()[:140] or None
    db.commit()
    return {"server_id": i.id, "shared": {"enabled": i.shared, "note": i.shared_note}}


def set_gate(db: Session, user: User, server_id: int, enabled: bool) -> dict:
    """Require (or stop requiring) a logged-in Warpyard member to reach the server's web
    ingress. Turning it on forces edge-terminated TLS (the edge must see the request to gate
    it), so the change reaches the edge on the next route sync (~15s)."""
    i = _owned_live(db, user, server_id)
    i.gated = bool(enabled)
    db.commit()
    return {"server_id": i.id, "gated": i.gated}


# ── monitoring (PoppaPing uptime checks + email alerts, on the user's own account) ──
def _monitor_spec(i: Instance) -> dict:
    """What a PoppaPing monitor for this server should check: A2S games get a real 'game'
    check, TCP games a port check, UDP-only games a TCP check on their SSH forward (proves
    the VM is up), everything else an HTTP check on the site."""
    connect = connect_info(i)
    host = i.hostname
    if connect and connect["endpoints"]:
        slug = i.image.slug
        if slug in ("cs", "cs2", "valheim"):  # PoppaPing's game check speaks A2S
            game_maps = [m for m in i.edge_mappings if m.target_port != 22]
            qm = _players_query_map(slug, game_maps)
            return {"type_": "game", "host": host, "port": qm.public_port}
        tcp = next((e for e in connect["endpoints"] if e["proto"] == "tcp"), None)
        if tcp:
            return {"type_": "tcp", "host": host, "port": int(tcp["endpoint"].rsplit(":", 1)[1])}
        ssh = next((m for m in i.edge_mappings if m.protocol == "tcp" and m.target_port == 22), None)
        if ssh:
            return {"type_": "tcp", "host": get_settings().EDGE_HOST, "port": ssh.public_port}
    return {"type_": "http", "url": f"https://{host}"}


def _attach_monitor(db: Session, user: User, i: Instance) -> None:
    """Adopt or create the PoppaPing monitor for this server, with the owner's email
    attached as the alert channel. Raises PoppaPingError with a user-showable message."""
    name = f"warpyard: {i.label}"
    mid = poppaping.find_monitor(user.poppaping_api_key, name)  # heal a previous half-create
    if not mid:
        channel = None
        with contextlib.suppress(poppaping.PoppaPingError):
            channel = poppaping.ensure_email_channel(user.poppaping_api_key, user.email)
        mid = poppaping.create_monitor(
            user.poppaping_api_key,
            name,
            alert_channel_ids=[channel] if channel else None,
            **_monitor_spec(i),
        )
    i.poppaping_monitor_id = mid
    db.commit()


def enable_monitoring(db: Session, user: User, server_id: int) -> dict:
    """One-click uptime monitoring: provision a PoppaPing account for the user's email if
    they haven't connected one, then create (or adopt) the monitor with email alerts."""
    i = _owned_live(db, user, server_id)
    if i.poppaping_monitor_id:
        return {"server_id": i.id, "monitoring": True}
    if not user.poppaping_api_key:
        secret = get_settings().WARPYARD_POPPAPING_PARTNER_SECRET
        if not secret:
            raise ServiceError(422, "Monitoring isn't available right now.")
        try:
            key = poppaping.provision_account(secret, user.email)
        except poppaping.PoppaPingError as e:
            raise ServiceError(502, e.message) from e
        if key is None:
            raise ServiceError(
                409,
                "You already have a PoppaPing account — create an API key there and connect it on your Account page.",
            )
        user.poppaping_api_key = key
        db.commit()
    try:
        _attach_monitor(db, user, i)
    except poppaping.PoppaPingError as e:
        raise ServiceError(502, e.message) from e
    return {"server_id": i.id, "monitoring": True}


def disable_monitoring(db: Session, user: User, server_id: int) -> dict:
    i = _owned_live(db, user, server_id)
    if i.poppaping_monitor_id and user.poppaping_api_key:
        with contextlib.suppress(poppaping.PoppaPingError):
            poppaping.delete_monitor(user.poppaping_api_key, i.poppaping_monitor_id)
    i.poppaping_monitor_id = None
    db.commit()
    return {"server_id": i.id, "monitoring": False}


MONITOR_PERIODS = ("24h", "7d", "30d", "90d")


def monitoring_data(db: Session, user: User, server_id: int, period: str = "24h") -> dict:
    """Uptime %, up/down check counts and response-time history from PoppaPing."""
    i = _owned_live(db, user, server_id)
    if not i.poppaping_monitor_id or not user.poppaping_api_key:
        raise ServiceError(404, "Monitoring isn't enabled on this server.")
    if period not in MONITOR_PERIODS:
        raise ServiceError(422, f"period must be one of: {', '.join(MONITOR_PERIODS)}.")
    try:
        return poppaping.monitor_history(user.poppaping_api_key, i.poppaping_monitor_id, period)
    except poppaping.PoppaPingError as e:
        raise ServiceError(502, "Monitoring data is unavailable right now — try again shortly.") from e


# ── metrics (CPU/memory/network history from the hypervisor) ───────────────────
def server_metrics(db: Session, user: User, server_id: int, timeframe: str = "hour") -> dict:
    from app.proxmox import ProxmoxError

    i = _owned_live(db, user, server_id)
    if not i.vmid:
        raise ServiceError(409, "The server isn't provisioned yet.")
    try:
        return vmstats.series(i.vmid, timeframe)
    except ValueError as e:
        raise ServiceError(422, str(e)) from e
    except ProxmoxError as e:
        raise ServiceError(502, "Metrics are unavailable right now — try again shortly.") from e


# ── account settings ────────────────────────────────────────────────────────────
def set_private_network(db: Session, user: User, enabled: bool) -> dict:
    """Let the user's OWN servers reach each other on the private VLAN (cross-account
    stays blocked either way). Re-pushes every server's peer allow-list."""
    from app.jobs.handlers import sync_owner_firewalls  # lazy: handlers imports service

    user.private_network = bool(enabled)
    db.commit()
    sync_owner_firewalls(db, user)
    return {"private_network": user.private_network}


# ── account SSH keys ───────────────────────────────────────────────────────────
# Keys are account-level and injected at server creation (and re-injected on rebuild), so the
# AI flow is: add_ssh_key -> create_server -> ssh in. Adding a key does NOT touch already-running
# servers until they're rebuilt.
def _key_json(k: SshKey) -> dict:
    parts = k.public_key.split()
    fp = parts[1][:24] + "…" if len(parts) >= 2 else k.public_key[:24] + "…"
    return {"id": k.id, "name": k.name, "type": parts[0] if parts else "", "preview": fp}


def valid_ssh_key(pk: str) -> bool:
    """Well-formed OpenSSH public key: the base64 blob must parse as SSH length-prefixed
    fields, consume exactly, and its first field (algorithm) must equal the type prefix.
    A structural-only check let a malformed key through to Proxmox, which 500s and wedged
    provisioning — so validate for real here."""
    parts = pk.split()
    if len(parts) < 2:
        return False
    ktype, blob = parts[0], parts[1]
    try:
        raw = base64.b64decode(blob, validate=True)
    except (ValueError, base64.binascii.Error):
        return False
    off = 0
    fields = []
    while off < len(raw):
        if off + 4 > len(raw):
            return False
        n = struct.unpack(">I", raw[off : off + 4])[0]
        off += 4
        if off + n > len(raw):
            return False
        fields.append(raw[off : off + n])
        off += n
    return off == len(raw) and bool(fields) and fields[0].decode("ascii", "replace") == ktype


def list_ssh_keys(db: Session, user: User) -> list[dict]:
    return [_key_json(k) for k in sorted(user.ssh_keys, key=lambda k: k.id)]


def add_ssh_key(db: Session, user: User, name: str, public_key: str) -> dict:
    pk = (public_key or "").strip()
    if not valid_ssh_key(pk):
        raise ServiceError(422, "That doesn't look like a valid SSH public key (e.g. 'ssh-ed25519 AAAA… you@host').")
    if any(k.public_key.split()[:2] == pk.split()[:2] for k in user.ssh_keys):
        raise ServiceError(409, "That key is already on your account.")
    k = SshKey(user_id=user.id, name=(name or "").strip()[:64] or "key", public_key=pk)
    db.add(k)
    db.commit()
    return _key_json(k)


def remove_ssh_key(db: Session, user: User, key_id: int) -> dict:
    k = db.get(SshKey, key_id)
    if not k or k.user_id != user.id:
        raise ServiceError(404, "No such SSH key.")
    db.delete(k)
    db.commit()
    return {"status": "removed", "id": key_id}


# ── custom domains ─────────────────────────────────────────────────────────────
def list_domains(db: Session, user: User, server_id: int) -> list[dict]:
    i = _owned_live(db, user, server_id)
    return [domain_json(r) for r in i.http_routes if r.kind == "custom"]


def add_domain(db: Session, user: User, server_id: int, hostname: str) -> dict:
    i = _owned_live(db, user, server_id)
    host = (hostname or "").strip().lower().rstrip(".")
    if "*" in host or not FQDN_RE.match(host) or len(host) > 253:
        raise ServiceError(422, "Enter a valid domain like app.example.com (no wildcards).")
    base = get_settings().BASE_DOMAIN
    if host == base or host.endswith("." + base):
        raise ServiceError(422, f"{base} names are reserved — you already get <name>.{base} for free.")
    customs = [r for r in i.http_routes if r.kind == "custom"]
    if len(customs) >= get_settings().MAX_CUSTOM_DOMAINS:
        raise ServiceError(422, f"At most {get_settings().MAX_CUSTOM_DOMAINS} custom domains per server.")
    # global case-insensitive uniqueness (hostname is stored lowercased)
    if db.scalar(select(HttpRoute.id).where(func.lower(HttpRoute.hostname) == host)):
        raise ServiceError(409, "That domain is already in use.")
    active = dns_check.resolves_to_edge(host)
    r = HttpRoute(
        instance_id=i.id,
        hostname=host,
        target_port=80,
        kind="custom",
        status="active" if active else "pending",
    )
    db.add(r)
    db.commit()
    return domain_json(r)


def remove_domain(db: Session, user: User, server_id: int, hostname: str) -> dict:
    i = _owned_live(db, user, server_id)
    host = (hostname or "").strip().lower().rstrip(".")
    r = next((r for r in i.http_routes if r.kind == "custom" and r.hostname == host), None)
    if not r:
        raise ServiceError(404, "No such custom domain on this server.")
    db.delete(r)
    db.commit()
    return {"status": "removed", "hostname": host}


def recheck_pending_domains(db: Session) -> int:
    """Flip pending custom domains to active once their DNS points at the edge. Called
    periodically by the worker (see app/jobs/worker.py)."""
    pending = db.scalars(
        select(HttpRoute)
        .join(Instance, HttpRoute.instance_id == Instance.id)
        .where(
            HttpRoute.kind == "custom",
            HttpRoute.status == "pending",
            Instance.status.notin_(states.TERMINAL_STATES),
        )
    ).all()
    flipped = 0
    for r in pending:
        if dns_check.resolves_to_edge(r.hostname):
            r.status = "active"
            flipped += 1
    if flipped:
        db.commit()
    return flipped
