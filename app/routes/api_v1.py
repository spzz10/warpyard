"""Public REST API (v1), authenticated with API keys. This is what a user points their AI
or scripts at. Thin HTTP layer over app.service (the same domain logic the MCP server uses)."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import service
from app.api_auth import api_user
from app.database import get_db
from app.models import User

router = APIRouter(prefix="/api/v1", tags=["api"])


class CreateBody(BaseModel):
    name: str = Field(pattern=service.LABEL_RE, max_length=63)
    plan: str
    image: str
    # end-to-end TLS (VM terminates its own HTTPS; edge sees ciphertext only). None = default
    # by image (on for os/app, off for games). encrypted = disk on the at-rest-encrypted pool.
    tls_passthrough: bool | None = None
    encrypted: bool = False


class ResizeBody(BaseModel):
    plan: str


class SshKeyBody(BaseModel):
    name: str = ""
    public_key: str


class DomainBody(BaseModel):
    hostname: str


class BackupsBody(BaseModel):
    enabled: bool


class RestoreBody(BaseModel):
    backup_id: str  # a volid from GET /servers/{id}/backups


class SnapshotBody(BaseModel):
    name: str = ""  # optional label; sanitized into the snapshot name


class RestartScheduleBody(BaseModel):
    enabled: bool
    at: str | None = None  # "HH:MM" 24-hour UTC; defaults to 09:00 when first enabled


class ShareBody(BaseModel):
    enabled: bool
    note: str | None = None  # up to 140 chars, shown next to the server on the board


class MonitoringBody(BaseModel):
    enabled: bool


class PrivateNetworkBody(BaseModel):
    enabled: bool


def _guard(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except service.ServiceError as e:
        raise HTTPException(e.status, e.message) from e


@router.get("/account")
def whoami(user: User = Depends(api_user), db: Session = Depends(get_db)):
    return service.account_info(db, user)


@router.get("/plans")
def list_plans(user: User = Depends(api_user), db: Session = Depends(get_db)):
    return service.list_plans(db)


@router.get("/images")
def list_images(user: User = Depends(api_user), db: Session = Depends(get_db)):
    return service.list_images(db)


@router.get("/servers")
def list_servers(user: User = Depends(api_user), db: Session = Depends(get_db)):
    return service.list_servers(db, user)


@router.get("/servers/{server_id}")
def get_server(server_id: int, user: User = Depends(api_user), db: Session = Depends(get_db)):
    return _guard(service.get_server, db, user, server_id)


@router.post("/servers", status_code=202)
def create_server(body: CreateBody, user: User = Depends(api_user), db: Session = Depends(get_db)):
    return _guard(
        service.create_server,
        db,
        user,
        body.name,
        body.plan,
        body.image,
        tls_passthrough=body.tls_passthrough,
        encrypted=body.encrypted,
    )


@router.post("/servers/{server_id}/actions/{action}", status_code=202)
def server_action(server_id: int, action: str, user: User = Depends(api_user), db: Session = Depends(get_db)):
    return _guard(service.server_action, db, user, server_id, action)


@router.post("/servers/{server_id}/resize", status_code=202)
def resize_server(server_id: int, body: ResizeBody, user: User = Depends(api_user), db: Session = Depends(get_db)):
    return _guard(service.resize_server, db, user, server_id, body.plan)


# ── account SSH keys (installed on servers you create; add before creating a server) ──
@router.get("/account/ssh-keys")
def list_ssh_keys(user: User = Depends(api_user), db: Session = Depends(get_db)):
    return service.list_ssh_keys(db, user)


@router.post("/account/ssh-keys", status_code=201)
def add_ssh_key(body: SshKeyBody, user: User = Depends(api_user), db: Session = Depends(get_db)):
    return _guard(service.add_ssh_key, db, user, body.name, body.public_key)


@router.delete("/account/ssh-keys/{key_id}")
def delete_ssh_key(key_id: int, user: User = Depends(api_user), db: Session = Depends(get_db)):
    return _guard(service.remove_ssh_key, db, user, key_id)


# ── backups (nightly PBS backups + on-demand; restore replaces the server's disk) ──
@router.get("/servers/{server_id}/backups")
def list_backups(server_id: int, user: User = Depends(api_user), db: Session = Depends(get_db)):
    return _guard(service.list_backups, db, user, server_id)


@router.put("/servers/{server_id}/backups")
def set_backups(server_id: int, body: BackupsBody, user: User = Depends(api_user), db: Session = Depends(get_db)):
    return _guard(service.set_backups, db, user, server_id, body.enabled)


@router.post("/servers/{server_id}/backups/actions/backup", status_code=202)
def backup_now(server_id: int, user: User = Depends(api_user), db: Session = Depends(get_db)):
    return _guard(service.backup_now, db, user, server_id)


@router.post("/servers/{server_id}/backups/actions/restore", status_code=202)
def restore_backup(server_id: int, body: RestoreBody, user: User = Depends(api_user), db: Session = Depends(get_db)):
    return _guard(service.restore_backup, db, user, server_id, body.backup_id)


# ── custom domains ──
@router.get("/servers/{server_id}/domains")
def list_domains(server_id: int, user: User = Depends(api_user), db: Session = Depends(get_db)):
    return _guard(service.list_domains, db, user, server_id)


@router.post("/servers/{server_id}/domains", status_code=201)
def add_domain(server_id: int, body: DomainBody, user: User = Depends(api_user), db: Session = Depends(get_db)):
    return _guard(service.add_domain, db, user, server_id, body.hostname)


@router.delete("/servers/{server_id}/domains/{hostname}")
def delete_domain(server_id: int, hostname: str, user: User = Depends(api_user), db: Session = Depends(get_db)):
    return _guard(service.remove_domain, db, user, server_id, hostname)


# ── snapshots ──
@router.get("/servers/{server_id}/snapshots")
def list_snapshots(server_id: int, user: User = Depends(api_user), db: Session = Depends(get_db)):
    return _guard(service.list_snapshots, db, user, server_id)


@router.post("/servers/{server_id}/snapshots", status_code=201)
def create_snapshot(server_id: int, body: SnapshotBody, user: User = Depends(api_user), db: Session = Depends(get_db)):
    return _guard(service.create_snapshot, db, user, server_id, body.name)


@router.delete("/servers/{server_id}/snapshots/{name}")
def delete_snapshot(server_id: int, name: str, user: User = Depends(api_user), db: Session = Depends(get_db)):
    return _guard(service.delete_snapshot, db, user, server_id, name)


@router.post("/servers/{server_id}/snapshots/{name}/actions/rollback", status_code=202)
def rollback_snapshot(server_id: int, name: str, user: User = Depends(api_user), db: Session = Depends(get_db)):
    return _guard(service.rollback_snapshot, db, user, server_id, name)


# ── per-server settings ──
@router.put("/servers/{server_id}/restart-schedule")
def set_restart_schedule(
    server_id: int, body: RestartScheduleBody, user: User = Depends(api_user), db: Session = Depends(get_db)
):
    return _guard(service.set_restart_schedule, db, user, server_id, body.enabled, body.at)


@router.put("/servers/{server_id}/share")
def set_share(server_id: int, body: ShareBody, user: User = Depends(api_user), db: Session = Depends(get_db)):
    return _guard(service.set_share, db, user, server_id, body.enabled, body.note)


# ── monitoring & metrics ──
@router.put("/servers/{server_id}/monitoring")
def set_monitoring(server_id: int, body: MonitoringBody, user: User = Depends(api_user), db: Session = Depends(get_db)):
    fn = service.enable_monitoring if body.enabled else service.disable_monitoring
    return _guard(fn, db, user, server_id)


@router.get("/servers/{server_id}/monitoring")
def get_monitoring(server_id: int, period: str = "24h", user: User = Depends(api_user), db: Session = Depends(get_db)):
    return _guard(service.monitoring_data, db, user, server_id, period)


@router.get("/servers/{server_id}/metrics")
def get_metrics(server_id: int, timeframe: str = "hour", user: User = Depends(api_user), db: Session = Depends(get_db)):
    return _guard(service.server_metrics, db, user, server_id, timeframe)


# ── account settings ──
@router.put("/account/private-network")
def set_private_network(body: PrivateNetworkBody, user: User = Depends(api_user), db: Session = Depends(get_db)):
    return _guard(service.set_private_network, db, user, body.enabled)
