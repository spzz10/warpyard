import pytest

from app import states
from app.models import Instance


def make(status):
    i = Instance(user_id=1, plan_id=1, image_id=1, label="x")
    i.status = status
    return i


def test_happy_path_create():
    i = make(states.PROVISIONING)
    states.transition(i, states.RUNNING)
    assert i.status == states.RUNNING


def test_illegal_transition_raises():
    i = make(states.STOPPED)
    with pytest.raises(states.InvalidTransition):
        states.transition(i, states.STOPPING)  # can't stop a stopped instance


def test_destroyed_is_terminal():
    i = make(states.DESTROYED)
    for target in states.ALL_STATES - {states.DESTROYED}:
        with pytest.raises(states.InvalidTransition):
            states.transition(i, target)


def test_every_state_can_reach_destroying_except_terminal():
    for s in states.ALL_STATES - states.TERMINAL_STATES - {states.DESTROYING}:
        assert states.DESTROYING in states.TRANSITIONS[s], f"{s} cannot be destroyed"


def test_error_reachable_from_all_inflight_states():
    for s in (
        states.PROVISIONING,
        states.STOPPING,
        states.STARTING,
        states.REBOOTING,
        states.REBUILDING,
        states.RESIZING,
    ):
        assert states.ERROR in states.TRANSITIONS[s]


def test_verb_gating():
    assert states.can_enqueue("instance.start", states.STOPPED)
    assert not states.can_enqueue("instance.start", states.RUNNING)
    assert states.can_enqueue("instance.rebuild", states.ERROR)
    assert not states.can_enqueue("instance.destroy", states.DESTROYED)


def test_booting_between_provisioning_and_running():
    i = make(states.PROVISIONING)
    states.transition(i, states.BOOTING)  # create hands off to readiness poller
    assert i.status == states.BOOTING
    states.transition(i, states.RUNNING)  # ready
    assert i.status == states.RUNNING


def test_await_ready_only_from_booting():
    assert states.can_enqueue("instance.await_ready", states.BOOTING)
    assert not states.can_enqueue("instance.await_ready", states.RUNNING)
    assert states.DESTROYING in states.TRANSITIONS[states.BOOTING]  # can delete a booting server


def test_rebuild_and_rollback_route_through_booting():
    for src in (states.RUNNING, states.STOPPED):
        i = make(src)
        states.transition(i, states.REBUILDING)
        assert states.BOOTING in states.TRANSITIONS[states.REBUILDING]
        i2 = make(src)
        states.transition(i2, states.RESTORING)
        assert states.BOOTING in states.TRANSITIONS[states.RESTORING]
    assert states.can_enqueue("instance.rebuild", states.RUNNING)
    assert states.can_enqueue("instance.rollback", states.STOPPED)
    assert not states.can_enqueue("instance.rollback", states.BOOTING)
