"""Share board: members-only list of opted-in running servers across accounts,
plus the per-server share toggle and the share-by-default account flag (a concierge account)."""

from app import security, service
from app.models import Instance, User


def _login(client, db, user):
    user.password_hash = security.hash_password("pw12345678")
    db.commit()
    r = client.post("/login", data={"email": user.email, "password": "pw12345678"})
    assert r.status_code in (200, 303)


def _instance(db, seeded, owner, label, status="running", shared=False, note=None):
    i = Instance(
        user_id=owner.id,
        plan_id=seeded["plan"].id,
        image_id=seeded["image"].id,
        label=label,
        hostname=f"{label}.warpyard.test",
        status=status,
        shared=shared,
        shared_note=note,
    )
    db.add(i)
    db.commit()
    return i


def test_board_requires_login(client):
    r = client.get("/board", follow_redirects=False)
    assert r.status_code in (302, 303, 307)


def test_board_lists_shared_running_only(client, db, seeded):
    buddy = User(email="buddy@example.com")
    db.add(buddy)
    db.commit()
    _instance(db, seeded, buddy, "coolsite", shared=True, note="my blog")
    _instance(db, seeded, buddy, "notshared")
    _instance(db, seeded, buddy, "offline", status="stopped", shared=True)
    _login(client, db, seeded["user"])
    html = client.get("/board").text
    assert "coolsite" in html and "my blog" in html and "buddy" in html
    assert "notshared" not in html and "offline" not in html


def test_share_toggle(client, db, seeded):
    i = _instance(db, seeded, seeded["user"], "mine1")
    _login(client, db, seeded["user"])
    r = client.post(f"/servers/{i.id}/share", data={"enabled": "on", "note": "wip"}, follow_redirects=False)
    assert r.status_code == 303
    db.refresh(i)
    assert i.shared and i.shared_note == "wip"
    client.post(f"/servers/{i.id}/share", data={})
    db.refresh(i)
    assert not i.shared


def test_cannot_share_someone_elses_server(client, db, seeded):
    buddy = User(email="buddy2@example.com")
    db.add(buddy)
    db.commit()
    i = _instance(db, seeded, buddy, "theirs")
    _login(client, db, seeded["user"])
    client.post(f"/servers/{i.id}/share", data={"enabled": "on"})
    db.refresh(i)
    assert not i.shared


def test_share_by_default_on_create(db, seeded):
    # create_server honors user.share_by_default (the a concierge account concierge account)
    user = seeded["user"]
    user.share_by_default = True
    db.commit()
    out = service.create_server(db, user, "botsite", "wy-1-1", "ubuntu-24.04")
    assert db.get(Instance, out["id"]).shared


def test_board_comments_post_render_and_escape(client, db, seeded):
    buddy = User(email="chatty@example.com")
    db.add(buddy)
    db.commit()
    inst = _instance(db, seeded, buddy, "chatty", shared=True)
    _login(client, db, seeded["user"])
    r = client.post(f"/board/{inst.id}/comments", data={"body": "nice <b>server</b>!"})
    assert r.status_code == 200
    assert "nice &lt;b&gt;server&lt;/b&gt;!" in r.text  # Jinja autoescape holds
    assert "1 comment" in client.get("/board").text
    # empty bodies are a quiet no-op
    r = client.post(f"/board/{inst.id}/comments", data={"body": "   "})
    assert r.status_code == 200 and "1 comment" in r.text


def test_board_comments_only_on_shared(client, db, seeded):
    buddy = User(email="quiet@example.com")
    db.add(buddy)
    db.commit()
    inst = _instance(db, seeded, buddy, "hidden", shared=False)
    _login(client, db, seeded["user"])
    assert client.post(f"/board/{inst.id}/comments", data={"body": "hi"}).status_code == 404


def test_board_comment_delete_permissions(client, db, seeded):
    owner = User(email="owner@example.com")
    rando = User(email="rando@example.com")
    db.add_all([owner, rando])
    db.commit()
    inst = _instance(db, seeded, owner, "moderated", shared=True)

    _login(client, db, seeded["user"])  # commenter
    client.post(f"/board/{inst.id}/comments", data={"body": "first!"})
    cid = inst.board_comments[0].id

    _login(client, db, rando)  # unrelated member: can't delete
    assert client.post(f"/board/{inst.id}/comments/{cid}/delete").status_code == 403

    _login(client, db, owner)  # server owner: can delete anyone's comment
    r = client.post(f"/board/{inst.id}/comments/{cid}/delete")
    assert r.status_code == 200 and "0 comments" in r.text
