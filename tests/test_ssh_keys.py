"""Account SSH-key management (service layer) used by the REST API and MCP."""

import pytest

from app import service

ED = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHYhjB2QPrh2unir8O8cytTh4a5Fud4Hh5ElrgJrYxEr test@warpyard"


def test_add_list_remove(db, seeded):
    u = seeded["user"]
    k = service.add_ssh_key(db, u, "laptop", ED)
    assert k["type"] == "ssh-ed25519" and k["name"] == "laptop"
    keys = service.list_ssh_keys(db, u)
    assert len(keys) == 1 and keys[0]["id"] == k["id"]
    service.remove_ssh_key(db, u, k["id"])
    assert service.list_ssh_keys(db, u) == []


def test_reject_garbage_key(db, seeded):
    with pytest.raises(service.ServiceError) as e:
        service.add_ssh_key(db, seeded["user"], "x", "not a key")
    assert e.value.status == 422


def test_reject_duplicate_key(db, seeded):
    service.add_ssh_key(db, seeded["user"], "a", ED)
    with pytest.raises(service.ServiceError) as e:
        service.add_ssh_key(db, seeded["user"], "b", ED)
    assert e.value.status == 409


def test_remove_other_users_key_404(db, seeded):
    from app.models import User

    k = service.add_ssh_key(db, seeded["user"], "a", ED)
    other = User(email="o@example.com", max_instances=1, max_vcpus=1, max_disk_gb=10)
    db.add(other)
    db.commit()
    with pytest.raises(service.ServiceError) as e:
        service.remove_ssh_key(db, other, k["id"])
    assert e.value.status == 404
