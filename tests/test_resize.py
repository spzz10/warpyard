from sqlalchemy import select

from app.models import Instance, Job


def hdr(user):
    return {"X-User-Id": str(user.id)}


def _running(db, seeded, plan_key="plan"):
    i = Instance(user_id=seeded["user"].id, plan_id=seeded[plan_key].id, image_id=seeded["image"].id, label="s")
    i.status = "running"
    db.add(i)
    db.commit()
    return i


def _login(client, seeded):
    client.post("/login", data={"email": seeded["user"].email, "password": "warpyard"})


def test_resize_form_lists_only_bigger_plans(client, db, seeded):
    from app import security

    seeded["user"].password_hash = security.hash_password("warpyard")
    db.commit()
    inst = _running(db, seeded)  # on the small plan
    _login(client, seeded)
    r = client.get(f"/servers/{inst.id}/resize")
    assert r.status_code == 200
    assert seeded["big_plan"].name in r.text  # bigger plan offered
    assert "can only grow" in r.text


def test_resize_enqueues_job(client, db, seeded):
    from app import security

    seeded["user"].password_hash = security.hash_password("warpyard")
    db.commit()
    inst = _running(db, seeded)
    _login(client, seeded)
    r = client.post(f"/servers/{inst.id}/resize", data={"plan": seeded["big_plan"].slug})
    assert r.status_code in (200, 303)
    job = db.scalar(select(Job).where(Job.type == "instance.resize"))
    assert job is not None and job.payload["plan_id"] == seeded["big_plan"].id


def test_resize_rejects_shrink(client, db, seeded):
    from app import security

    seeded["user"].password_hash = security.hash_password("warpyard")
    db.commit()
    inst = _running(db, seeded, plan_key="big_plan")  # on the big plan
    _login(client, seeded)
    r = client.post(f"/servers/{inst.id}/resize", data={"plan": seeded["plan"].slug})  # smaller disk
    assert r.status_code == 422
    assert "shrink" in r.text
    assert db.scalar(select(Job).where(Job.type == "instance.resize")) is None
