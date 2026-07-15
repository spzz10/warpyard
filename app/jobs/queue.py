"""Postgres-backed job queue.

Why not Redis/celery: enqueue + instance state transition must be one transaction,
and the reconciler needs to reason about jobs and instances in the same snapshot.
Workers claim with FOR UPDATE SKIP LOCKED (no-op under SQLite in tests, which run
single-threaded).
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import mailer, states
from app.config import get_settings
from app.models import Event, Instance, Job, utcnow

ACTIVE_JOB_STATUSES = ("queued", "running", "failed")  # failed = retry pending

BACKOFF_BASE_SECONDS = 30
# A job left "running" longer than this lost its worker (crash/restart). Handlers are
# idempotent, so re-queueing and re-running is safe. Well above any real job duration.
STALE_LOCK_SECONDS = 300


class JobConflict(Exception):
    """An active job already exists for this instance (one mutating job at a time)."""


def enqueue(
    db: Session,
    type_: str,
    instance_id: int | None = None,
    payload: dict | None = None,
    run_after: datetime | None = None,
) -> Job:
    """Create a job. Caller commits — so the instance state transition that goes
    with this job lands in the same transaction. Enforces one active mutating job
    per instance."""
    if instance_id is not None:
        active = db.scalar(
            select(Job.id).where(Job.instance_id == instance_id, Job.status.in_(ACTIVE_JOB_STATUSES)).limit(1)
        )
        if active is not None:
            raise JobConflict(f"instance {instance_id} already has active job {active}")
    job = Job(
        type=type_,
        instance_id=instance_id,
        payload=payload or {},
        run_after=run_after or utcnow(),
        max_attempts=get_settings().JOB_MAX_ATTEMPTS,
    )
    db.add(job)
    db.flush()
    return job


def claim(db: Session, worker_id: str) -> Job | None:
    """Claim the oldest runnable job. Commits the claim immediately so other
    workers skip it even if the handler runs long."""
    stmt = select(Job).where(Job.status.in_(("queued", "failed")), Job.run_after <= utcnow()).order_by(Job.id).limit(1)
    if db.bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)
    job = db.scalar(stmt)
    if job is None:
        return None
    job.status = "running"
    job.locked_by = worker_id
    job.locked_at = utcnow()
    db.commit()
    return job


def requeue_stale(db: Session, older_than_seconds: int = STALE_LOCK_SECONDS) -> int:
    """Reclaim jobs stuck in 'running' whose worker died (crash or restart). Their
    lock is cleared and they go back to 'queued'. Handlers are idempotent, so the
    re-run resumes where the dead worker left off. Returns how many were recovered."""
    cutoff = datetime.now(UTC) - timedelta(seconds=older_than_seconds)
    stale = db.scalars(select(Job).where(Job.status == "running", Job.locked_at < cutoff)).all()
    for job in stale:
        job.status = "queued"
        job.locked_by = None
        job.locked_at = None
    if stale:
        db.commit()
    return len(stale)


def succeed(db: Session, job: Job) -> None:
    job.status = "succeeded"
    job.error = None
    db.commit()


def fail(db: Session, job: Job, error: str) -> None:
    """Retry with exponential backoff; after max_attempts the job is dead and the
    instance goes to `error` (docs/VERBS.md: no limbo states)."""
    job.attempts += 1
    job.error = error[:4000]
    alert = None  # (to, label, hostname, instance_id, detail) captured pre-commit
    if job.attempts >= job.max_attempts:
        job.status = "dead"
        if job.instance_id is not None:
            instance = db.get(Instance, job.instance_id)
            if instance is not None and instance.status not in states.TERMINAL_STATES:
                old = instance.status
                instance.status = states.ERROR  # forced, not transition(): error is always reachable
                instance.error = f"job {job.type} dead after {job.attempts} attempts: {error[:500]}"
                db.add(
                    Event(
                        instance_id=instance.id,
                        user_id=instance.user_id,
                        action=job.type,
                        status="failed",
                        detail={"job_id": job.id, "error": error[:500]},
                    )
                )
                # edge-triggered owner alert: only on the transition INTO error, so an
                # already-errored instance whose retry dies again doesn't email twice
                if old != states.ERROR:
                    alert = (instance.user.email, instance.label, instance.hostname or "", instance.id, instance.error)
    else:
        job.status = "failed"
        backoff = timedelta(seconds=BACKOFF_BASE_SECONDS * (2 ** (job.attempts - 1)))
        job.run_after = datetime.now(UTC) + backoff
    db.commit()
    if alert:  # after the commit, so a mail hiccup can never roll back the state change
        to, label, hostname, instance_id, detail = alert
        mailer.send_server_alert(to, label, hostname, "error", detail=detail, server_id=instance_id)
