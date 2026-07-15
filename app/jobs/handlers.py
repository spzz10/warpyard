"""Job handlers — one per verb. Every handler is idempotent: each step checks
current Proxmox state before acting, so a retried job resumes instead of
double-applying. Handlers receive a fresh Session and commit their own outcome."""

import socket
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import ipam, service, states
from app.config import get_settings
from app.models import EdgeMapping, Event, HttpRoute, Image, Instance, Job, Plan
from app.proxmox import ProxmoxClient, ProxmoxError

READY_POLL_SECONDS = 5  # gap between SSH-port checks while a VM boots
READY_MAX_ATTEMPTS = 40  # ~3.5 min cap, then mark running anyway (VM is up, just slow)


def _in(seconds: int) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds)


def _event(db: Session, instance: Instance, action: str, status: str, **detail) -> None:
    db.add(Event(user_id=instance.user_id, instance_id=instance.id, action=action, status=status, detail=detail))


def _port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _ssh_keys_blob(instance: Instance, fallback_key: str = "") -> str:
    """cloud-init ssh_keys for a tenant: the owner's account keys, or a one-off key passed on
    create when they have none. Only the owner's keys go on the VM — the control plane keeps
    no standing root access to tenants."""
    keys = [k.public_key for k in instance.user.ssh_keys]
    if not keys and fallback_key:
        keys.append(fallback_key)
    return "\n".join(keys)


def _owner_peer_ips(instance: Instance, exclude_id: int | None = None) -> list[str]:
    """IPs of the owner's OTHER live servers — only if the owner enabled private networking.
    Cross-account servers are never included, so accounts stay isolated regardless."""
    if not instance.user.private_network:
        return []
    return [
        other.ip.address
        for other in instance.user.instances
        if other.id != (exclude_id or instance.id)
        and other.ip is not None
        and other.status not in states.TERMINAL_STATES
    ]


def sync_owner_firewalls(db: Session, user, exclude_vmid: int | None = None) -> None:
    """Re-push the same-owner peer allow-list to all of a user's live VMs. Call after a
    server is created/destroyed or the user toggles private networking. Best-effort."""
    px = ProxmoxClient("config")
    live = [i for i in user.instances if i.vmid and i.vmid != exclude_vmid and i.status not in states.TERMINAL_STATES]
    ips = [i.ip.address for i in live if i.ip] if user.private_network else []
    import contextlib

    for inst in live:
        peers = [a for a in ips if a != (inst.ip.address if inst.ip else None)]
        with contextlib.suppress(ProxmoxError):
            px.set_peers(inst.vmid, peers)


def _wait_task(px: ProxmoxClient, upid: str, timeout: int = 300) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = px.task_status(upid)
        if st.get("status") == "stopped":
            if st.get("exitstatus") != "OK":
                raise ProxmoxError(f"task {upid} failed: {st.get('exitstatus')}")
            return
        time.sleep(2)
    raise ProxmoxError(f"task {upid} timed out after {timeout}s")  # -> provision-timeout path


def _pick_vmid(px: ProxmoxClient, db: Session) -> int:
    s = get_settings()
    used = px.pool_vmids() | {v for (v,) in db.execute(select(Instance.vmid).where(Instance.vmid.isnot(None)))}
    for vmid in range(s.TENANT_VMID_MIN, s.TENANT_VMID_MAX + 1):
        if vmid not in used:
            return vmid
    raise RuntimeError("tenant vmid range exhausted")


def instance_create(db: Session, job: Job) -> None:
    s = get_settings()
    instance = db.get(Instance, job.instance_id)
    plan = db.get(Plan, instance.plan_id)
    image = db.get(Image, instance.image_id)
    px_cfg = ProxmoxClient("config")
    px_pow = ProxmoxClient("power")

    # 1. vmid + clone (skip if already cloned on a previous attempt)
    if instance.vmid is None:
        instance.vmid = _pick_vmid(px_cfg, db)
        db.commit()
    try:
        px_cfg.vm_status(instance.vmid)
        cloned = True
    except ProxmoxError:
        cloned = False
    hostname = f"{instance.label}.{s.BASE_DOMAIN}"
    if not cloned:
        # linked clone (ZFS CoW) is near-instant; full copy is the slow path
        # encrypted servers full-clone onto the encrypted-at-rest storage (a linked clone can't
        # cross the encryption boundary); unencrypted use the fast linked clone on the default pool
        upid = px_cfg.clone(
            image.template_vmid,
            instance.vmid,
            name=instance.label,
            full=True if instance.encrypted else s.CLONE_FULL,
            storage=s.TENANT_STORAGE if instance.encrypted else None,
        )
        _wait_task(px_cfg, upid, timeout=600)

    # 2. IP + cloud-init + anti-spoof + resources (all idempotent re-applies).
    # No root password: SSH is key-based and the browser console auto-logins root (it's
    # already ownership-gated). Cloud images ship root locked, so a console password was
    # both broken and pointless.
    ip = ipam.allocate(db, instance)
    ssh_keys = _ssh_keys_blob(instance, job.payload.get("ssh_key", ""))
    px_cfg.set_cloudinit(
        instance.vmid,
        hostname=hostname,
        ssh_keys=ssh_keys,
        ipconfig0=ipam.cloudinit_ipconfig(ip),
        cicustom=(get_settings().TLS_SNIPPET if instance.tls_passthrough else None),
    )
    px_cfg.set_nic(instance.vmid, mac=ip.mac, rate_mbps=plan.net_mbps)
    px_cfg.set_resources(instance.vmid, vcpus=plan.vcpus, memory_mb=plan.memory_mb)
    if plan.disk_gb > image.min_disk_gb:
        px_cfg.resize_disk(instance.vmid, "scsi0", plan.disk_gb)
    px_cfg.apply_ipfilter(instance.vmid, ip=ip.address, mac=ip.mac, peer_ips=_owner_peer_ips(instance))
    instance.hostname = hostname
    db.commit()

    # 3a. default web route — every server is reachable at <label>.<base domain>:80 out of
    # the box (the edge picks it up once the IP is assigned). Users can add more later.
    if not any(r.hostname == hostname for r in instance.http_routes):
        db.add(HttpRoute(instance_id=instance.id, hostname=hostname, target_port=80))

    # 3b. SSH forward — the VM has no public IP (isolated VLAN), so publish an SSH port
    # on the edge: ssh -p <2200+id> user@edge. Deterministic port keeps it stable.
    if not any(m.protocol == "tcp" and m.target_port == 22 for m in instance.edge_mappings):
        db.add(
            EdgeMapping(
                instance_id=instance.id,
                protocol="tcp",
                public_port=s.SSH_FORWARD_BASE + instance.id,
                target_port=22,
            )
        )

    # 3c. raw L4 forwards declared by the image (game/app ports, e.g. Minecraft tcp:25565) →
    # published on the edge next to the SSH forward. Deterministic public ports keep them stable.
    for idx, (proto, port) in enumerate(service.parse_ports(image.ports)):
        if not any(m.protocol == proto and m.target_port == port for m in instance.edge_mappings):
            db.add(
                EdgeMapping(
                    instance_id=instance.id,
                    protocol=proto,
                    public_port=s.GAME_FORWARD_BASE + instance.id * 4 + idx,
                    target_port=port,
                )
            )

    # 4. boot, then hand off to the readiness poller — the instance stays "booting" (so the
    # console is gated and the user isn't shown boot spam) until the OS answers on SSH.
    if px_pow.vm_status(instance.vmid).get("status") != "running":
        px_pow.start(instance.vmid)
    states.transition(instance, states.BOOTING)
    db.add(Job(type="instance.await_ready", instance_id=instance.id, run_after=_in(6)))
    _event(db, instance, "instance.create", "succeeded", vmid=instance.vmid, ip=ip.address, hostname=hostname)
    db.commit()
    # let the owner's other servers learn about this new peer (if private networking is on)
    sync_owner_firewalls(db, instance.user, exclude_vmid=instance.vmid)


def instance_await_ready(db: Session, job: Job) -> None:
    """Non-blocking readiness poll: probe the VM's SSH port; flip booting->running when it
    answers. If not yet up, re-schedule itself with a delay (doesn't hold the worker) up to
    a cap, then mark running regardless so it never gets stuck."""
    instance = db.get(Instance, job.instance_id)
    if instance is None or instance.status != states.BOOTING:
        return  # already moved on (stopped/destroyed/etc.)
    if instance.ip and _port_open(instance.ip.address, 22):
        states.transition(instance, states.RUNNING)
        _event(db, instance, "instance.await_ready", "succeeded", ready=True)
        db.commit()
        return
    attempt = int(job.payload.get("attempt", 0)) + 1
    if attempt >= READY_MAX_ATTEMPTS:
        states.transition(instance, states.RUNNING)  # give up waiting; the VM is booted, just quiet
        _event(db, instance, "instance.await_ready", "succeeded", ready=False, note="ssh probe timed out")
        db.commit()
        return
    # re-queue self (direct add bypasses the one-active-job guard, which is for user actions)
    db.add(
        Job(
            type="instance.await_ready",
            instance_id=instance.id,
            payload={"attempt": attempt},
            run_after=_in(READY_POLL_SECONDS),
        )
    )
    db.commit()


def instance_resize(db: Session, job: Job) -> None:
    """Resize to a bigger plan: stop, apply cores/mem, grow the disk (grow-only), start.
    The guest's cloud-init growpart/resizefs extends the filesystem on the next boot.
    Idempotent: each step checks live state so a retry resumes."""
    instance = db.get(Instance, job.instance_id)
    new_plan = db.get(Plan, int(job.payload["plan_id"]))
    old_disk_gb = instance.plan.disk_gb
    if instance.status != states.RESIZING:
        states.transition(instance, states.RESIZING)
        db.commit()

    px_cfg = ProxmoxClient("config")
    px_pow = ProxmoxClient("power")
    # power off for a clean resize (matches how Linode/DO require a reboot to resize)
    if px_pow.vm_status(instance.vmid).get("status") == "running":
        stop_upid = px_pow.stop(instance.vmid)
        _wait_task(px_pow, stop_upid, timeout=90)

    px_cfg.set_resources(instance.vmid, vcpus=new_plan.vcpus, memory_mb=new_plan.memory_mb)
    if instance.ip:  # keep NIC rate limit in step with the new plan
        px_cfg.set_nic(instance.vmid, mac=instance.ip.mac, rate_mbps=new_plan.net_mbps)
    if new_plan.disk_gb > old_disk_gb:
        px_cfg.resize_disk(instance.vmid, "scsi0", new_plan.disk_gb)
    instance.plan_id = new_plan.id
    db.commit()

    px_pow.start(instance.vmid)
    states.transition(instance, states.BOOTING)  # boots + grows the fs, then readiness -> running
    db.add(Job(type="instance.await_ready", instance_id=instance.id, run_after=_in(6)))
    _event(db, instance, "instance.resize", "succeeded", plan=new_plan.slug, disk_gb=new_plan.disk_gb)
    db.commit()


def instance_rebuild(db: Session, job: Job) -> None:
    """Wipe and reinstall from the template, keeping the same name / IP / URL / SSH port.
    Destroys the disk and re-clones fresh, re-injecting the user's keys. Idempotent."""
    s = get_settings()
    instance = db.get(Instance, job.instance_id)
    plan, image = instance.plan, instance.image
    px_cfg = ProxmoxClient("config")
    px_pow = ProxmoxClient("power")
    if instance.status != states.REBUILDING:
        states.transition(instance, states.REBUILDING)
        db.commit()

    # 1. destroy the current VM (keep the DB row, IP, routes, edge mappings, hostname)
    reclone = True
    if instance.vmid is not None:
        try:
            if px_pow.vm_status(instance.vmid).get("status") == "running":
                stop_upid = px_pow.stop(instance.vmid)
                _wait_task(px_pow, stop_upid, timeout=90)
            # a fresh clone has no snapshots; if the disk is already the fresh clone from a
            # prior attempt we still re-provision below, so just delete + re-clone.
            upid = px_cfg.delete(instance.vmid)
            _wait_task(px_cfg, upid)
        except ProxmoxError as e:
            if "does not exist" not in str(e):
                raise
            reclone = True

    # 2. fresh clone to the same vmid
    if reclone:
        # encrypted servers full-clone onto the encrypted-at-rest storage (a linked clone can't
        # cross the encryption boundary); unencrypted use the fast linked clone on the default pool
        upid = px_cfg.clone(
            image.template_vmid,
            instance.vmid,
            name=instance.label,
            full=True if instance.encrypted else s.CLONE_FULL,
            storage=s.TENANT_STORAGE if instance.encrypted else None,
        )
        _wait_task(px_cfg, upid, timeout=600)

    # 3. re-provision with the existing IP/mac/hostname and current plan size
    ip = instance.ip
    ssh_keys = _ssh_keys_blob(instance, "")
    hostname = instance.hostname or f"{instance.label}.{s.BASE_DOMAIN}"
    px_cfg.set_cloudinit(
        instance.vmid,
        hostname=hostname,
        ssh_keys=ssh_keys,
        ipconfig0=ipam.cloudinit_ipconfig(ip),
        cicustom=(get_settings().TLS_SNIPPET if instance.tls_passthrough else None),
    )
    px_cfg.set_nic(instance.vmid, mac=ip.mac, rate_mbps=plan.net_mbps)
    px_cfg.set_resources(instance.vmid, vcpus=plan.vcpus, memory_mb=plan.memory_mb)
    if plan.disk_gb > image.min_disk_gb:
        px_cfg.resize_disk(instance.vmid, "scsi0", plan.disk_gb)
    px_cfg.apply_ipfilter(instance.vmid, ip=ip.address, mac=ip.mac, peer_ips=_owner_peer_ips(instance))
    db.commit()

    # 4. boot -> readiness gate
    px_pow.start(instance.vmid)
    states.transition(instance, states.BOOTING)
    db.add(Job(type="instance.await_ready", instance_id=instance.id, run_after=_in(6)))
    _event(db, instance, "instance.rebuild", "succeeded", vmid=instance.vmid)
    db.commit()


def instance_rollback(db: Session, job: Job) -> None:
    """Roll the VM back to a snapshot (stop -> rollback disk -> start). Idempotent enough:
    a retry re-runs rollback (returns the disk to the same snapshot) and boots."""
    instance = db.get(Instance, job.instance_id)
    snapname = job.payload["snapshot"]
    px_cfg = ProxmoxClient("config")
    px_pow = ProxmoxClient("power")
    if instance.status != states.RESTORING:
        states.transition(instance, states.RESTORING)
        db.commit()
    if px_pow.vm_status(instance.vmid).get("status") == "running":
        stop_upid = px_pow.stop(instance.vmid)
        _wait_task(px_pow, stop_upid, timeout=90)
    upid = px_cfg.rollback(instance.vmid, snapname)
    _wait_task(px_cfg, upid, timeout=300)
    px_pow.start(instance.vmid)
    states.transition(instance, states.BOOTING)
    db.add(Job(type="instance.await_ready", instance_id=instance.id, run_after=_in(6)))
    _event(db, instance, "instance.rollback", "succeeded", snapshot=snapname)
    db.commit()


def instance_backup(db: Session, job: Job) -> None:
    """Off-host backup: snapshot-mode vzdump to the PBS datastore. The VM keeps running and
    its status is untouched — the job row itself is the 'backup in progress' signal (and the
    one-active-job guard keeps power actions out of the way). Idempotent: a retry just takes
    a fresh backup, which PBS dedups against the last one."""
    s = get_settings()
    instance = db.get(Instance, job.instance_id)
    px = ProxmoxClient("backup")
    upid = px.backup(instance.vmid, s.BACKUP_STORAGE)
    _wait_task(px, upid, timeout=3600)  # first backup of a big disk is the slow path; dedup after
    instance.last_backup_at = datetime.now(UTC)
    _event(db, instance, "instance.backup", "succeeded", storage=s.BACKUP_STORAGE)
    db.commit()


def instance_restore_backup(db: Session, job: Job) -> None:
    """Restore a PBS backup OVER the instance (stop -> restore disks+config -> boot). The
    volid was ownership-checked at enqueue; re-check here anyway since the payload outlives
    that request. Idempotent: a retry re-runs the restore to the same archive."""
    instance = db.get(Instance, job.instance_id)
    volid = job.payload["volid"]
    if f"/vm/{instance.vmid}/" not in f"/{volid.split(':', 1)[-1]}/":
        raise RuntimeError(f"volid {volid} does not belong to vm {instance.vmid}")
    px_bak = ProxmoxClient("backup")
    px_cfg = ProxmoxClient("config")
    px_pow = ProxmoxClient("power")
    if instance.status != states.RESTORING:
        states.transition(instance, states.RESTORING)
        db.commit()
    if px_pow.vm_status(instance.vmid).get("status") == "running":
        stop_upid = px_pow.stop(instance.vmid)
        _wait_task(px_pow, stop_upid, timeout=90)
    upid = px_bak.restore_backup(instance.vmid, volid)
    _wait_task(px_bak, upid, timeout=1800)
    # the archive carries config+firewall from backup time; re-pin the CURRENT allocation
    # (idempotent) in case rules/peers changed since the backup was taken
    if instance.ip:
        px_cfg.apply_ipfilter(
            instance.vmid, ip=instance.ip.address, mac=instance.ip.mac, peer_ips=_owner_peer_ips(instance)
        )
    px_pow.start(instance.vmid)
    states.transition(instance, states.BOOTING)
    db.add(Job(type="instance.await_ready", instance_id=instance.id, run_after=_in(6)))
    _event(db, instance, "instance.restore_backup", "succeeded", volid=volid)
    db.commit()


def _simple_power(verb: str, through: str, to: str, action):
    def handler(db: Session, job: Job) -> None:
        instance = db.get(Instance, job.instance_id)
        px = ProxmoxClient("power")
        if instance.status != through:  # first attempt sets it; retries find it set
            states.transition(instance, through)
            db.commit()
        action(px, instance.vmid)
        states.transition(instance, to)
        _event(db, instance, verb, "succeeded")
        db.commit()

    return handler


def instance_destroy(db: Session, job: Job) -> None:
    instance = db.get(Instance, job.instance_id)
    if instance.status != states.DESTROYING:
        states.transition(instance, states.DESTROYING)
        db.commit()
    if instance.vmid is not None:
        px_cfg = ProxmoxClient("config")
        px_pow = ProxmoxClient("power")
        try:
            if px_pow.vm_status(instance.vmid).get("status") == "running":
                stop_upid = px_pow.stop(instance.vmid)
                _wait_task(px_pow, stop_upid, timeout=90)  # delete 500s if the VM is still running
            upid = px_cfg.delete(instance.vmid)
            _wait_task(px_cfg, upid)
        except ProxmoxError as e:
            if "does not exist" not in str(e):
                raise
    # forget the PBS backup group so a future tenant on a reused vmid can never see (or
    # restore) this tenant's data. Best-effort — prune caps whatever lingers.
    if instance.vmid is not None and instance.backups_enabled:
        from app import pbs

        pbs.forget_group(instance.vmid)
    # best-effort: drop the PoppaPing monitor so it doesn't alert forever on a gone server
    if instance.poppaping_monitor_id and instance.user.poppaping_api_key:
        import contextlib

        from app import poppaping

        with contextlib.suppress(Exception):
            poppaping.delete_monitor(instance.user.poppaping_api_key, instance.poppaping_monitor_id)
    ipam.release(db, instance)
    for route in list(instance.http_routes):
        db.delete(route)
    for mapping in list(instance.edge_mappings):
        db.delete(mapping)
    owner = instance.user
    instance.vmid = None
    instance.hostname = None  # release <label>.<base domain> so the name can be reused
    states.transition(instance, states.DESTROYED)
    _event(db, instance, "instance.destroy", "succeeded")
    db.commit()
    # remaining servers drop this one from their peer allow-list
    sync_owner_firewalls(db, owner)


HANDLERS = {
    "instance.create": instance_create,
    "instance.await_ready": instance_await_ready,
    "instance.start": _simple_power("instance.start", states.STARTING, states.RUNNING, lambda px, v: px.start(v)),
    "instance.stop": _simple_power("instance.stop", states.STOPPING, states.STOPPED, lambda px, v: px.shutdown(v)),
    "instance.reboot": _simple_power("instance.reboot", states.REBOOTING, states.RUNNING, lambda px, v: px.reboot(v)),
    "instance.resize": instance_resize,
    "instance.rebuild": instance_rebuild,
    "instance.rollback": instance_rollback,
    "instance.destroy": instance_destroy,
    "instance.backup": instance_backup,
    "instance.restore_backup": instance_restore_backup,
    # TODO: instance.suspend/unsuspend
}
