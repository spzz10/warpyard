import secrets
from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """All deployment-specific values live here and are overridden via `.env`
    (see `.env.example` for a fully annotated copy). Defaults below are
    placeholders — a real deployment sets at minimum: BASE_DOMAIN, PUBLIC_URL,
    MCP_URL, the PROXMOX_* block, the tenant-network block, and the EDGE_* block.
    """

    DATABASE_URL: str = "sqlite:///./warpyard.db"

    # Signs session cookies AND password-reset tokens. Set it in prod; when unset, a
    # random per-process secret is used (fine for dev — restarts log everyone out and
    # void outstanding reset links).
    SESSION_SECRET: str = ""

    # Product
    BASE_DOMAIN: str = "example.com"  # tenant services live at <label>.BASE_DOMAIN
    PUBLIC_URL: str = "https://app.example.com"  # public dashboard URL (for invite links etc.)
    MCP_URL: str = "https://mcp.example.com"  # public MCP resource-server URL (AI clients add this)

    # Proxmox — privilege-split API tokens, all scoped to the tenant pool only.
    # power: VM.PowerMgmt | config: VM.Clone/VM.Config.*/VM.Allocate | console: VM.Console
    # See docs/PVE-SETUP.md for creating the user, roles, and tokens.
    PROXMOX_API_URL: str = "https://proxmox.example.com:8006"
    PROXMOX_NODE: str = "pve"
    PROXMOX_POOL: str = "tenants"
    PROXMOX_VERIFY_TLS: bool = False  # most homelab PVE hosts run a self-signed cert
    PROXMOX_TOKEN_POWER: str = ""  # "user@pve!tokenid=uuid"
    PROXMOX_TOKEN_CONFIG: str = ""
    PROXMOX_TOKEN_CONSOLE: str = ""
    PROXMOX_TOKEN_BACKUP: str = ""  # VM.Backup + Datastore.AllocateSpace/Audit (vzdump + restore)

    # Backups: nightly vzdump to a Proxmox Backup Server datastore (its size IS the
    # tenant quota; retention/GC/verify are PBS-side jobs the PVE token can't touch).
    # Restores go through the same backup token. Leave PBS_TOKEN empty to run
    # without off-host backups entirely.
    BACKUP_STORAGE: str = "warpyard-pbs"  # PVE storage entry on the node
    BACKUP_HOUR_UTC: int = 8  # schedule nightly backups before your PBS GC window
    # PBS API, used ONLY to forget a destroyed server's backup group (same auth-id that owns
    # the groups; PBS lets Datastore.Backup remove own snapshots). Empty = orphan groups
    # linger until manually forgotten (prune still caps them).
    PBS_API_URL: str = "https://pbs.example.com:8007"
    PBS_DATASTORE: str = "warpyard"
    PBS_VERIFY_TLS: bool = False
    PBS_TOKEN: str = ""  # "user@pbs!tokenid:uuid"

    # Tenant network: an isolated VLAN with no route to your LAN. Your router/firewall
    # should enforce: no traffic to RFC1918, no outbound :25, allow internet egress.
    # See docs/NETWORK.md for the reference layout.
    TENANT_BRIDGE: str = "vmbr0"
    TENANT_VLAN_TAG: int = 66
    TENANT_GATEWAY: str = "10.66.0.1"
    TENANT_NAMESERVERS: str = "1.1.1.1 8.8.8.8"  # public DNS — tenants can't reach LAN DNS by design
    # Per-VM firewall default-denies inbound; only these may reach a tenant (so tenants on
    # the shared VLAN can't reach each other at L2). Edge = ingress; control plane = readiness.
    EDGE_WG_IP: str = "10.10.66.2"
    CONTROL_PLANE_IP: str = ""  # this machine's IP as seen from the tenant VLAN
    TENANT_VMID_MIN: int = 90000  # tenant VMs live in a dedicated vmid range
    TENANT_VMID_MAX: int = 99999

    # Linked clones (ZFS copy-on-write) provision in seconds vs a full 10GB copy.
    # Trade-off: clones depend on the template disk, so templates must never be deleted.
    CLONE_FULL: bool = False
    # tenant disks are cloned onto this storage; point it at a ZFS-native-encrypted
    # dataset to offer at-rest encryption (requires a full clone — the encryption
    # boundary can't be crossed by a linked clone).
    TENANT_STORAGE: str = "local-zfs"

    # Worker
    WORKER_POLL_SECONDS: float = 2.0
    JOB_MAX_ATTEMPTS: int = 5

    # Edge (a small public VPS running Caddy + WireGuard). The edge agent pulls its
    # routing table from this control plane — this is just the shared-secret for that pull.
    EDGE_SYNC_TOKEN: str = ""
    EDGE_HOST: str = "edge.example.com"  # public name of the edge (SSH/TCP forwards land here)
    EDGE_IP: str = ""  # edge public IP — custom domains must resolve here (DNS pre-check)
    MAX_CUSTOM_DOMAINS: int = 5  # per instance
    SSH_FORWARD_BASE: int = 2200  # a VM's SSH port on the edge = SSH_FORWARD_BASE + instance.id
    GAME_FORWARD_BASE: int = 30000  # game port j on the edge = GAME_FORWARD_BASE + instance.id*4 + j (<=4/vm)
    SSH_LOGIN_USER: str = "root"  # cloud image default user the injected keys authorize
    # cloud-init vendor snippet (on the PVE node's snippet-enabled storage) that
    # self-installs Caddy on a tenant so it terminates its own HTTPS; injected on web
    # VMs so the edge stays ciphertext-only. See docs/NETWORK.md (end-to-end TLS).
    TLS_SNIPPET: str = "vendor=local:snippets/wy-tls.yml"

    # Transactional email (Resend). Empty token = email disabled (links still shown in the UI).
    WARPYARD_RESEND_TOKEN: str = ""
    MAIL_FROM: str = "Warpyard <invites@example.com>"  # must be on a Resend-verified domain

    # Optional uptime monitoring via PoppaPing (https://poppaping.com). Users paste
    # their own API key on the Account page; both features hide when unused.
    POPPAPING_BASE_URL: str = "https://poppaping.com"
    # Partner provisioning (one-click monitoring accounts). Empty = the
    # "set up automatically" button is hidden; users paste their own API key instead.
    WARPYARD_POPPAPING_PARTNER_SECRET: str = ""

    model_config = {"env_file": ".env", "extra": "ignore"}


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def session_secret() -> str:
    """The signing secret for sessions and reset tokens — SESSION_SECRET, or one
    random per-process value shared by every consumer (never a hardcoded constant)."""
    return get_settings().SESSION_SECRET or secrets.token_hex(32)
