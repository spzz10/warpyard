"""Metrics endpoint (owner-gated rrd series for the charts) and live player counts
surfaced through connect_info onto the server page, dashboard cards and board."""

from app import gamequery, security, vmstats
from app.models import EdgeMapping, Image, Instance, User


def _login(client, db, user):
    user.password_hash = security.hash_password("pw12345678")
    db.commit()
    r = client.post("/login", data={"email": user.email, "password": "pw12345678"})
    assert r.status_code in (200, 303)


def _game_instance(db, seeded, owner, label="mc1", shared=False):
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
            guidance="join {endpoint}",
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
        vmid=91000 + owner.id,
        shared=shared,
    )
    db.add(i)
    db.flush()
    db.add(EdgeMapping(instance_id=i.id, protocol="tcp", public_port=30040, target_port=25565))
    db.add(EdgeMapping(instance_id=i.id, protocol="tcp", public_port=2240, target_port=22))
    db.commit()
    return i


def test_metrics_requires_owner(client, db, seeded, monkeypatch):
    buddy = User(email="buddy@example.com")
    db.add(buddy)
    db.commit()
    i = _game_instance(db, seeded, buddy)
    _login(client, db, seeded["user"])
    assert client.get(f"/servers/{i.id}/metrics.json").status_code == 404


def test_metrics_shape_and_timeframe(client, db, seeded, monkeypatch):
    i = _game_instance(db, seeded, seeded["user"])
    _login(client, db, seeded["user"])

    def fake_series(vmid, tf="hour"):
        if tf not in ("hour", "day", "week"):  # mirror the real validation
            raise ValueError(tf)
        return {"timeframe": tf, "points": [{"t": 1, "cpu": 1.5}], "now": {"cpu": 1.5}}

    monkeypatch.setattr(vmstats, "series", fake_series)
    r = client.get(f"/servers/{i.id}/metrics.json?timeframe=day")
    assert r.status_code == 200
    body = r.json()
    assert body["timeframe"] == "day" and body["points"][0]["cpu"] == 1.5
    assert client.get(f"/servers/{i.id}/metrics.json?timeframe=year").status_code == 422


def test_players_on_server_page_and_board(client, db, seeded, monkeypatch):
    gamequery.clear_cache()
    monkeypatch.setattr(gamequery, "players_for_slug", lambda slug, host, port: {"online": 3, "max": 20})
    i = _game_instance(db, seeded, seeded["user"], shared=True)
    _login(client, db, seeded["user"])
    page = client.get(f"/servers/{i.id}").text
    assert "3 / 20 playing" in page
    board = client.get("/board").text
    assert "3 / 20 playing" in board


def test_players_absent_when_query_fails(client, db, seeded, monkeypatch):
    gamequery.clear_cache()
    monkeypatch.setattr(gamequery, "players_for_slug", lambda slug, host, port: None)
    i = _game_instance(db, seeded, seeded["user"])
    _login(client, db, seeded["user"])
    page = client.get(f"/servers/{i.id}").text
    # placeholder chip renders hidden so the OOB poll can reveal it later
    assert 'id="players-chip"' in page and "playing" not in page.split('id="players-chip"')[1][:120]
