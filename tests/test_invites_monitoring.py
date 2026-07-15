"""Member invite slots (account page) and the PoppaPing monitoring integration."""

from app import poppaping, security
from app.models import EdgeMapping, Image, Instance, Invite


def _login(client, db, user):
    user.password_hash = security.hash_password("pw12345678")
    db.commit()
    r = client.post("/login", data={"email": user.email, "password": "pw12345678"})
    assert r.status_code in (200, 303)


def test_member_invite_slots(client, db, seeded):
    user = seeded["user"]
    _login(client, db, user)
    assert client.post("/account/invites", data={}, follow_redirects=False).status_code == 303
    assert client.post("/account/invites", data={}, follow_redirects=False).status_code == 303
    assert db.query(Invite).filter_by(created_by=user.id).count() == 2
    # third is over the default 2-slot quota
    r = client.post("/account/invites", data={}, follow_redirects=False)
    assert "inverr" in r.headers["location"]
    assert db.query(Invite).filter_by(created_by=user.id).count() == 2
    # the account page renders the open invites
    page = client.get("/account").text
    assert "Invite a friend" in page and "/join/" in page


def test_admin_invites_unlimited(client, db, seeded):
    user = seeded["user"]
    user.is_admin = True
    db.commit()
    _login(client, db, user)
    for _ in range(4):
        client.post("/account/invites", data={})
    assert db.query(Invite).filter_by(created_by=user.id).count() == 4


def test_poppaping_key_connect_disconnect(client, db, seeded):
    user = seeded["user"]
    _login(client, db, user)
    client.post("/account/poppaping", data={"api_key": "pp_test123"})
    db.refresh(user)
    assert user.poppaping_api_key == "pp_test123"
    # an empty re-submit must NOT clear the key; explicit disconnect does
    client.post("/account/poppaping", data={"api_key": ""})
    db.refresh(user)
    assert user.poppaping_api_key == "pp_test123"
    client.post("/account/poppaping", data={"disconnect": "1"})
    db.refresh(user)
    assert user.poppaping_api_key is None


def _server(db, seeded, image=None, mappings=()):
    i = Instance(
        user_id=seeded["user"].id,
        plan_id=seeded["plan"].id,
        image_id=(image or seeded["image"]).id,
        label="mon1",
        hostname="mon1.warpyard.test",
        status="running",
        vmid=94001,
    )
    db.add(i)
    db.flush()
    for proto, pub, target in mappings:
        db.add(EdgeMapping(instance_id=i.id, protocol=proto, public_port=pub, target_port=target))
    db.commit()
    return i


def test_monitor_create_http_and_delete(client, db, seeded, monkeypatch):
    user = seeded["user"]
    user.poppaping_api_key = "pp_k"
    db.commit()
    calls = {}

    def fake_create(api_key, name, **spec):
        calls.update(spec, name=name, api_key=api_key)
        return "mon-uuid-1"

    monkeypatch.setattr(poppaping, "find_monitor", lambda k, n: None)
    monkeypatch.setattr(poppaping, "ensure_email_channel", lambda k, e: "chan-1")
    monkeypatch.setattr(poppaping, "create_monitor", fake_create)
    monkeypatch.setattr(poppaping, "delete_monitor", lambda k, m: calls.update(deleted=m))
    i = _server(db, seeded)
    _login(client, db, user)
    client.post(f"/servers/{i.id}/monitor")
    db.refresh(i)
    assert i.poppaping_monitor_id == "mon-uuid-1"
    assert calls["type_"] == "http" and calls["url"] == "https://mon1.warpyard.test"
    assert calls["alert_channel_ids"] == ["chan-1"]  # owner's email attached for alerting
    page = client.get(f"/servers/{i.id}").text
    assert "Monitored by PoppaPing" in page
    client.post(f"/servers/{i.id}/monitor/delete")
    db.refresh(i)
    assert i.poppaping_monitor_id is None and calls["deleted"] == "mon-uuid-1"


def test_monitor_adopts_existing_by_name(client, db, seeded, monkeypatch):
    user = seeded["user"]
    user.poppaping_api_key = "pp_k"
    db.commit()
    monkeypatch.setattr(poppaping, "find_monitor", lambda k, n: "already-there")
    monkeypatch.setattr(poppaping, "create_monitor", lambda *a, **kw: (_ for _ in ()).throw(AssertionError))
    i = _server(db, seeded)
    _login(client, db, user)
    client.post(f"/servers/{i.id}/monitor")
    db.refresh(i)
    assert i.poppaping_monitor_id == "already-there"


def test_monitor_enable_provisions_account(client, db, seeded, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "WARPYARD_POPPAPING_PARTNER_SECRET", "shh", raising=False)
    monkeypatch.setattr(poppaping, "provision_account", lambda secret, email: "pp_live_newkey")
    monkeypatch.setattr(poppaping, "find_monitor", lambda k, n: None)
    monkeypatch.setattr(poppaping, "ensure_email_channel", lambda k, e: "chan-9")
    monkeypatch.setattr(poppaping, "create_monitor", lambda k, n, **s: "mon-9")
    user = seeded["user"]
    i = _server(db, seeded)
    _login(client, db, user)
    client.post(f"/servers/{i.id}/monitor/enable")
    db.refresh(user)
    db.refresh(i)
    assert user.poppaping_api_key == "pp_live_newkey"
    assert i.poppaping_monitor_id == "mon-9"


def test_monitor_enable_existing_account_no_key(client, db, seeded, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "WARPYARD_POPPAPING_PARTNER_SECRET", "shh", raising=False)
    monkeypatch.setattr(poppaping, "provision_account", lambda secret, email: None)  # account exists
    user = seeded["user"]
    i = _server(db, seeded)
    _login(client, db, user)
    r = client.post(f"/servers/{i.id}/monitor/enable", follow_redirects=False)
    assert "merr=" in r.headers["location"]
    db.refresh(user)
    assert user.poppaping_api_key is None


def test_monitor_create_game_uses_a2s(client, db, seeded, monkeypatch):
    user = seeded["user"]
    user.poppaping_api_key = "pp_k"
    db.commit()
    cs = Image(
        slug="cs",
        name="Counter-Strike 1.6",
        distro="ubuntu",
        version="24.04",
        template_vmid=9014,
        category="game",
        lgsm_game="csserver",
        ports="udp:27015",
    )
    db.add(cs)
    db.flush()
    calls = {}
    monkeypatch.setattr(poppaping, "find_monitor", lambda k, n: None)
    monkeypatch.setattr(poppaping, "ensure_email_channel", lambda k, e: None)
    monkeypatch.setattr(poppaping, "create_monitor", lambda k, n, **s: calls.update(s) or "mon-2")
    i = _server(db, seeded, image=cs, mappings=[("udp", 30300, 27015), ("tcp", 2299, 22)])
    _login(client, db, user)
    client.post(f"/servers/{i.id}/monitor")
    assert calls["type_"] == "game" and calls["host"] == "mon1.warpyard.test" and calls["port"] == 30300


def test_monitor_error_surfaces(client, db, seeded, monkeypatch):
    user = seeded["user"]
    user.poppaping_api_key = "pp_k"
    db.commit()

    def boom(*a, **k):
        raise poppaping.PoppaPingError("PoppaPing rejected the API key — check it on your Account page.")

    monkeypatch.setattr(poppaping, "find_monitor", lambda k, n: None)
    monkeypatch.setattr(poppaping, "ensure_email_channel", lambda k, e: None)
    monkeypatch.setattr(poppaping, "create_monitor", boom)
    i = _server(db, seeded)
    _login(client, db, user)
    r = client.post(f"/servers/{i.id}/monitor", follow_redirects=False)
    assert "merr=" in r.headers["location"]
    db.refresh(i)
    assert i.poppaping_monitor_id is None
