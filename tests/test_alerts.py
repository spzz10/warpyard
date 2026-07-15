"""Owner email alerts: a job dying flips the instance to error and emails once; the
reconciler emails once on an unexpected stop (running in DB, stopped in Proxmox);
user-initiated flows and @warpyard.test system accounts never email."""

import pytest

from app import mailer, reconciler, states
from app.jobs import queue
from app.models import Instance


@pytest.fixture()
def sent(monkeypatch):
    """Capture send_server_alert calls (patched on the shared mailer module, which is
    the same object queue.py and reconciler.py call through)."""
    calls = []

    def fake(to, label, hostname, kind, detail="", server_id=None):
        calls.append({"to": to, "label": label, "kind": kind, "server_id": server_id, "detail": detail})
        return True

    monkeypatch.setattr(mailer, "send_server_alert", fake)
    return calls


_vmid = iter(range(91000, 99000))


def _instance(db, seeded, owner=None, status="running", label="web1"):
    i = Instance(
        user_id=(owner or seeded["user"]).id,
        plan_id=seeded["plan"].id,
        image_id=seeded["image"].id,
        label=label,
        hostname=f"{label}.warpyard.test",
        status=status,
        vmid=next(_vmid),
    )
    db.add(i)
    db.commit()
    return i


class FakePx:
    """Stub ProxmoxClient: fixed pool membership + per-vmid power state."""

    def __init__(self, statuses: dict[int, str]):
        self.statuses = statuses

    def pool_vmids(self):
        return set(self.statuses)

    def vm_status(self, vmid):
        return {"status": self.statuses[vmid]}


def _kill_job(db, job):
    """Fail a claimed job until it's dead (drives the real queue.fail path)."""
    while job.status != "dead":
        queue.fail(db, job, "proxmox timeout")
        if job.status == "failed":
            job.status = "queued"
            job.run_after = queue.utcnow()
            db.commit()
            job = queue.claim(db, "w1")


def test_dead_job_alerts_owner_once(db, seeded, sent):
    i = _instance(db, seeded)
    queue.enqueue(db, "instance.create", instance_id=i.id)
    db.commit()
    job = queue.claim(db, "w1")
    _kill_job(db, job)
    db.refresh(i)
    assert i.status == states.ERROR
    assert len(sent) == 1
    assert sent[0]["to"] == seeded["user"].email
    assert sent[0]["kind"] == "error"
    assert sent[0]["server_id"] == i.id
    assert "instance.create" in sent[0]["detail"]


def test_dead_job_on_already_errored_instance_does_not_realert(db, seeded, sent):
    i = _instance(db, seeded, status=states.ERROR)
    queue.enqueue(db, "instance.start", instance_id=i.id)
    db.commit()
    _kill_job(db, queue.claim(db, "w1"))
    assert sent == []  # transition guard: old status was already error


def test_user_initiated_stop_sends_nothing(db, seeded, sent):
    i = _instance(db, seeded)
    queue.enqueue(db, "instance.stop", instance_id=i.id)
    db.commit()
    job = queue.claim(db, "w1")
    queue.succeed(db, job)
    # a stop in flight also shields the instance from the reconciler's drift repair
    assert sent == []


def test_reconciler_unexpected_stop_alerts_once(db, seeded, sent):
    i = _instance(db, seeded, status=states.RUNNING)
    px = FakePx({i.vmid: "stopped"})
    findings = reconciler.reconcile(db, px=px)
    assert any(f["kind"] == "power-drift-repaired" for f in findings)
    db.refresh(i)
    assert i.status == states.STOPPED
    assert len(sent) == 1 and sent[0]["kind"] == "stopped" and sent[0]["to"] == seeded["user"].email
    # second pass: DB now agrees with reality -> no drift, no resend
    assert reconciler.reconcile(db, px=px) == []
    assert len(sent) == 1


def test_reconciler_drift_to_running_does_not_alert(db, seeded, sent):
    i = _instance(db, seeded, status=states.STOPPED)
    reconciler.reconcile(db, px=FakePx({i.vmid: "running"}))
    assert sent == []


def test_warpyard_system_accounts_are_skipped(db, monkeypatch):
    # the skip lives inside send_server_alert itself: no send_email call, returns False
    called = []
    monkeypatch.setattr(mailer, "send_email", lambda *a, **k: called.append(a) or True)
    assert mailer.send_server_alert("concierge@warpyard.test", "x", "x.warpyard.test", "stopped") is False
    assert mailer.send_server_alert("dev@warpyard.test", "x", "x.warpyard.test", "error") is False
    assert called == []
    # a real owner does go through
    assert mailer.send_server_alert("friend@example.com", "x", "x.warpyard.test", "stopped", server_id=7) is True
    assert len(called) == 1
