from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.config import get_settings
from app.database import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str | None] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default="active")  # active | suspended
    # opt-in: let THIS user's own servers reach each other on the tenant VLAN. Cross-account
    # is never reachable regardless. Off by default.
    private_network: Mapped[bool] = mapped_column(Boolean, default=False)
    # New servers start listed on the share board (useful for bot/concierge accounts
    # whose servers are community-facing by nature). Off for normal accounts.
    share_by_default: Mapped[bool] = mapped_column(Boolean, default=False)
    # every member can invite a couple of friends (admins are unlimited)
    max_invites: Mapped[int] = mapped_column(Integer, default=2)
    # PoppaPing API key (monitors:write) for one-click uptime monitoring of servers
    poppaping_api_key: Mapped[str | None] = mapped_column(String(100))
    stripe_customer_id: Mapped[str | None] = mapped_column(String(64))
    # Per-user quotas — the platform must enforce these before Proxmox does. On a typical
    # single-host deployment CPU/RAM are abundant; disk is usually the real cap.
    max_instances: Mapped[int] = mapped_column(Integer, default=15)
    max_vcpus: Mapped[int] = mapped_column(Integer, default=32)
    max_disk_gb: Mapped[int] = mapped_column(Integer, default=300)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    ssh_keys: Mapped[list["SshKey"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    instances: Mapped[list["Instance"]] = relationship(back_populates="user")


class OAuthClient(Base):
    """A dynamically-registered OAuth client (an MCP client like Claude/Cursor). Public
    clients using PKCE — no client secret."""

    __tablename__ = "oauth_clients"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[str] = mapped_column(String(48), unique=True, index=True)
    client_name: Mapped[str | None] = mapped_column(String(128))
    redirect_uris: Mapped[str] = mapped_column(Text)  # newline-separated
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OAuthCode(Base):
    """Short-lived authorization code (PKCE). Exchanged once for a token."""

    __tablename__ = "oauth_codes"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    client_id: Mapped[str] = mapped_column(String(48), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    redirect_uri: Mapped[str] = mapped_column(Text)
    code_challenge: Mapped[str] = mapped_column(String(128))  # S256
    scope: Mapped[str] = mapped_column(String(128), default="")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used: Mapped[bool] = mapped_column(Boolean, default=False)


class OAuthToken(Base):
    """Bearer access token the MCP server validates. Opaque; only stored here."""

    __tablename__ = "oauth_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    client_id: Mapped[str] = mapped_column(String(48))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    scope: Mapped[str] = mapped_column(String(128), default="")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped["User"] = relationship()


class ApiKey(Base):
    """A user's API key for the REST API (and, later, MCP). Only the hash is stored; the
    full key `wy_<secret>` is shown once at creation. `prefix` is for display/identification."""

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    prefix: Mapped[str] = mapped_column(String(16), index=True)  # e.g. "wy_a1b2c3d4"
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # sha256 of full key
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped["User"] = relationship()


class InviteRequest(Base):
    """Public 'request an invite' form on the marketing site — admins approve or dismiss
    from the /invites page (approve mints an email-pinned invite and sends it)."""

    __tablename__ = "invite_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), index=True)
    message: Mapped[str | None] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(12), default="pending")  # pending | invited | dismissed
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Invite(Base):
    """Invite-only signup: an admin mints a token; whoever redeems it becomes a
    member. Single-use, optionally email-pinned."""

    __tablename__ = "invites"

    id: Mapped[int] = mapped_column(primary_key=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(255))  # optional: pin to one address
    note: Mapped[str | None] = mapped_column(String(120))  # e.g. "for Sam"
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    redeemed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    redeemed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SshKey(Base):
    __tablename__ = "ssh_keys"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    public_key: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped["User"] = relationship(back_populates="ssh_keys")


class Plan(Base):
    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(32), unique=True)  # e.g. wy-1-1
    name: Mapped[str] = mapped_column(String(64))
    vcpus: Mapped[int] = mapped_column(Integer)
    memory_mb: Mapped[int] = mapped_column(Integer)
    disk_gb: Mapped[int] = mapped_column(Integer)
    transfer_gb: Mapped[int] = mapped_column(Integer)  # monthly; size against your upstream bandwidth
    net_mbps: Mapped[int] = mapped_column(Integer, default=100)  # Proxmox NIC rate limit
    price_cents: Mapped[int] = mapped_column(Integer)  # flat monthly, prepaid
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Image(Base):
    __tablename__ = "images"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True)  # e.g. ubuntu-24.04
    name: Mapped[str] = mapped_column(String(128))
    distro: Mapped[str] = mapped_column(String(32))
    version: Mapped[str] = mapped_column(String(32))
    template_vmid: Mapped[int] = mapped_column(Integer)  # cloud-init golden template on the node
    min_disk_gb: Mapped[int] = mapped_column(Integer, default=10)
    # Image lifecycle: active | deprecated (existing instances keep it,
    # new creates rejected) | retired
    status: Mapped[str] = mapped_column(String(20), default="active")
    # os | app | game — groups the create picker and drives what usage info we surface
    category: Mapped[str] = mapped_column(String(16), default="os")
    # LinuxGSM server code for game images (e.g. mcserver) — the shared LGSM base template
    # installs this game on first boot. Empty for non-game images.
    lgsm_game: Mapped[str] = mapped_column(String(32), default="")
    # ports the edge should forward (raw L4), CSV of proto:port, e.g. "tcp:25565" or "udp:2456,udp:2457"
    ports: Mapped[str] = mapped_column(String(128), default="")
    # recommended plan slug — the create page pre-selects this size when the image is chosen
    # (e.g. CS2 needs a big-disk plan). Empty = leave the cheapest default.
    default_plan: Mapped[str] = mapped_column(String(32), default="")
    # one-line tagline for the create picker (e.g. "Blog & site builder")
    blurb: Mapped[str | None] = mapped_column(String(120))
    # human usage/connect instructions shown on the server page ({endpoint} is substituted)
    guidance: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Instance(Base):
    __tablename__ = "instances"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id"))
    image_id: Mapped[int] = mapped_column(ForeignKey("images.id"))
    label: Mapped[str] = mapped_column(String(63))  # DNS-safe; becomes <label>.<BASE_DOMAIN>
    hostname: Mapped[str | None] = mapped_column(String(255), unique=True)  # FQDN, set at provision
    status: Mapped[str] = mapped_column(String(20), default="provisioning", index=True)
    vmid: Mapped[int | None] = mapped_column(Integer, unique=True)  # assigned by create job
    node: Mapped[str] = mapped_column(String(64), default=lambda: get_settings().PROXMOX_NODE)
    # True = the VM terminates its own HTTPS (Caddy via cloud-init) and the edge SNI-passes
    # :443 straight through, so the edge never sees plaintext. False (legacy) = edge terminates.
    tls_passthrough: Mapped[bool] = mapped_column(Boolean, default=False)
    # True = disk is on the ZFS-encrypted pool (aes-256-gcm). Full-clone only (slower create).
    encrypted: Mapped[bool] = mapped_column(Boolean, default=False)
    # UNUSED — always NULL. The console auto-logs-in via serial getty (see docs/PVE-SETUP.md)
    # and SSH is key-based, so no root password exists anywhere. Column kept for schema
    # parity with the migration chain; a drop migration is tracked upstream.
    root_password: Mapped[str | None] = mapped_column(String(64))
    # Backups add-on: nightly vzdump to PBS + on-demand. Off by default.
    backups_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    last_backup_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Share board: opt-in listing on the members-only board (shown only while running)
    shared: Mapped[bool] = mapped_column(Boolean, default=False)
    shared_note: Mapped[str | None] = mapped_column(String(140))
    # Scheduled nightly restart (game servers): HH:MM UTC; skipped while players are online
    restart_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    restart_at: Mapped[str | None] = mapped_column(String(5))
    last_auto_restart_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # PoppaPing monitor watching this server (one-click from the server page)
    poppaping_monitor_id: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)  # populated when status == error
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    destroy_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # non-payment grace
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    user: Mapped["User"] = relationship(back_populates="instances")
    plan: Mapped["Plan"] = relationship()
    image: Mapped["Image"] = relationship()
    ip: Mapped["IpAddress | None"] = relationship(back_populates="instance", uselist=False)
    http_routes: Mapped[list["HttpRoute"]] = relationship(back_populates="instance", cascade="all, delete-orphan")
    edge_mappings: Mapped[list["EdgeMapping"]] = relationship(back_populates="instance", cascade="all, delete-orphan")
    board_comments: Mapped[list["BoardComment"]] = relationship(
        cascade="all, delete-orphan", order_by="BoardComment.created_at"
    )


class BoardComment(Base):
    """Comments under a board listing. Members-only like the board itself. Kept when a
    server is unshared (they come back if it's re-shared); destroyed servers can never
    return to the board, so their comments simply stop being rendered."""

    __tablename__ = "board_comments"

    id: Mapped[int] = mapped_column(primary_key=True)
    instance_id: Mapped[int] = mapped_column(ForeignKey("instances.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    body: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped["User"] = relationship()


class IpAddress(Base):
    """Tenant-VLAN IPAM pool. Seeded by ops script; the control plane is the only
    allocator. The same row that feeds cloud-init also generates the Proxmox
    ipfilter anti-spoof entries (one source of truth, never hand-edited)."""

    __tablename__ = "ip_addresses"

    id: Mapped[int] = mapped_column(primary_key=True)
    address: Mapped[str] = mapped_column(String(45), unique=True)
    prefix_len: Mapped[int] = mapped_column(Integer, default=24)
    gateway: Mapped[str] = mapped_column(String(45))
    instance_id: Mapped[int | None] = mapped_column(ForeignKey("instances.id"), unique=True, index=True)
    reserved: Mapped[bool] = mapped_column(Boolean, default=False)  # infra addresses, never allocated
    mac: Mapped[str | None] = mapped_column(String(17))  # pinned at allocation for ipfilter

    instance: Mapped["Instance | None"] = relationship(back_populates="ip")


class HttpRoute(Base):
    """Netlify-style ingress: <hostname> is served by the edge (Caddy on a small
    public VPS) which proxies over WireGuard to the instance's tenant-VLAN IP:port.
    The edge agent pulls this table; TLS is a wildcard cert at the edge."""

    __tablename__ = "http_routes"

    id: Mapped[int] = mapped_column(primary_key=True)
    instance_id: Mapped[int] = mapped_column(ForeignKey("instances.id"), index=True)
    hostname: Mapped[str] = mapped_column(String(255), unique=True)  # <label>.<base domain> or custom domain
    target_port: Mapped[int] = mapped_column(Integer, default=80)
    kind: Mapped[str] = mapped_column(String(10), default="system")  # system (<label>.<base domain>) | custom
    # active = served by the edge; pending = custom domain whose DNS doesn't point at us yet
    status: Mapped[str] = mapped_column(String(10), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    instance: Mapped["Instance"] = relationship(back_populates="http_routes")


class EdgeMapping(Base):
    """Raw TCP/UDP ingress (SSH, game servers, ...): public edge-IP:port -> tenant IP:port."""

    __tablename__ = "edge_mappings"
    __table_args__ = (UniqueConstraint("protocol", "public_port"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    instance_id: Mapped[int] = mapped_column(ForeignKey("instances.id"), index=True)
    protocol: Mapped[str] = mapped_column(String(3))  # tcp | udp
    public_port: Mapped[int] = mapped_column(Integer)
    target_port: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    instance: Mapped["Instance"] = relationship(back_populates="edge_mappings")


class Job(Base):
    """Postgres-backed work queue. Enqueue + instance state transition happen in one
    transaction; workers claim with FOR UPDATE SKIP LOCKED. See app/jobs/queue.py."""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str] = mapped_column(String(64), index=True)  # e.g. instance.create
    instance_id: Mapped[int | None] = mapped_column(ForeignKey("instances.id"), index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    # queued | running | succeeded | failed (retryable, backoff pending) | dead
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    locked_by: Mapped[str | None] = mapped_column(String(64))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Event(Base):
    """Append-only audit log — every lifecycle action, reconciler finding, and
    admin intervention lands here. Never deleted, even when the instance is."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    instance_id: Mapped[int | None] = mapped_column(Integer, index=True)  # no FK: outlives the instance row
    action: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20))  # started | succeeded | failed | flagged
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UsageSample(Base):
    """Per-instance transfer accounting (bandwidth is the scarce resource on a home uplink).
    Sampled from Proxmox netin/netout counters by the reconciler loop."""

    __tablename__ = "usage_samples"

    id: Mapped[int] = mapped_column(primary_key=True)
    instance_id: Mapped[int] = mapped_column(Integer, index=True)
    netin_bytes: Mapped[float] = mapped_column(Float)
    netout_bytes: Mapped[float] = mapped_column(Float)
    sampled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
