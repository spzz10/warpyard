import os

from app.models import HttpRoute, Instance, IpAddress


def _live_instance_with_route(db, seeded, host="app.warpyard.test", ip="10.66.0.150"):
    inst = Instance(user_id=seeded["user"].id, plan_id=seeded["plan"].id, image_id=seeded["image"].id, label="app")
    inst.status = "running"
    db.add(inst)
    db.flush()
    db.add(IpAddress(address=ip, gateway="10.66.0.1", instance_id=inst.id))
    db.add(HttpRoute(instance_id=inst.id, hostname=host, target_port=8080))
    db.commit()
    return inst


def test_edge_routes_requires_token(client, db, seeded):
    os.environ["EDGE_SYNC_TOKEN"] = ""
    from app.config import get_settings

    get_settings.cache_clear()
    _live_instance_with_route(db, seeded)
    # no token configured -> always 401
    assert client.get("/edge/routes").status_code == 401


def test_edge_routes_returns_live_routes(client, db, seeded):
    os.environ["EDGE_SYNC_TOKEN"] = "sekret"
    from app.config import get_settings

    get_settings.cache_clear()
    _live_instance_with_route(db, seeded)
    r = client.get("/edge/routes", headers={"Authorization": "Bearer sekret"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["http"] == [
        {
            "hostname": "app.warpyard.test",
            "upstream": "10.66.0.150:8080",
            "passthrough": False,
            "https_upstream": "10.66.0.150:443",
        }
    ]
    # wrong token rejected
    assert client.get("/edge/routes", headers={"Authorization": "Bearer nope"}).status_code == 401
    os.environ.pop("EDGE_SYNC_TOKEN", None)
    get_settings.cache_clear()


def test_edge_routes_marks_passthrough(client, db, seeded):
    os.environ["EDGE_SYNC_TOKEN"] = "sekret"
    from app.config import get_settings

    get_settings.cache_clear()
    inst = _live_instance_with_route(db, seeded)
    inst.tls_passthrough = True  # VM terminates its own TLS
    db.commit()
    r = client.get("/edge/routes", headers={"Authorization": "Bearer sekret"})
    entry = r.json()["http"][0]
    assert entry["passthrough"] is True
    assert entry["https_upstream"] == "10.66.0.150:443"
    os.environ.pop("EDGE_SYNC_TOKEN", None)
    get_settings.cache_clear()


def test_create_server_sets_tls_passthrough_for_web(db, seeded):
    """os/app images terminate their own TLS; game images stay edge-terminated."""
    from app import service
    from app.models import Image

    seeded["user"].max_instances = 10
    seeded["user"].max_vcpus = 100
    seeded["user"].max_disk_gb = 1000
    for slug, category in [("os-x", "os"), ("app-x", "app"), ("game-x", "game")]:
        db.add(Image(slug=slug, name=slug, distro="ubuntu", version="24.04", template_vmid=9000, category=category))
    db.commit()
    for slug, expected in [("os-x", True), ("app-x", True), ("game-x", False)]:
        out = service.create_server(db, seeded["user"], f"srv-{slug}", seeded["plan"].slug, slug)
        inst = db.get(service.Instance, out["id"])
        assert inst.tls_passthrough is expected, slug


def test_create_server_toggles(db, seeded):
    """tls_passthrough overrides the image default; encrypted is opt-in."""
    from app import service
    from app.models import Image

    seeded["user"].max_instances = 10
    seeded["user"].max_vcpus = 100
    seeded["user"].max_disk_gb = 1000
    db.add(Image(slug="webz", name="webz", distro="ubuntu", version="24.04", template_vmid=9000, category="app"))
    db.add(Image(slug="gamez", name="gamez", distro="ubuntu", version="24.04", template_vmid=9000, category="game"))
    db.commit()
    # explicit tls off on a web image + encrypted on
    a = db.get(
        service.Instance, service.create_server(db, seeded["user"], "a", seeded["plan"].slug, "webz", False, True)["id"]
    )
    assert a.tls_passthrough is False and a.encrypted is True
    # tls on for a game (override), encrypted defaults off
    b = db.get(
        service.Instance, service.create_server(db, seeded["user"], "b", seeded["plan"].slug, "gamez", True)["id"]
    )
    assert b.tls_passthrough is True and b.encrypted is False


def test_new_form_renders_toggles(client, db, seeded):
    from app import security

    seeded["user"].password_hash = security.hash_password("pw12345678")
    db.commit()
    client.post("/login", data={"email": seeded["user"].email, "password": "pw12345678"})
    r = client.get("/new")
    assert r.status_code == 200
    assert 'name="tls_e2e"' in r.text and 'name="encrypt"' in r.text
    assert "50 new certificates per week" in r.text


def test_edge_excludes_destroyed(client, db, seeded):
    os.environ["EDGE_SYNC_TOKEN"] = "sekret"
    from app.config import get_settings

    get_settings.cache_clear()
    inst = _live_instance_with_route(db, seeded)
    inst.status = "destroyed"
    db.commit()
    r = client.get("/edge/routes", headers={"Authorization": "Bearer sekret"})
    assert r.json()["http"] == []
    os.environ.pop("EDGE_SYNC_TOKEN", None)
    get_settings.cache_clear()
