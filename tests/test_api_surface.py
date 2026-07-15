"""The service layer behind the newer REST/MCP surface: snapshots, restart schedule,
share board, monitoring, metrics and the private-network toggle — plus the REST routes."""

import pytest
from sqlalchemy import select

import app.proxmox as proxmox_mod
from app import poppaping, service
from app.api_auth import generate_key
from app.models import ApiKey, Instance, Job


def _running(db, seeded, **kw):
    i = Instance(
        user_id=seeded["user"].id,
        plan_id=seeded["plan"].id,
        image_id=seeded["image"].id,
        label="s",
        vmid=90001,
        **kw,
    )
    i.status = "running"
    db.add(i)
    db.commit()
    return i


class FakePx:
    """Stands in for ProxmoxClient wherever snapshots talk to PVE."""

    snaps = [{"name": "snapbefore-mods", "description": "via Warpyard"}, {"name": "current"}]
    taken: list = []
    deleted: list = []

    def __init__(self, role="config"):
        self.role = role

    def list_snapshots(self, vmid):
        return list(self.snaps)

    def snapshot(self, vmid, name, description=""):
        self.taken.append((vmid, name))
        return "UPID"

    def delete_snapshot(self, vmid, name):
        self.deleted.append((vmid, name))
        return "UPID"


@pytest.fixture()
def px(monkeypatch):
    FakePx.taken, FakePx.deleted = [], []
    monkeypatch.setattr(proxmox_mod, "ProxmoxClient", FakePx)
    return FakePx


# ── snapshots ──
def test_snapshot_list_and_create(db, seeded, px):
    inst = _running(db, seeded)
    snaps = service.list_snapshots(db, seeded["user"], inst.id)
    assert snaps == [{"name": "snapbefore-mods", "description": "via Warpyard"}]  # 'current' hidden
    out = service.create_snapshot(db, seeded["user"], inst.id, "Before Mods!")
    assert out["snapshot"].startswith("snap") and "!" not in out["snapshot"]
    assert px.taken == [(90001, out["snapshot"])]


def test_snapshot_delete_checks_ownership_of_name(db, seeded, px):
    inst = _running(db, seeded)
    with pytest.raises(service.ServiceError) as e:
        service.delete_snapshot(db, seeded["user"], inst.id, "not-a-snapshot")
    assert e.value.status == 404
    service.delete_snapshot(db, seeded["user"], inst.id, "snapbefore-mods")
    assert px.deleted == [(90001, "snapbefore-mods")]


def test_snapshot_rollback_enqueues(db, seeded, px):
    inst = _running(db, seeded)
    out = service.rollback_snapshot(db, seeded["user"], inst.id, "snapbefore-mods")
    assert out["status"] == "accepted"
    job = db.scalar(select(Job).where(Job.type == "instance.rollback"))
    assert job is not None and job.payload == {"snapshot": "snapbefore-mods"}
    with pytest.raises(service.ServiceError):
        service.rollback_snapshot(db, seeded["user"], inst.id, "nope")


def test_snapshots_unprovisioned(db, seeded, px):
    inst = _running(db, seeded)
    inst.vmid = None
    db.commit()
    assert service.list_snapshots(db, seeded["user"], inst.id) == []
    with pytest.raises(service.ServiceError) as e:
        service.create_snapshot(db, seeded["user"], inst.id)
    assert e.value.status == 409


# ── restart schedule / share ──
def test_restart_schedule_defaults_and_validates(db, seeded):
    inst = _running(db, seeded)
    out = service.set_restart_schedule(db, seeded["user"], inst.id, True)
    assert out["nightly_restart"] == {"enabled": True, "at": "09:00"}
    out = service.set_restart_schedule(db, seeded["user"], inst.id, True, "04:30")
    assert inst.restart_at == "04:30"
    with pytest.raises(service.ServiceError) as e:
        service.set_restart_schedule(db, seeded["user"], inst.id, True, "25:99")
    assert e.value.status == 422
    service.set_restart_schedule(db, seeded["user"], inst.id, False)
    assert inst.restart_enabled is False and inst.restart_at == "04:30"  # time kept for re-enable


def test_share_toggle_and_note_cap(db, seeded):
    inst = _running(db, seeded)
    out = service.set_share(db, seeded["user"], inst.id, True, "x" * 200)
    assert out["shared"]["enabled"] is True and len(out["shared"]["note"]) == 140
    service.set_share(db, seeded["user"], inst.id, False, "")
    assert inst.shared is False and inst.shared_note is None


# ── monitoring ──
def test_enable_monitoring_requires_key_or_partner(db, seeded):
    inst = _running(db, seeded)
    with pytest.raises(service.ServiceError) as e:  # no key, no partner secret
        service.enable_monitoring(db, seeded["user"], inst.id)
    assert e.value.status == 422


def test_enable_and_disable_monitoring(db, seeded, monkeypatch):
    inst = _running(db, seeded)
    seeded["user"].poppaping_api_key = "pp_live_k"
    db.commit()
    monkeypatch.setattr(poppaping, "find_monitor", lambda k, n: None)
    monkeypatch.setattr(poppaping, "ensure_email_channel", lambda k, e: "chan-1")
    monkeypatch.setattr(poppaping, "create_monitor", lambda k, n, **s: "mon-1")
    out = service.enable_monitoring(db, seeded["user"], inst.id)
    assert out["monitoring"] is True and inst.poppaping_monitor_id == "mon-1"
    assert service.server_json(inst)["monitoring"] is True
    deleted = []
    monkeypatch.setattr(poppaping, "delete_monitor", lambda k, m: deleted.append(m))
    service.disable_monitoring(db, seeded["user"], inst.id)
    assert deleted == ["mon-1"] and inst.poppaping_monitor_id is None


def test_monitoring_data_guards(db, seeded, monkeypatch):
    inst = _running(db, seeded)
    with pytest.raises(service.ServiceError) as e:
        service.monitoring_data(db, seeded["user"], inst.id)
    assert e.value.status == 404  # not monitored
    seeded["user"].poppaping_api_key = "pp_live_k"
    inst.poppaping_monitor_id = "mon-1"
    db.commit()
    with pytest.raises(service.ServiceError) as e:
        service.monitoring_data(db, seeded["user"], inst.id, "1y")
    assert e.value.status == 422
    monkeypatch.setattr(poppaping, "monitor_history", lambda k, m, p: {"uptime": 99.9, "points": []})
    assert service.monitoring_data(db, seeded["user"], inst.id, "7d")["uptime"] == 99.9


# ── metrics ──
def test_server_metrics(db, seeded, monkeypatch):
    inst = _running(db, seeded)

    def fake_series(vmid, tf):
        if tf not in ("hour", "day", "week"):  # mirror the real validation
            raise ValueError("timeframe must be one of hour, day, week")
        return {"timeframe": tf, "points": [], "now": None}

    monkeypatch.setattr(service.vmstats, "series", fake_series)
    assert service.server_metrics(db, seeded["user"], inst.id, "day")["timeframe"] == "day"
    with pytest.raises(service.ServiceError) as e:
        service.server_metrics(db, seeded["user"], inst.id, "decade")
    assert e.value.status == 422


# ── account settings ──
def test_set_private_network_syncs_firewalls(db, seeded, monkeypatch):
    import app.jobs.handlers as handlers

    synced = []
    monkeypatch.setattr(handlers, "sync_owner_firewalls", lambda d, u: synced.append(u.id))
    out = service.set_private_network(db, seeded["user"], True)
    assert out["private_network"] is True and synced == [seeded["user"].id]
    assert service.account_info(db, seeded["user"])["private_network"] is True


# ── the new fields on existing payloads ──
def test_server_json_settings_fields(db, seeded):
    inst = _running(db, seeded, restart_enabled=True, restart_at="05:00", shared=True, shared_note="hi")
    j = service.server_json(inst)
    assert j["nightly_restart"] == {"enabled": True, "at": "05:00"}
    assert j["shared"] == {"enabled": True, "note": "hi"}
    assert j["monitoring"] is False
    assert j["tls_passthrough"] is False and j["encrypted"] is False


def test_list_images_has_blurb(db, seeded):
    seeded["image"].blurb = "The default choice"
    db.commit()
    assert service.list_images(db)[0]["blurb"] == "The default choice"


# ── REST wiring (one auth'd round-trip through each new route family) ──
@pytest.fixture()
def api_hdr(db, seeded):
    full, prefix, key_hash = generate_key()
    db.add(ApiKey(user_id=seeded["user"].id, name="t", prefix=prefix, key_hash=key_hash))
    db.commit()
    return {"Authorization": f"Bearer {full}"}


def test_rest_snapshots_and_settings(client, db, seeded, px, api_hdr):
    inst = _running(db, seeded)
    r = client.get(f"/api/v1/servers/{inst.id}/snapshots", headers=api_hdr)
    assert r.status_code == 200 and r.json()[0]["name"] == "snapbefore-mods"
    r = client.post(f"/api/v1/servers/{inst.id}/snapshots", json={"name": "pre"}, headers=api_hdr)
    assert r.status_code == 201
    r = client.put(f"/api/v1/servers/{inst.id}/restart-schedule", json={"enabled": True}, headers=api_hdr)
    assert r.status_code == 200 and r.json()["nightly_restart"]["at"] == "09:00"
    r = client.put(f"/api/v1/servers/{inst.id}/share", json={"enabled": True, "note": "demo"}, headers=api_hdr)
    assert r.status_code == 200 and r.json()["shared"]["enabled"] is True
    r = client.get(f"/api/v1/servers/{inst.id}/monitoring", headers=api_hdr)
    assert r.status_code == 404  # not monitored yet — clean error, not a 500
    assert client.get("/api/v1/servers/999/snapshots", headers=api_hdr).status_code == 404
