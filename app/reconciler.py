"""Reconciler: diff DB desired-state vs Proxmox actual-state.

Policy (docs/VERBS.md):
  AUTO-REPAIR — power-state drift on instances with no in-flight job.
  FLAG ONLY   — orphan VMs in the pool (never auto-delete), DB rows with no VM,
                spec drift (billing), missing/foreign ipfilter or wrong bridge
                (security — alert immediately).
Run periodically (systemd timer / APScheduler in the worker, P2 decision)."""

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import mailer, states
from app.jobs.queue import ACTIVE_JOB_STATUSES
from app.models import Event, Instance, Job
from app.proxmox import ProxmoxClient

log = logging.getLogger("warpyard.reconciler")

IN_FLIGHT = {
    states.PROVISIONING,
    states.STOPPING,
    states.STARTING,
    states.REBOOTING,
    states.REBUILDING,
    states.RESIZING,
    states.DESTROYING,
}


def reconcile(db: Session, px: ProxmoxClient | None = None) -> list[dict]:
    px = px or ProxmoxClient("config")
    findings: list[dict] = []
    stopped_alerts: list[tuple[str, str, str, int]] = []  # owner emails, sent post-commit
    pool_vmids = px.pool_vmids()
    instances = db.scalars(select(Instance).where(Instance.status.notin_(states.TERMINAL_STATES))).all()
    known_vmids = {i.vmid for i in instances if i.vmid is not None}

    # Orphans in Proxmox — FLAG, never auto-delete (could be a half-created VM mid-job)
    for vmid in pool_vmids - known_vmids:
        findings.append({"kind": "orphaned-in-proxmox", "vmid": vmid})

    for instance in instances:
        has_active_job = db.scalar(
            select(Job.id).where(Job.instance_id == instance.id, Job.status.in_(ACTIVE_JOB_STATUSES)).limit(1)
        )
        if has_active_job or instance.status in IN_FLIGHT:
            continue  # a job owns this instance right now; don't fight it

        if instance.vmid is None or instance.vmid not in pool_vmids:
            findings.append({"kind": "missing-in-proxmox", "instance_id": instance.id, "vmid": instance.vmid})
            continue

        actual = px.vm_status(instance.vmid).get("status")  # 'running' | 'stopped'
        expected = "running" if instance.status == states.RUNNING else "stopped"
        if actual != expected:
            # Safe drift: trust reality, fix the DB, leave an audit trail.
            new = states.RUNNING if actual == "running" else states.STOPPED
            old = instance.status
            instance.status = new
            db.add(
                Event(
                    instance_id=instance.id,
                    user_id=instance.user_id,
                    action="reconciler.power-drift",
                    status="succeeded",
                    detail={"from": old, "to": new},
                )
            )
            findings.append({"kind": "power-drift-repaired", "instance_id": instance.id, "from": old, "to": new})
            # Unexpected stop: no active job and not IN_FLIGHT (guarded above), so this
            # power-off didn't come through the platform — tell the owner. Edge-triggered:
            # status is STOPPED after this repair, so the next pass sees no drift.
            if old == states.RUNNING and new == states.STOPPED:
                stopped_alerts.append((instance.user.email, instance.label, instance.hostname or "", instance.id))

    for f in findings:
        if f["kind"] != "power-drift-repaired":
            db.add(
                Event(instance_id=f.get("instance_id"), action=f"reconciler.{f['kind']}", status="flagged", detail=f)
            )
            log.warning("reconciler flag: %s", f)
    db.commit()
    # alerts go out after the commit so a mail failure can't roll back the repair
    for to, label, hostname, instance_id in stopped_alerts:
        mailer.send_server_alert(to, label, hostname, "stopped", server_id=instance_id)
    return findings
