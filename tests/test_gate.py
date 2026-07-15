"""Platform-auth gate: token round-trip, the /edge/gate forward-auth decision,
the /gate login handoff, /__wygate cookie set, and the server-page toggle."""

from app import gate, security
from app.models import Instance


def _instance(db, seeded, **kw):
    u, p, img = seeded["user"], seeded["plan"], seeded["image"]
    i = Instance(
        user_id=u.id, plan_id=p.id, image_id=img.id, label="priv", hostname="priv.warpyard.test", status="running", **kw
    )
    db.add(i)
    db.commit()
    return i


def test_token_roundtrip_and_host_binding():
    t = gate._mint("priv.warpyard.test", 7)
    assert gate._verify(t, "priv.warpyard.test") == 7
    assert gate._verify(t, "other.warpyard.test") is None  # host-bound
    assert gate._verify("garbage", "priv.warpyard.test") is None
    assert gate._verify(None, "priv.warpyard.test") is None


def test_safe_target_rejects_offsite_and_http():
    assert gate._safe_target("https://x.warpyard.test/p") == "x.warpyard.test"
    assert gate._safe_target("https://evil.com/p") is None
    assert gate._safe_target("http://x.warpyard.test/") is None  # must be https
    assert gate._safe_target("notaurl") is None


def test_edge_gate_allows_valid_cookie_denies_otherwise(client):
    good = gate._mint("priv.warpyard.test", 1)
    r = client.get("/edge/gate", headers={"X-Wy-Host": "priv.warpyard.test"}, cookies={gate.GATE_COOKIE: good})
    assert r.status_code == 200
    # no cookie -> bounce to the platform login handoff
    r = client.get("/edge/gate", headers={"X-Wy-Host": "priv.warpyard.test", "X-Wy-Uri": "/x"}, follow_redirects=False)
    assert r.status_code == 302 and "/gate?next=" in r.headers["location"]


def test_gate_requires_login_then_hands_off(client, db, seeded):
    nxt = "https://priv.warpyard.test/dash"
    # logged out -> /login (carrying the return path)
    r = client.get("/gate", params={"next": nxt}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login?next=")
    # logged in member -> handoff to the target host's /__wygate with a token
    seeded["user"].password_hash = security.hash_password("pw")
    db.commit()
    client.post("/login", data={"email": seeded["user"].email, "password": "pw"})
    r = client.get("/gate", params={"next": nxt}, follow_redirects=False)
    assert r.status_code == 303
    loc = r.headers["location"]
    assert loc.startswith("https://priv.warpyard.test/__wygate?token=")


def test_gate_rejects_offsite_next(client):
    r = client.get("/gate", params={"next": "https://evil.com/x"}, follow_redirects=False)
    assert r.status_code == 303 and "evil.com" not in r.headers["location"]


def test_wygate_sets_cookie_for_valid_token(client):
    t = gate._mint("priv.warpyard.test", 3)
    r = client.get("/__wygate", params={"token": t, "next": "https://priv.warpyard.test/x"}, follow_redirects=False)
    assert r.status_code == 303
    assert gate.GATE_COOKIE in r.cookies
    # a token minted for a different host must not set a cookie for this next
    bad = gate._mint("elsewhere.warpyard.test", 3)
    r = client.get("/__wygate", params={"token": bad, "next": "https://priv.warpyard.test/x"}, follow_redirects=False)
    assert gate.GATE_COOKIE not in r.cookies


def test_edge_routes_marks_gated_and_forces_termination(client, db, seeded):
    from app.models import HttpRoute, IpAddress

    i = _instance(db, seeded, tls_passthrough=True, gated=True)
    db.add(IpAddress(address="10.66.0.77", gateway="10.66.0.1", instance_id=i.id))
    db.add(HttpRoute(instance_id=i.id, hostname="priv.warpyard.test", target_port=80, status="active"))
    db.commit()
    # emitted to the edge over the sync token
    from app.config import get_settings

    tok = get_settings().EDGE_SYNC_TOKEN or ""
    if not tok:
        import os

        os.environ["EDGE_SYNC_TOKEN"] = "t"
        get_settings.cache_clear()
        tok = "t"
    r = client.get("/edge/routes", headers={"Authorization": f"Bearer {tok}"})
    row = next(h for h in r.json()["http"] if h["hostname"] == "priv.warpyard.test")
    assert row["gated"] is True
    assert row["passthrough"] is False  # gated overrides passthrough -> edge terminates


def test_gate_toggle_route(client, db, seeded):
    seeded["user"].password_hash = security.hash_password("pw")
    db.commit()
    client.post("/login", data={"email": seeded["user"].email, "password": "pw"})
    i = _instance(db, seeded)
    client.post(f"/servers/{i.id}/gate", data={"enabled": "on"}, follow_redirects=False)
    db.refresh(i)
    assert i.gated is True
    client.post(f"/servers/{i.id}/gate", data={"enabled": ""}, follow_redirects=False)
    db.refresh(i)
    assert i.gated is False
