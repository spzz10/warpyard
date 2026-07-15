"""Player-aware scheduled nightly restarts: schedule_due_restarts enqueues instance.reboot
once per ~day for opted-in running game servers, holding while anyone is playing."""

from datetime import UTC, datetime

from sqlalchemy import select

from app import gamequery, service
from app.models import EdgeMapping, Instance, Job

FIXED_NOW = datetime(2026, 7, 13, 9, 30, tzinfo=UTC)  # 09:30 UTC


def _game(db, seeded, restart_at="09:00", status="running", with_map=True, label="g1", vmid=90001, **kw):
    i = Instance(
        user_id=seeded["user"].id,
        plan_id=seeded["plan"].id,
        image_id=seeded["image"].id,
        label=label,
        hostname=f"{label}.warpyard.test",
        vmid=vmid,
        restart_enabled=True,
        restart_at=restart_at,
        **kw,
    )
    i.status = status
    db.add(i)
    db.flush()
    if with_map:
        db.add(EdgeMapping(instance_id=i.id, protocol="tcp", public_port=30001 + i.id, target_port=25565))
        db.add(EdgeMapping(instance_id=i.id, protocol="tcp", public_port=2201 + i.id, target_port=22))
    db.commit()
    return i


def _freeze(monkeypatch, players):
    monkeypatch.setattr(service, "utcnow", lambda: FIXED_NOW)
    monkeypatch.setattr(gamequery, "players_for_slug", lambda slug, host, port: players)


def _reboot_jobs(db):
    return db.scalars(select(Job).where(Job.type == "instance.reboot")).all()


def _aware(dt):
    return dt.replace(tzinfo=UTC) if dt is not None and dt.tzinfo is None else dt  # sqlite: naive round-trip


def test_due_empty_server_enqueues_once(db, seeded, monkeypatch):
    _freeze(monkeypatch, {"online": 0, "max": 20})
    i = _game(db, seeded)
    assert service.schedule_due_restarts(db) == 1
    assert len(_reboot_jobs(db)) == 1
    assert _aware(i.last_auto_restart_at) == FIXED_NOW
    # same window: debounced by last_auto_restart_at
    assert service.schedule_due_restarts(db) == 0
    assert len(_reboot_jobs(db)) == 1


def test_held_while_players_online_then_fires_when_empty(db, seeded, monkeypatch):
    _freeze(monkeypatch, {"online": 3, "max": 20})
    i = _game(db, seeded)
    assert service.schedule_due_restarts(db) == 0
    assert i.last_auto_restart_at is None and not _reboot_jobs(db)
    # players leave -> next tick restarts
    monkeypatch.setattr(gamequery, "players_for_slug", lambda slug, host, port: {"online": 0, "max": 20})
    assert service.schedule_due_restarts(db) == 1
    assert _aware(i.last_auto_restart_at) == FIXED_NOW


def test_query_failure_still_restarts(db, seeded, monkeypatch):
    _freeze(monkeypatch, None)  # unsupported game / dead query must not wedge the schedule
    _game(db, seeded)
    assert service.schedule_due_restarts(db) == 1


def test_no_game_mapping_still_restarts(db, seeded, monkeypatch):
    _freeze(monkeypatch, {"online": 5, "max": 20})  # would hold, but there's no map to query
    _game(db, seeded, with_map=False)
    assert service.schedule_due_restarts(db) == 1


def test_stopped_server_never_restarts(db, seeded, monkeypatch):
    _freeze(monkeypatch, {"online": 0, "max": 20})
    _game(db, seeded, status="stopped")
    assert service.schedule_due_restarts(db) == 0
    assert not _reboot_jobs(db)


def test_malformed_time_skipped_without_error(db, seeded, monkeypatch):
    _freeze(monkeypatch, {"online": 0, "max": 20})
    _game(db, seeded, restart_at="9am", label="bad1", vmid=90011)
    _game(db, seeded, restart_at="25:61", label="bad2", vmid=90012)
    assert service.schedule_due_restarts(db) == 0
    assert not _reboot_jobs(db)


def test_before_scheduled_time_does_nothing(db, seeded, monkeypatch):
    _freeze(monkeypatch, {"online": 0, "max": 20})
    _game(db, seeded, restart_at="10:00")  # now is 09:30
    assert service.schedule_due_restarts(db) == 0


def test_anchored_to_schedule_no_drift(db, seeded, monkeypatch):
    """A restart yesterday (even a late one) doesn't push today's past its slot: due is
    anchored to today's restart_at, not a rolling cooldown since the last fire."""
    _freeze(monkeypatch, {"online": 0, "max": 20})
    i = _game(db, seeded, last_auto_restart_at=datetime(2026, 7, 12, 21, 45, tzinfo=UTC))
    assert service.schedule_due_restarts(db) == 1
    assert _aware(i.last_auto_restart_at) == FIXED_NOW


def test_enabling_past_time_waits_for_next_occurrence(db, seeded, monkeypatch):
    """Enabling (or editing) a schedule whose time already passed today must not reboot
    on the spot — the first fire is the next occurrence of restart_at."""
    _freeze(monkeypatch, {"online": 0, "max": 20})
    i = _game(db, seeded)
    service.set_restart_schedule(db, seeded["user"], i.id, True, "09:00")  # now is 09:30
    assert service.schedule_due_restarts(db) == 0
    assert not _reboot_jobs(db)
    monkeypatch.setattr(service, "utcnow", lambda: datetime(2026, 7, 14, 9, 0, tzinfo=UTC))
    assert service.schedule_due_restarts(db) == 1
