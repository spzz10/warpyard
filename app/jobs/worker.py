"""Worker loop: claim -> dispatch -> succeed/fail-with-backoff. Run as its own
process/service next to the API: `python -m app.jobs.worker`."""

import logging
import signal
import socket
import time

from app.config import get_settings
from app.database import SessionLocal
from app.jobs import queue
from app.jobs.handlers import HANDLERS

log = logging.getLogger("warpyard.worker")

_shutdown = False


def _handle_sigterm(signum, frame):
    global _shutdown
    _shutdown = True


def run_once(worker_id: str) -> bool:
    """Process at most one job. Returns True if a job was handled."""
    db = SessionLocal()
    try:
        job = queue.claim(db, worker_id)
        if job is None:
            return False
        handler = HANDLERS.get(job.type)
        if handler is None:
            queue.fail(db, job, f"no handler for job type {job.type}")
            return True
        log.info("job %s %s (instance=%s, attempt %s)", job.id, job.type, job.instance_id, job.attempts + 1)
        try:
            handler(db, job)
            queue.succeed(db, job)
        except Exception as e:  # noqa: BLE001 — any handler error is a retryable job failure
            db.rollback()
            log.exception("job %s failed", job.id)
            queue.fail(db, job, f"{type(e).__name__}: {e}")
        return True
    finally:
        db.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)
    worker_id = f"{socket.gethostname()}:{time.time_ns() % 100000}"
    poll = get_settings().WORKER_POLL_SECONDS
    log.info("worker %s started", worker_id)

    # On startup, reclaim jobs stranded 'running' by a previous worker that was killed
    # mid-job (e.g. a redeploy restart). This deployment runs one worker, so anything
    # still 'running' when we boot has no live owner — recover it immediately.
    db = SessionLocal()
    try:
        recovered = queue.requeue_stale(db, older_than_seconds=0)
        if recovered:
            log.warning("recovered %d stranded job(s) from a previous worker", recovered)
    finally:
        db.close()

    ticks = 0
    while not _shutdown:
        if not run_once(worker_id):
            time.sleep(poll)
        # periodic safety net for jobs that hang mid-run (uses the stale threshold so it
        # never steals the job this worker is actively processing)
        ticks += 1
        if ticks % 60 == 0:
            db = SessionLocal()
            try:
                queue.requeue_stale(db)
                # flip pending custom domains active once their DNS points at the edge
                from app import service

                if flipped := service.recheck_pending_domains(db):
                    log.info("activated %d custom domain(s)", flipped)
                # nightly PBS backups for servers with the add-on enabled
                if queued := service.schedule_due_backups(db):
                    log.info("scheduled %d nightly backup(s)", queued)
                # opted-in nightly game-server restarts (held while players are online)
                if restarted := service.schedule_due_restarts(db):
                    log.info("scheduled %d nightly restart(s)", restarted)
            finally:
                db.close()
    log.info("worker %s stopped", worker_id)


if __name__ == "__main__":
    main()
