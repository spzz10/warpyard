from app import security
from app.models import Invite, User


def test_bcrypt_roundtrip():
    h = security.hash_password("hunter2")
    ok, upgrade = security.verify_password("hunter2", h)
    assert ok and upgrade is None  # current bcrypt, no re-hash needed
    assert security.verify_password("wrong", h) == (False, None)
    assert security.verify_password("hunter2", None) == (False, None)


def _admin(db):
    u = User(email="admin@warpyard.test", password_hash=security.hash_password("warpyard"), is_admin=True)
    db.add(u)
    db.commit()
    return u


def test_invite_mint_redeem_and_single_use(client, db):
    _admin(db)
    client.post("/login", data={"email": "admin@warpyard.test", "password": "warpyard"})
    assert client.post("/invites", data={"note": "for sam", "email": ""}).status_code in (200, 303)
    token = db.scalar(__import__("sqlalchemy").select(Invite.token))
    assert token

    fresh = client.__class__(client.app)  # separate cookie jar = anonymous
    r = fresh.post(f"/join/{token}", data={"email": "sam@example.com", "password": "harbor12345"})
    assert r.status_code in (200, 303)
    assert db.scalar(__import__("sqlalchemy").select(User).where(User.email == "sam@example.com"))
    # reuse rejected
    r2 = client.__class__(client.app).post(
        f"/join/{token}", data={"email": "eve@example.com", "password": "harbor12345"}
    )
    assert r2.status_code == 422


def test_admin_gate(client, db):
    from app.models import User as U

    db.add(U(email="plain@warpyard.test", password_hash=security.hash_password("warpyard"), is_admin=False))
    db.commit()
    client.post("/login", data={"email": "plain@warpyard.test", "password": "warpyard"})
    assert client.get("/invites").status_code == 403
