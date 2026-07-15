"""IPAM for the tenant VLAN. The control plane is the only allocator; the same
allocation feeds cloud-init AND the Proxmox ipfilter anti-spoof rules, so a tenant
re-IPing inside their VM is dropped at the vNIC (a hard design requirement)."""

import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Instance, IpAddress


class IpPoolExhausted(Exception):
    pass


def _generate_mac() -> str:
    # Locally-administered unicast prefix; pinned in PVE net0 + macfilter.
    return "BC:24:11:" + ":".join(f"{secrets.randbits(8):02X}" for _ in range(3))


def allocate(db: Session, instance: Instance) -> IpAddress:
    """Allocate the lowest free address to an instance (idempotent: returns the
    existing allocation if this instance already holds one)."""
    existing = db.scalar(select(IpAddress).where(IpAddress.instance_id == instance.id))
    if existing is not None:
        return existing
    stmt = (
        select(IpAddress)
        .where(IpAddress.instance_id.is_(None), IpAddress.reserved.is_(False))
        .order_by(IpAddress.id)
        .limit(1)
    )
    if db.bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)
    ip = db.scalar(stmt)
    if ip is None:
        raise IpPoolExhausted("tenant IP pool exhausted")
    ip.instance_id = instance.id
    ip.mac = _generate_mac()
    db.flush()
    return ip


def release(db: Session, instance: Instance) -> None:
    ip = db.scalar(select(IpAddress).where(IpAddress.instance_id == instance.id))
    if ip is not None:
        ip.instance_id = None
        ip.mac = None
        db.flush()


def cloudinit_ipconfig(ip: IpAddress) -> str:
    return f"ip={ip.address}/{ip.prefix_len},gw={ip.gateway}"


def seed_pool(db: Session, network: str, gateway: str, first: int, last: int, prefix_len: int = 24) -> int:
    """Ops helper: seed e.g. 10.66.0.10..10.66.0.250 into the pool. Idempotent."""
    base = network.rsplit(".", 1)[0]
    existing = {a for (a,) in db.execute(select(IpAddress.address))}
    added = 0
    for host in range(first, last + 1):
        addr = f"{base}.{host}"
        if addr not in existing:
            db.add(IpAddress(address=addr, gateway=gateway, prefix_len=prefix_len))
            added += 1
    db.flush()
    return added
