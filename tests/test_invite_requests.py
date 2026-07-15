"""Public request-an-invite webform + admin approve/dismiss flow."""

from app import mailer, security
from app.models import Invite, InviteRequest, User


def _login(client, db, user):
    user.password_hash = security.hash_password("pw12345678")
    db.commit()
    r = client.post("/login", data={"email": user.email, "password": "pw12345678"})
    assert r.status_code in (200, 303)


def test_form_is_public(client):
    r = client.get("/request-invite")
    assert r.status_code == 200 and "Request an invite" in r.text


def test_submit_creates_request_and_notifies_admins(client, db, seeded, monkeypatch):
    admin = User(email="boss@example.com", is_admin=True)
    sysadmin = User(email="dev@warpyard.test", is_admin=True)  # system inbox: skipped
    db.add_all([admin, sysadmin])
    db.commit()
    notified = []
    monkeypatch.setattr(mailer, "send_invite_request_notice", lambda to, who, msg: notified.append(to) or True)
    r = client.post("/request-invite", data={"email": "New@Friend.com", "message": "hi im sam"}, follow_redirects=False)
    assert r.status_code == 303 and "sent=1" in r.headers["location"]
    req = db.query(InviteRequest).one()
    assert req.email == "new@friend.com" and req.message == "hi im sam" and req.status == "pending"
    assert notified == ["boss@example.com"]
    # duplicate pending submit: quiet no-op, no second row or notice
    client.post("/request-invite", data={"email": "new@friend.com"})
    assert db.query(InviteRequest).count() == 1 and len(notified) == 1


def test_honeypot_and_bad_email(client, db):
    r = client.post("/request-invite", data={"email": "bot@spam.com", "website": "http://spam"}, follow_redirects=False)
    assert r.status_code == 303  # pretend success
    assert db.query(InviteRequest).count() == 0
    assert client.post("/request-invite", data={"email": "not-an-email"}).status_code == 422
    assert db.query(InviteRequest).count() == 0


def test_admin_approve_and_dismiss(client, db, seeded, monkeypatch):
    user = seeded["user"]
    user.is_admin = True
    db.add_all([InviteRequest(email="a@x.com"), InviteRequest(email="b@x.com")])
    db.commit()
    sent = []
    monkeypatch.setattr(mailer, "send_invite", lambda to, url, note=None: sent.append(to) or True)
    _login(client, db, user)
    reqs = db.query(InviteRequest).order_by(InviteRequest.id).all()
    page = client.get("/invites").text
    assert "a@x.com" in page and "Send invite" in page
    client.post(f"/invites/requests/{reqs[0].id}/approve")
    db.refresh(reqs[0])
    assert reqs[0].status == "invited" and sent == ["a@x.com"]
    inv = db.query(Invite).filter_by(email="a@x.com").one()
    assert inv.redeemed_by is None
    client.post(f"/invites/requests/{reqs[1].id}/dismiss")
    db.refresh(reqs[1])
    assert reqs[1].status == "dismissed"


def test_approve_requires_admin(client, db, seeded):
    db.add(InviteRequest(email="c@x.com"))
    db.commit()
    _login(client, db, seeded["user"])  # not an admin
    req = db.query(InviteRequest).one()
    assert client.post(f"/invites/requests/{req.id}/approve").status_code == 403
    db.refresh(req)
    assert req.status == "pending"
