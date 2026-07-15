import pytest

from app import states
from app.jobs import queue
from app.models import Instance, Job


@pytest.fixture()
def instance(db, seeded):
    i = Instance(user_id=seeded["user"].id, plan_id=seeded["plan"].id, image_id=seeded["image"].id, label="web1")
    db.add(i)
    db.commit()
    return i


def test_enqueue_and_claim(db, instance):
    job = queue.enqueue(db, "instance.create", instance_id=instance.id)
    db.commit()
    claimed = queue.claim(db, "w1")
    assert claimed.id == job.id
    assert claimed.status == "running"
    assert claimed.locked_by == "w1"
    assert queue.claim(db, "w2") is None  # nothing else runnable


def test_one_active_job_per_instance(db, instance):
    queue.enqueue(db, "instance.create", instance_id=instance.id)
    db.commit()
    with pytest.raises(queue.JobConflict):
        queue.enqueue(db, "instance.stop", instance_id=instance.id)


def test_fail_retries_with_backoff_then_dead(db, instance):
    job = queue.enqueue(db, "instance.create", instance_id=instance.id)
    db.commit()
    job = queue.claim(db, "w1")
    first_run_after = job.run_after

    queue.fail(db, job, "proxmox timeout")
    assert job.status == "failed"
    assert job.attempts == 1
    assert job.run_after > first_run_after  # backoff scheduled

    # exhaust remaining attempts
    for _ in range(job.max_attempts - 1):
        job.run_after = first_run_after
        job.status = "queued"
        db.commit()
        job = queue.claim(db, "w1")
        queue.fail(db, job, "proxmox timeout")

    assert job.status == "dead"
    db.refresh(instance)
    assert instance.status == states.ERROR
    assert "instance.create" in instance.error


def test_succeed(db, instance):
    queue.enqueue(db, "instance.create", instance_id=instance.id)
    db.commit()
    job = queue.claim(db, "w1")
    queue.succeed(db, job)
    assert db.get(Job, job.id).status == "succeeded"


def test_requeue_stale_recovers_stranded_job(db, instance):
    # simulate a job left 'running' by a worker that died (redeploy/crash)
    job = queue.enqueue(db, "instance.create", instance_id=instance.id)
    db.commit()
    claimed = queue.claim(db, "dead-worker")
    assert claimed.status == "running"
    # older_than=0 reclaims it (startup recovery); it goes back to queued + claimable
    assert queue.requeue_stale(db, older_than_seconds=0) == 1
    db.refresh(claimed)
    assert claimed.status == "queued" and claimed.locked_by is None
    assert queue.claim(db, "new-worker").id == job.id


def test_requeue_stale_leaves_fresh_running_jobs(db, instance):
    queue.enqueue(db, "instance.create", instance_id=instance.id)
    db.commit()
    queue.claim(db, "live-worker")  # just claimed, locked_at = now
    # with the real threshold, a freshly-claimed job is NOT stolen
    assert queue.requeue_stale(db, older_than_seconds=300) == 0
