from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app import service
from app.config import get_settings
from app.models import Instance, Job


def _running(db, seeded, **kw):
    i = Instance(
        user_id=seeded["user"].id,
        plan_id=seeded["plan"].id,
        image_id=seeded["image"].id,
        label="s",
        vmid=90001,
        **kw,
    )
    i.status = "running"
    db.add(i)
    db.commit()
    return i


class FakePx:
    """Stands in for ProxmoxClient wherever backups talk to PVE."""

    volids = ["warpyard-pbs:backup/vm/90001/2026-07-12T08:00:00Z"]

    def __init__(self, role="backup"):
        self.role = role

    def list_backups(self, vmid, storage):
        return [
            {"volid": v, "ctime": 1783929600, "size": 3 * 1024**3}
            for v in self.volids
            if f"/vm/{vmid}/" in f"/{v.split(':', 1)[-1]}/"
        ]


def test_toggle_and_server_json(db, seeded):
    inst = _running(db, seeded)
    out = service.set_backups(db, seeded["user"], inst.id, True)
    assert out["backups_enabled"] is True
    assert service.server_json(inst)["backups"]["enabled"] is True
    service.set_backups(db, seeded["user"], inst.id, False)
    assert inst.backups_enabled is False


def test_backup_now_enqueues(db, seeded):
    inst = _running(db, seeded)
    out = service.backup_now(db, seeded["user"], inst.id)
    assert out["status"] == "accepted"
    job = db.scalar(select(Job).where(Job.type == "instance.backup"))
    assert job is not None and job.instance_id == inst.id


def test_backup_now_rejected_while_busy(db, seeded):
    inst = _running(db, seeded)
    service.backup_now(db, seeded["user"], inst.id)
    try:
        service.backup_now(db, seeded["user"], inst.id)
        raise AssertionError("expected ServiceError")
    except service.ServiceError as e:
        assert e.status == 409


def test_restore_rejects_foreign_volid(db, seeded, monkeypatch):
    import app.proxmox as proxmox

    monkeypatch.setattr(proxmox, "ProxmoxClient", FakePx)
    inst = _running(db, seeded)  # vmid 90001
    try:
        service.restore_backup(db, seeded["user"], inst.id, "warpyard-pbs:backup/vm/90002/2026-07-12T08:00:00Z")
        raise AssertionError("expected ServiceError")
    except service.ServiceError as e:
        assert e.status == 404
    assert db.scalar(select(Job).where(Job.type == "instance.restore_backup")) is None


def test_restore_enqueues_own_volid(db, seeded, monkeypatch):
    import app.proxmox as proxmox

    monkeypatch.setattr(proxmox, "ProxmoxClient", FakePx)
    inst = _running(db, seeded)
    out = service.restore_backup(db, seeded["user"], inst.id, FakePx.volids[0])
    assert out["status"] == "accepted"
    job = db.scalar(select(Job).where(Job.type == "instance.restore_backup"))
    assert job is not None and job.payload["volid"] == FakePx.volids[0]


def test_scheduler_enqueues_due_only(db, seeded, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "BACKUP_HOUR_UTC", datetime.now(UTC).hour)
    fresh = _running(db, seeded)
    fresh.backups_enabled = True
    fresh.last_backup_at = datetime.now(UTC) - timedelta(hours=2)  # not due
    due = Instance(
        user_id=seeded["user"].id,
        plan_id=seeded["plan"].id,
        image_id=seeded["image"].id,
        label="d",
        vmid=90002,
        backups_enabled=True,
        last_backup_at=datetime.now(UTC) - timedelta(hours=30),
    )
    due.status = "running"
    disabled = Instance(
        user_id=seeded["user"].id,
        plan_id=seeded["plan"].id,
        image_id=seeded["image"].id,
        label="x",
        vmid=90003,
        backups_enabled=False,
    )
    disabled.status = "running"
    db.add_all([due, disabled])
    db.commit()
    assert service.schedule_due_backups(db) == 1
    job = db.scalar(select(Job).where(Job.type == "instance.backup"))
    assert job.instance_id == due.id


def test_scheduler_noop_outside_window(db, seeded, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "BACKUP_HOUR_UTC", (datetime.now(UTC).hour + 1) % 24)
    inst = _running(db, seeded, backups_enabled=True)
    assert service.schedule_due_backups(db) == 0
    assert inst.backups_enabled  # untouched


def test_backup_handler_stamps_last_backup(db, seeded, monkeypatch):
    import app.jobs.handlers as handlers

    class Px(FakePx):
        def backup(self, vmid, storage):
            assert storage == get_settings().BACKUP_STORAGE
            return "UPID:fake"

        def task_status(self, upid):
            return {"status": "stopped", "exitstatus": "OK"}

    monkeypatch.setattr(handlers, "ProxmoxClient", Px)
    inst = _running(db, seeded, backups_enabled=True)
    job = Job(type="instance.backup", instance_id=inst.id)
    db.add(job)
    db.commit()
    handlers.instance_backup(db, job)
    assert inst.last_backup_at is not None
    assert inst.status == "running"  # backup never disturbs the instance state
