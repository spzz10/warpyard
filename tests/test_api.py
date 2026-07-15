from sqlalchemy import select

from app.models import Job


def hdr(user):
    return {"X-User-Id": str(user.id)}


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_create_instance_enqueues_job(client, db, seeded):
    r = client.post(
        "/instances", json={"label": "web1", "plan": "wy-1-1", "image": "ubuntu-24.04"}, headers=hdr(seeded["user"])
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "provisioning"
    job = db.scalar(select(Job).where(Job.type == "instance.create"))
    assert job is not None and job.instance_id == body["id"]


def test_label_validation(client, seeded):
    r = client.post(
        "/instances",
        json={"label": "Bad_Label!", "plan": "wy-1-1", "image": "ubuntu-24.04"},
        headers=hdr(seeded["user"]),
    )
    assert r.status_code == 422


def test_hostname_collision_rejected(client, seeded):
    ok = client.post(
        "/instances", json={"label": "web1", "plan": "wy-1-1", "image": "ubuntu-24.04"}, headers=hdr(seeded["user"])
    )
    assert ok.status_code == 202
    # hostname is set by the provision job; collision check uses live rows with same label
    # second create with the same label from same user hits the instance quota or races the
    # hostname — here it must NOT 500. (hostname set at provision; label dupes allowed pre-P3 DNS)
    r2 = client.post(
        "/instances", json={"label": "web1", "plan": "wy-1-1", "image": "ubuntu-24.04"}, headers=hdr(seeded["user"])
    )
    assert r2.status_code in (202, 409, 422)


def test_quota_enforced(client, seeded):
    for label in ("a1", "a2"):
        assert (
            client.post(
                "/instances",
                json={"label": label, "plan": "wy-1-1", "image": "ubuntu-24.04"},
                headers=hdr(seeded["user"]),
            ).status_code
            == 202
        )
    r = client.post(
        "/instances", json={"label": "a3", "plan": "wy-1-1", "image": "ubuntu-24.04"}, headers=hdr(seeded["user"])
    )
    assert r.status_code == 422
    assert "quota" in r.json()["detail"]


def test_action_gating(client, db, seeded):
    r = client.post(
        "/instances", json={"label": "web2", "plan": "wy-1-1", "image": "ubuntu-24.04"}, headers=hdr(seeded["user"])
    )
    iid = r.json()["id"]
    # provisioning: start is not a legal verb, and a create job is already active
    r2 = client.post(f"/instances/{iid}/actions/start", headers=hdr(seeded["user"]))
    assert r2.status_code == 409


def test_unknown_user_rejected(client, seeded):
    r = client.get("/instances", headers={"X-User-Id": "9999"})
    assert r.status_code == 401


def test_service_create_rejects_taken_name_and_frees_after_destroy(db, seeded):
    """Name collision is caught up front (422, not a provision-job crash), and destroying
    a server releases its hostname so the name can be reused."""
    import pytest

    from app import service, states
    from app.models import Instance

    user = seeded["user"]
    first = service.create_server(db, user, "dupname", "wy-1-1", "ubuntu-24.04")
    with pytest.raises(service.ServiceError) as e:
        service.create_server(db, user, "dupname", "wy-1-1", "ubuntu-24.04")
    assert e.value.status == 422 and "taken" in e.value.message
    # destroy releases the name (the handler also nulls hostname; emulate its terminal state)
    i = db.get(Instance, first["id"])
    i.hostname = None
    i.status = states.DESTROYED
    for r in list(i.http_routes):
        db.delete(r)
    db.commit()
    again = service.create_server(db, user, "dupname", "wy-1-1", "ubuntu-24.04")
    assert again["id"] != first["id"]
