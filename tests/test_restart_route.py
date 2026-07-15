"""Web route + UI for the nightly-restart schedule, and picker blurbs."""

from app import security
from app.models import EdgeMapping, Image, Instance, User


def _login(client, db, user):
    user.password_hash = security.hash_password("pw12345678")
    db.commit()
    r = client.post("/login", data={"email": user.email, "password": "pw12345678"})
    assert r.status_code in (200, 303)


def _game(db, seeded, owner, label="mc9"):
    game = db.query(Image).filter_by(slug="minecraft").one_or_none()
    if not game:
        game = Image(
            slug="minecraft",
            name="Minecraft",
            distro="ubuntu",
            version="24.04",
            template_vmid=9003,
            category="game",
            lgsm_game="mcserver",
            ports="tcp:25565",
        )
        db.add(game)
        db.flush()
    i = Instance(
        user_id=owner.id,
        plan_id=seeded["plan"].id,
        image_id=game.id,
        label=label,
        hostname=f"{label}.warpyard.test",
        status="running",
        vmid=92000 + owner.id,
    )
    db.add(i)
    db.flush()
    db.add(EdgeMapping(instance_id=i.id, protocol="tcp", public_port=30060, target_port=25565))
    db.commit()
    return i


def test_restart_schedule_toggle(client, db, seeded):
    i = _game(db, seeded, seeded["user"])
    _login(client, db, seeded["user"])
    r = client.post(f"/servers/{i.id}/restart-schedule", data={"enabled": "on", "at": "06:30"}, follow_redirects=False)
    assert r.status_code == 303
    db.refresh(i)
    assert i.restart_enabled and i.restart_at == "06:30"
    # disable keeps the time for re-enabling
    client.post(f"/servers/{i.id}/restart-schedule", data={"at": "06:30"})
    db.refresh(i)
    assert not i.restart_enabled and i.restart_at == "06:30"


def test_restart_schedule_bad_time_gets_default(client, db, seeded):
    i = _game(db, seeded, seeded["user"])
    _login(client, db, seeded["user"])
    client.post(f"/servers/{i.id}/restart-schedule", data={"enabled": "on", "at": "25:99"})
    db.refresh(i)
    assert i.restart_enabled and i.restart_at == "09:00"


def test_restart_schedule_owner_only(client, db, seeded):
    buddy = User(email="buddy3@example.com")
    db.add(buddy)
    db.commit()
    i = _game(db, seeded, buddy)
    _login(client, db, seeded["user"])
    client.post(f"/servers/{i.id}/restart-schedule", data={"enabled": "on", "at": "06:30"})
    db.refresh(i)
    assert not i.restart_enabled


def test_restart_control_renders_for_games_or_when_active(client, db, seeded):
    # user reconsidered 2026-07-13: restart toggle is game-only (web servers don't need it)
    # …refined 2026-07-14: a schedule enabled via API/MCP must still SHOW on any server,
    # so nothing can be invisibly auto-rebooting
    game = _game(db, seeded, seeded["user"])
    plain = Instance(
        user_id=seeded["user"].id,
        plan_id=seeded["plan"].id,
        image_id=seeded["image"].id,
        label="plain1",
        hostname="plain1.warpyard.test",
        status="running",
        vmid=93999,
    )
    db.add(plain)
    db.commit()
    _login(client, db, seeded["user"])
    assert "Nightly restart" in client.get(f"/servers/{game.id}").text
    assert "Nightly restart" not in client.get(f"/servers/{plain.id}").text
    # an active schedule on a web server surfaces the control
    plain.restart_enabled = True
    plain.restart_at = "09:00"
    db.commit()
    assert "Nightly restart" in client.get(f"/servers/{plain.id}").text


def test_picker_shows_blurb(client, db, seeded):
    seeded["image"].blurb = "Plain Ubuntu, yours to shape"
    db.commit()
    _login(client, db, seeded["user"])
    assert "Plain Ubuntu, yours to shape" in client.get("/new").text
