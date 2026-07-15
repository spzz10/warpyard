"""Instance state machine — the single source of truth for lifecycle transitions.

Mirrors docs/VERBS.md. Every mutation of Instance.status must go through
`transition()`; handlers and the reconciler never assign status strings directly.
"""

PROVISIONING = "provisioning"
BOOTING = "booting"  # VM started, waiting for the OS to finish booting (SSH up) before it's usable
RUNNING = "running"
STOPPING = "stopping"
STOPPED = "stopped"
STARTING = "starting"
REBOOTING = "rebooting"
REBUILDING = "rebuilding"
RESIZING = "resizing"
RESTORING = "restoring"  # rolling back to a snapshot
SUSPENDED = "suspended"
DESTROYING = "destroying"
DESTROYED = "destroyed"
ERROR = "error"

ALL_STATES = {
    PROVISIONING,
    BOOTING,
    RUNNING,
    STOPPING,
    STOPPED,
    STARTING,
    REBOOTING,
    REBUILDING,
    RESIZING,
    RESTORING,
    SUSPENDED,
    DESTROYING,
    DESTROYED,
    ERROR,
}
TERMINAL_STATES = {DESTROYED}

# from-state -> set of allowed to-states
TRANSITIONS: dict[str, set[str]] = {
    PROVISIONING: {BOOTING, RUNNING, ERROR, DESTROYING},
    BOOTING: {RUNNING, STOPPING, ERROR, DESTROYING},
    RUNNING: {STOPPING, REBOOTING, REBUILDING, RESIZING, RESTORING, SUSPENDED, DESTROYING, ERROR},
    STOPPING: {STOPPED, ERROR, DESTROYING},
    STOPPED: {STARTING, REBUILDING, RESIZING, RESTORING, SUSPENDED, DESTROYING, ERROR},
    STARTING: {RUNNING, BOOTING, ERROR, DESTROYING},
    REBOOTING: {RUNNING, ERROR, DESTROYING},
    REBUILDING: {RUNNING, BOOTING, ERROR, DESTROYING},
    RESIZING: {RUNNING, BOOTING, STOPPED, ERROR, DESTROYING},
    RESTORING: {RUNNING, BOOTING, STOPPED, ERROR, DESTROYING},
    SUSPENDED: {STOPPED, DESTROYING, ERROR},
    DESTROYING: {DESTROYED, ERROR},
    # human decides: rebuild, restore a backup, or destroy (or a retried job resumes)
    ERROR: {REBUILDING, RESTORING, DESTROYING},
    DESTROYED: set(),
}

# job type -> states it may be enqueued from
VERB_FROM_STATES: dict[str, set[str]] = {
    "instance.start": {STOPPED},
    "instance.stop": {RUNNING},
    "instance.reboot": {RUNNING},
    "instance.rebuild": {RUNNING, STOPPED, ERROR},
    "instance.resize": {RUNNING, STOPPED},
    "instance.destroy": ALL_STATES - TERMINAL_STATES,
    "instance.suspend": {RUNNING, STOPPED},
    "instance.unsuspend": {SUSPENDED},
    "instance.snapshot": {RUNNING, STOPPED},
    "instance.await_ready": {BOOTING},  # internal: poll until the OS is up, then -> running
    "instance.rollback": {RUNNING, STOPPED},
    "instance.backup": {RUNNING, STOPPED},  # vzdump snapshot mode works live; status unchanged
    "instance.restore_backup": {RUNNING, STOPPED, ERROR},  # a backup is also the disaster exit
}


class InvalidTransition(Exception):
    pass


def transition(instance, to_state: str) -> None:
    """Move an instance to to_state, enforcing the transition table."""
    if to_state not in TRANSITIONS.get(instance.status, set()):
        raise InvalidTransition(f"instance {instance.id}: {instance.status} -> {to_state} not allowed")
    instance.status = to_state


def can_enqueue(verb: str, status: str) -> bool:
    return status in VERB_FROM_STATES.get(verb, set())
