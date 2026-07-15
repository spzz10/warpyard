"""MCP server (Streamable HTTP) that lets a user's AI manage their Warpyard servers.

It is an OAuth 2.1 *resource server*: it validates the Bearer tokens minted by the control
plane's authorization server (app.oauth) against the shared DB, and every tool acts strictly
as the token's owner. The actual work goes through app.service — the same domain logic the
REST API uses — so there is one source of truth for quotas and the state machine."""

from urllib.parse import urlparse

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl

from app import oauth, service
from app.config import get_settings
from app.database import SessionLocal
from app.models import User

_settings = get_settings()
_mcp_host = urlparse(_settings.MCP_URL).netloc  # e.g. mcp.example.com
# The request Host header the edge forwards is the public MCP host; also allow local calls.
_ALLOWED_HOSTS = [_mcp_host, "localhost", "127.0.0.1", "localhost:8000", "127.0.0.1:8000"]
_ALLOWED_ORIGINS = [_settings.MCP_URL, f"https://{_mcp_host}"]

INSTRUCTIONS = """Manage the user's Warpyard cloud servers (a self-hosted VM host).
Use list_plans / list_images to see what's available, list_servers to see what they have,
then create_server / server_action / resize_server to make changes. Creating, rebuilding,
resizing and destroying are asynchronous — the call returns immediately and the server's
status moves through provisioning/booting to running; poll get_server to watch it settle.
Destroy and rebuild are irreversible (they wipe the disk); confirm with the user first.
Nightly off-host backups are an opt-in per server (set_backups); restore_backup replaces
the server's current disk with a restore point and is destructive — confirm first.
Snapshots are instant local restore points (create/rollback/delete); rollback is destructive
too. get_metrics shows CPU/memory/network history; enable_monitoring adds uptime checks with
email alerts (via PoppaPing). Per-server settings: set_restart_schedule (nightly reboot,
player-aware on game servers) and set_share (list it on the members-only board)."""


class WarpyardTokenVerifier(TokenVerifier):
    """Validate an opaque wyt_ access token by looking it up in the shared OAuth token table."""

    async def verify_token(self, token: str) -> AccessToken | None:
        with SessionLocal() as db:
            user = oauth.user_for_token(db, token)
            if not user:
                return None
            return AccessToken(
                token=token,
                client_id=f"user:{user.id}",
                scopes=[oauth.SCOPE],
                subject=str(user.id),
            )


mcp = FastMCP(
    name="Warpyard",
    instructions=INSTRUCTIONS,
    stateless_http=True,  # each request is self-contained; simplest to run behind the edge
    streamable_http_path="/mcp",
    token_verifier=WarpyardTokenVerifier(),
    auth=AuthSettings(
        issuer_url=AnyHttpUrl(_settings.PUBLIC_URL),
        resource_server_url=AnyHttpUrl(_settings.MCP_URL),
        required_scopes=[oauth.SCOPE],
    ),
    transport_security=TransportSecuritySettings(
        allowed_hosts=_ALLOWED_HOSTS,
        allowed_origins=_ALLOWED_ORIGINS,
    ),
)


def _user(db) -> User:
    tok = get_access_token()
    user = db.get(User, int(tok.subject)) if tok else None
    if not user or user.status != "active":
        raise ValueError("Not authenticated.")
    return user


def _run(fn, *a, **kw):
    """Run a service call as the authenticated user, turning rule violations into plain text
    the model can relay to the user rather than an opaque protocol error."""
    with SessionLocal() as db:
        user = _user(db)
        try:
            return fn(db, user, *a, **kw)
        except service.ServiceError as e:
            return {"error": e.message}


@mcp.tool()
def whoami() -> dict:
    """Show the signed-in account: email, how many servers are in use, and the server limit."""
    with SessionLocal() as db:
        return service.account_info(db, _user(db))


@mcp.tool()
def list_plans() -> list[dict]:
    """List the server sizes (plans) that can be created, with their specs."""
    with SessionLocal() as db:
        _user(db)
        return service.list_plans(db)


@mcp.tool()
def list_images() -> list[dict]:
    """List the operating-system images a server can be created from."""
    with SessionLocal() as db:
        _user(db)
        return service.list_images(db)


@mcp.tool()
def list_servers() -> list[dict]:
    """List the user's current servers with status, specs, public URL and SSH command."""
    with SessionLocal() as db:
        return service.list_servers(db, _user(db))


@mcp.tool()
def get_server(server_id: int) -> dict:
    """Get one server by id, including its status, hostname, SSH command and private IP."""
    return _run(service.get_server, server_id)


@mcp.tool()
def create_server(
    name: str, plan: str, image: str, tls_passthrough: bool | None = None, encrypted: bool = False
) -> dict:
    """Create a new server. `name` is a lowercase DNS label (becomes the server’s public hostname, <name>.<base domain>);
    `plan` and `image` are slugs from list_plans / list_images. SSH keys on the account are
    installed at creation — to get shell access for yourself, call add_ssh_key FIRST, then
    create the server.

    `tls_passthrough` (default: on for os/app images, off for games): the VM terminates its own
    HTTPS so the edge never sees plaintext; each uses one Let's Encrypt cert (50 new/week limit
    across the platform). `encrypted` (default off): store the disk encrypted at rest — adds
    ~1 min to creation (full clone). Returns the new server."""
    return _run(service.create_server, name, plan, image, tls_passthrough=tls_passthrough, encrypted=encrypted)


@mcp.tool()
def list_ssh_keys() -> list[dict]:
    """List the SSH public keys on the account. These are installed on every server created
    afterwards, and re-installed when a server is rebuilt."""
    with SessionLocal() as db:
        return service.list_ssh_keys(db, _user(db))


@mcp.tool()
def add_ssh_key(name: str, public_key: str) -> dict:
    """Add an SSH public key to the account so you (or a user) can SSH into servers. `public_key`
    is a full key line like 'ssh-ed25519 AAAA… label'. It takes effect on servers created after
    this call (or on a server.action rebuild). To give yourself access: add your key, then
    create_server, then SSH in with the command from get_server."""
    return _run(service.add_ssh_key, name, public_key)


@mcp.tool()
def remove_ssh_key(key_id: int) -> dict:
    """Remove an SSH key from the account by its id (see list_ssh_keys). Existing servers keep it
    until rebuilt."""
    return _run(service.remove_ssh_key, key_id)


@mcp.tool()
def add_domain(server_id: int, hostname: str) -> dict:
    """Point a custom domain at a server (in addition to its free <name>.<base domain> address). If the
    domain's DNS already points at Warpyard's edge it goes active immediately; otherwise it's
    returned as 'pending' with the exact DNS record the user must create — HTTPS is issued
    automatically once that record resolves. Names under the platform base domain are not allowed."""
    return _run(service.add_domain, server_id, hostname)


@mcp.tool()
def list_domains(server_id: int) -> list[dict]:
    """List a server's custom domains and their status (active / pending, with the DNS record
    still needed for pending ones)."""
    return _run(service.list_domains, server_id)


@mcp.tool()
def remove_domain(server_id: int, hostname: str) -> dict:
    """Remove a custom domain from a server. The free platform address stays."""
    return _run(service.remove_domain, server_id, hostname)


@mcp.tool()
def list_backups(server_id: int) -> list[dict]:
    """List a server's off-host backups (restore points), newest first. Each has an id (pass
    to restore_backup), created_at, size and verification state."""
    return _run(service.list_backups, server_id)


@mcp.tool()
def set_backups(server_id: int, enabled: bool) -> dict:
    """Turn nightly off-host backups on or off for a server. When on, a backup is taken every
    night (kept 7 daily + 4 weekly) and the server keeps running during it."""
    return _run(service.set_backups, server_id, enabled)


@mcp.tool()
def backup_now(server_id: int) -> dict:
    """Take an off-host backup of a server right now (asynchronous; the server keeps running).
    Useful before a risky change. See list_backups for the result."""
    return _run(service.backup_now, server_id)


@mcp.tool()
def restore_backup(server_id: int, backup_id: str) -> dict:
    """Restore a server from a backup (id from list_backups). DESTRUCTIVE: the server's current
    disk is replaced with the restore point and it reboots — changes made since that backup are
    lost. Confirm with the user first. Asynchronous; poll get_server until it's running again."""
    return _run(service.restore_backup, server_id, backup_id)


@mcp.tool()
def list_snapshots(server_id: int) -> list[dict]:
    """List a server's snapshots (instant local restore points on the host)."""
    return _run(service.list_snapshots, server_id)


@mcp.tool()
def create_snapshot(server_id: int, name: str = "") -> dict:
    """Take an instant snapshot of a server (synchronous). `name` is an optional label.
    Snapshots live on the same host as the server — for an off-host copy use backup_now."""
    return _run(service.create_snapshot, server_id, name)


@mcp.tool()
def delete_snapshot(server_id: int, name: str) -> dict:
    """Delete a snapshot by name (see list_snapshots)."""
    return _run(service.delete_snapshot, server_id, name)


@mcp.tool()
def rollback_snapshot(server_id: int, name: str) -> dict:
    """Roll a server back to a snapshot. DESTRUCTIVE: changes made since that snapshot are
    lost and the server reboots — confirm with the user first. Asynchronous; poll get_server."""
    return _run(service.rollback_snapshot, server_id, name)


@mcp.tool()
def set_restart_schedule(server_id: int, enabled: bool, at: str | None = None) -> dict:
    """Turn a nightly automatic reboot on or off. `at` is "HH:MM" 24-hour UTC (defaults to
    09:00 when first enabled). Game servers restart player-aware: an occupied server waits
    until it's empty."""
    return _run(service.set_restart_schedule, server_id, enabled, at)


@mcp.tool()
def set_share(server_id: int, enabled: bool, note: str | None = None) -> dict:
    """Show or hide the server on the members-only share board (visible to other Warpyard
    members while the server is running). `note` is an optional short description."""
    return _run(service.set_share, server_id, enabled, note)


@mcp.tool()
def enable_monitoring(server_id: int) -> dict:
    """Turn on uptime monitoring with email alerts for a server (checks the site, game port
    or SSH — whichever fits the server). Runs on PoppaPing under the user's own account; one
    is provisioned for their email automatically if they don't have one connected."""
    return _run(service.enable_monitoring, server_id)


@mcp.tool()
def disable_monitoring(server_id: int) -> dict:
    """Turn off uptime monitoring for a server (removes its check and alerts)."""
    return _run(service.disable_monitoring, server_id)


@mcp.tool()
def get_monitoring(server_id: int, period: str = "24h") -> dict:
    """Uptime % , up/down check counts and response-time history for a monitored server.
    `period` is one of 24h, 7d, 30d, 90d."""
    return _run(service.monitoring_data, server_id, period)


@mcp.tool()
def get_metrics(server_id: int, timeframe: str = "hour") -> dict:
    """CPU %, memory and network history for a server, straight from the host.
    `timeframe` is one of hour, day, week. Points may contain nulls where the server was off."""
    return _run(service.server_metrics, server_id, timeframe)


@mcp.tool()
def set_private_network(enabled: bool) -> dict:
    """Let the user's OWN servers reach each other over the private network (other accounts
    can never reach them either way). Applies to all their servers immediately."""
    return _run(service.set_private_network, enabled)


@mcp.tool()
def server_action(server_id: int, action: str) -> dict:
    """Run a lifecycle action on a server. `action` is one of: start, stop, reboot, rebuild,
    destroy. rebuild and destroy wipe the disk and are irreversible — confirm with the user."""
    return _run(service.server_action, server_id, action)


@mcp.tool()
def resize_server(server_id: int, plan: str) -> dict:
    """Move a server to a different plan (slug from list_plans). Disk can only grow, and the
    server reboots as part of the resize."""
    return _run(service.resize_server, server_id, plan)


# ASGI app for the Streamable HTTP transport, mounted by app.main. The parent app must run
# `mcp.session_manager.run()` in its lifespan (main.py does this).
mcp_app = mcp.streamable_http_app()
