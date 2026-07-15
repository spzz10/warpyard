"""Monitoring-tab chart data endpoint + PoppaPing history shaping + account tabs render."""

from app import poppaping, security
from app.models import Instance, User


def _login(client, db, user):
    user.password_hash = security.hash_password("pw12345678")
    db.commit()
    r = client.post("/login", data={"email": user.email, "password": "pw12345678"})
    assert r.status_code in (200, 303)


def _server(db, seeded, monitor_id="mon-1"):
    i = Instance(
        user_id=seeded["user"].id,
        plan_id=seeded["plan"].id,
        image_id=seeded["image"].id,
        label="mon1",
        hostname="mon1.warpyard.test",
        status="running",
        vmid=95001,
        poppaping_monitor_id=monitor_id,
    )
    db.add(i)
    db.commit()
    return i


def test_monitor_data_owner_gated(client, db, seeded, monkeypatch):
    buddy = User(email="b@x.com", poppaping_api_key="pp_k")
    db.add(buddy)
    db.commit()
    i = _server(db, seeded)
    _login(client, db, seeded["user"])  # not the owner of buddy's server, but this server is theirs
    # server has a monitor but the OWNER has no key → not_monitored
    assert client.get(f"/servers/{i.id}/monitor/data.json").status_code == 404


def test_monitor_data_shapes_history(client, db, seeded, monkeypatch):
    user = seeded["user"]
    user.poppaping_api_key = "pp_k"
    db.commit()
    i = _server(db, seeded)
    captured = {}

    def fake_hist(api_key, monitor_id, period):
        captured.update(api_key=api_key, monitor_id=monitor_id, period=period)
        return {
            "uptime_pct": 99.9,
            "avg_ms": 123.4,
            "up": 200,
            "down": 1,
            "total": 201,
            "points": [{"t": 1, "ms": 120, "up": True}],
        }

    monkeypatch.setattr(poppaping, "monitor_history", fake_hist)
    _login(client, db, user)
    r = client.get(f"/servers/{i.id}/monitor/data.json?period=7d")
    assert r.status_code == 200
    body = r.json()
    assert body["uptime_pct"] == 99.9 and body["points"][0]["ms"] == 120
    assert captured["monitor_id"] == "mon-1" and captured["period"] == "7d"
    # bad period is coerced, not rejected
    monkeypatch.setattr(poppaping, "monitor_history", lambda k, m, p: {"period_used": p})
    assert client.get(f"/servers/{i.id}/monitor/data.json?period=nonsense").json()["period_used"] == "24h"


def test_monitor_data_unmonitored_is_404(client, db, seeded):
    user = seeded["user"]
    user.poppaping_api_key = "pp_k"
    db.commit()
    i = Instance(
        user_id=user.id,
        plan_id=seeded["plan"].id,
        image_id=seeded["image"].id,
        label="nomon",
        hostname="nomon.warpyard.test",
        status="running",
        vmid=95002,
    )
    db.add(i)
    db.commit()
    _login(client, db, user)
    assert client.get(f"/servers/{i.id}/monitor/data.json").status_code == 404


def test_history_reverses_and_parses(monkeypatch):
    """monitor_history reverses newest-first checks to oldest-first and parses timestamps."""

    class FakeResp:
        def __init__(self, code, payload):
            self.status_code = code
            self._p = payload

        def json(self):
            return self._p

    def fake_request(method, url, api_key, **kw):
        if url.endswith("/uptime"):
            return FakeResp(
                200,
                {
                    "data": {
                        "uptime_percentage": 99.5,
                        "avg_response_time_ms": 88,
                        "up_checks": 10,
                        "down_checks": 0,
                        "total_checks": 10,
                    }
                },
            )
        return FakeResp(
            200,
            {
                "data": [
                    {"checked_at": "2026-07-13T12:05:00Z", "response_time_ms": 90, "status": "up"},
                    {"checked_at": "2026-07-13T12:00:00Z", "response_time_ms": 80, "status": "up"},
                ]
            },
        )

    monkeypatch.setattr(poppaping, "_request", fake_request)
    out = poppaping.monitor_history("pp_k", "mon-1", "24h")
    assert out["uptime_pct"] == 99.5 and out["avg_ms"] == 88
    assert [p["ms"] for p in out["points"]] == [80, 90]  # oldest→newest
    assert out["points"][0]["t"] < out["points"][1]["t"]


def test_account_page_has_tabs(client, db, seeded):
    _login(client, db, seeded["user"])
    html = client.get("/account").text
    assert 'role="tablist"' in html
    for tab in ("account", "invites", "ai", "monitoring", "access"):
        assert f'data-tab="{tab}"' in html
