"""Custom-domain rules (service layer) + edge exposure. DNS resolution is monkeypatched so
tests are hermetic — the real check is app.dns_check.resolves_to_edge."""

import os

import pytest

from app import service
from app.models import HttpRoute, Instance, IpAddress


def _instance(db, seeded, label="web", ip="10.66.0.150"):
    inst = Instance(user_id=seeded["user"].id, plan_id=seeded["plan"].id, image_id=seeded["image"].id, label=label)
    inst.status = "running"
    inst.hostname = f"{label}.warpyard.test"
    db.add(inst)
    db.flush()
    db.add(IpAddress(address=ip, gateway="10.66.0.1", instance_id=inst.id))
    db.add(HttpRoute(instance_id=inst.id, hostname=inst.hostname, target_port=80))  # system route (default active)
    db.commit()
    return inst


@pytest.fixture()
def no_dns(monkeypatch):
    monkeypatch.setattr(service.dns_check, "resolves_to_edge", lambda h: False)


@pytest.fixture()
def yes_dns(monkeypatch):
    monkeypatch.setattr(service.dns_check, "resolves_to_edge", lambda h: True)


def test_add_domain_active_when_dns_points_at_edge(db, seeded, yes_dns):
    inst = _instance(db, seeded)
    out = service.add_domain(db, seeded["user"], inst.id, "App.Example.COM")
    assert out["hostname"] == "app.example.com"  # normalized
    assert out["status"] == "active"
    assert "dns_record" not in out


def test_add_domain_pending_when_dns_absent(db, seeded, no_dns):
    inst = _instance(db, seeded)
    out = service.add_domain(db, seeded["user"], inst.id, "shop.example.com")
    assert out["status"] == "pending"
    assert out["dns_record"] == {"type": "CNAME", "name": "shop.example.com", "value": "edge.warpyard.test"}


def test_apex_domain_wants_a_record(db, seeded, no_dns):
    inst = _instance(db, seeded)
    out = service.add_domain(db, seeded["user"], inst.id, "example.com")
    assert out["dns_record"]["type"] == "A" and out["dns_record"]["value"] == "203.0.113.10"


def test_reject_warpyard_names(db, seeded, yes_dns):
    inst = _instance(db, seeded)
    for bad in ("evil.warpyard.test", "warpyard.test"):
        with pytest.raises(service.ServiceError) as e:
            service.add_domain(db, seeded["user"], inst.id, bad)
        assert e.value.status == 422


def test_reject_wildcards_and_junk(db, seeded, yes_dns):
    inst = _instance(db, seeded)
    for bad in ("*.example.com", "not a domain", "nodot", "-bad.example.com"):
        with pytest.raises(service.ServiceError):
            service.add_domain(db, seeded["user"], inst.id, bad)


def test_global_uniqueness_across_users(db, seeded, yes_dns):
    from app.models import User

    inst = _instance(db, seeded)
    service.add_domain(db, seeded["user"], inst.id, "taken.example.com")
    other = User(email="other@example.com", max_instances=2, max_vcpus=4, max_disk_gb=80)
    db.add(other)
    db.flush()
    inst2 = Instance(user_id=other.id, plan_id=seeded["plan"].id, image_id=seeded["image"].id, label="w2")
    inst2.status = "running"
    db.add(inst2)
    db.flush()
    db.add(IpAddress(address="10.66.0.151", gateway="10.66.0.1", instance_id=inst2.id))
    db.commit()
    with pytest.raises(service.ServiceError) as e:
        service.add_domain(db, other, inst2.id, "TAKEN.example.com")
    assert e.value.status == 409


def test_cap_per_instance(db, seeded, yes_dns):
    inst = _instance(db, seeded)
    for n in range(5):
        service.add_domain(db, seeded["user"], inst.id, f"d{n}.example.com")
    with pytest.raises(service.ServiceError) as e:
        service.add_domain(db, seeded["user"], inst.id, "d5.example.com")
    assert e.value.status == 422


def test_pending_flips_active_on_recheck(db, seeded, monkeypatch):
    inst = _instance(db, seeded)
    monkeypatch.setattr(service.dns_check, "resolves_to_edge", lambda h: False)
    service.add_domain(db, seeded["user"], inst.id, "later.example.com")
    assert service.list_domains(db, seeded["user"], inst.id)[0]["status"] == "pending"
    # DNS now points at us
    monkeypatch.setattr(service.dns_check, "resolves_to_edge", lambda h: True)
    assert service.recheck_pending_domains(db) == 1
    assert service.list_domains(db, seeded["user"], inst.id)[0]["status"] == "active"


def test_remove_domain(db, seeded, yes_dns):
    inst = _instance(db, seeded)
    service.add_domain(db, seeded["user"], inst.id, "gone.example.com")
    service.remove_domain(db, seeded["user"], inst.id, "gone.example.com")
    assert service.list_domains(db, seeded["user"], inst.id) == []
    with pytest.raises(service.ServiceError):
        service.remove_domain(db, seeded["user"], inst.id, "gone.example.com")


def test_edge_routes_only_exposes_active(client, db, seeded, monkeypatch):
    os.environ["EDGE_SYNC_TOKEN"] = "t0ken"
    from app.config import get_settings

    get_settings.cache_clear()
    inst = _instance(db, seeded)  # system route active
    monkeypatch.setattr(service.dns_check, "resolves_to_edge", lambda h: True)
    service.add_domain(db, seeded["user"], inst.id, "live.example.com")  # active
    monkeypatch.setattr(service.dns_check, "resolves_to_edge", lambda h: False)
    service.add_domain(db, seeded["user"], inst.id, "waiting.example.com")  # pending

    r = client.get("/edge/routes", headers={"Authorization": "Bearer t0ken"})
    assert r.status_code == 200
    hosts = {row["hostname"] for row in r.json()["http"]}
    assert "web.warpyard.test" in hosts and "live.example.com" in hosts
    assert "waiting.example.com" not in hosts
    os.environ["EDGE_SYNC_TOKEN"] = ""
    get_settings.cache_clear()
