"""Host page: hoststats shaping (node rrd → chart points, status/storage/VMs
→ overview), the Board-model visibility filter on the VM table, and route gating
(members only — logged-out gets nothing).

Real node rrddata rows (captured live from a real PVE node, 2026-07-14, power token with
WYNodeAudit) look like:
  {"time": 1784036880, "cpu": 0.0047582941326083, "iowait": 0.000548898408353972,
   "loadavg": 0.187666666666667, "memused": 86787763541.3333, "memtotal": 405597892608,
   "arcsize": 47630803911.2, "netin": 23846.2166666667, "netout": 13939.54,
   "pressureiosome": 0.749666666666667, "maxcpu": 56, ...}
"""

import re

import pytest

from app import hoststats, security
from app.models import Instance, User

NODE_STATUS = {
    "cpu": 0.0086,
    "uptime": 1150000,
    "pveversion": "pve-manager/7.0-14/abc",
    "kversion": "Linux 6.8.4-2-pve #1 SMP",
    "loadavg": ["0.19", "0.24", "0.31"],
    "memory": {"total": 405597892608, "used": 86787763541, "free": 318810129067},
    "cpuinfo": {"cpus": 56, "sockets": 2, "model": "Intel(R) Xeon(R) CPU E5-2680 v4 @ 2.40GHz"},
}
STORAGES = [
    {"storage": "local", "type": "dir", "active": 1, "total": 100861726720, "used": 48696795136},
    {"storage": "vmpool", "type": "zfspool", "active": 1, "total": 1731408691200, "used": 51643510784},
    {"storage": "vmpool-enc", "type": "zfspool", "active": 1, "total": 1679765377024, "used": 196608},
    {"storage": "ghost", "type": "dir", "active": 0, "total": 10, "used": 5},  # inactive → hidden
]
VMS = [
    {
        "vmid": 90000,
        "name": "portrait",
        "status": "running",
        "cpu": 0.0034,
        "maxcpu": 2,
        "mem": 873684992,
        "maxmem": 4294967296,
        "uptime": 59230,
        "template": 0,
    },
    {
        "vmid": 90001,
        "name": "asleep",
        "status": "stopped",
        "cpu": 0,
        "maxcpu": 4,
        "mem": 0,
        "maxmem": 8589934592,
        "uptime": 0,
        "template": 0,
    },
    {
        "vmid": 90002,
        "name": "shared-site",
        "status": "running",
        "cpu": 0.4,
        "maxcpu": 2,
        "mem": 2147483648,
        "maxmem": 4294967296,
        "uptime": 100,
        "template": 0,
    },
]
RRD_HOUR = [
    {
        "time": 100 + i,
        "cpu": 0.01 * (i + 1),
        "loadavg": 0.1 * (i + 1),
        "memused": 100.0 + i,
        "arcsize": 50.0,
        "netin": 10.0,
        "netout": 20.0,
    }
    for i in range(4)
]


class _StubClient:
    rows: list[dict] = []
    node = "pve-test-node"

    def __init__(self, role: str = "config"):
        _StubClient.last_role = role

    def node_rrddata(self, timeframe: str = "hour") -> list[dict]:
        return _StubClient.rows

    def node_status(self) -> dict:
        return NODE_STATUS

    def node_storage(self) -> list[dict]:
        return STORAGES

    def cluster_vms(self) -> list[dict]:
        return VMS


@pytest.fixture()
def stub(monkeypatch):
    _StubClient.rows = []
    monkeypatch.setattr(hoststats, "ProxmoxClient", _StubClient)
    return _StubClient


def _login(client, db, user, admin=False):
    user.is_admin = admin
    user.password_hash = security.hash_password("pw12345678")
    db.commit()
    r = client.post("/login", data={"email": user.email, "password": "pw12345678"})
    assert r.status_code in (200, 303)


def _fleet(db, seeded):
    """portrait = seeded user's private server; shared-site = another member's Board share."""
    other = User(email="neighbor@example.com")
    db.add(other)
    db.flush()
    mine = Instance(
        user_id=seeded["user"].id,
        plan_id=seeded["plan"].id,
        image_id=seeded["image"].id,
        label="portrait",
        status="running",
        vmid=90000,
    )
    theirs = Instance(
        user_id=other.id,
        plan_id=seeded["plan"].id,
        image_id=seeded["image"].id,
        label="shared-site",
        status="running",
        vmid=90002,
        shared=True,
    )
    db.add_all([mine, theirs])
    db.commit()
    return mine, theirs, other


# ---- series shaping ----


def test_series_scales_and_gaps(stub):
    stub.rows = [
        {
            "time": 200,
            "cpu": 0.25,
            "iowait": 0.005,
            "loadavg": 1.5,
            "memused": 100.0,
            "memtotal": 400.0,
            "arcsize": 50.0,
            "netin": 10.0,
            "netout": 20.0,
            "pressurecpusome": 0.1,
            "pressurememorysome": 0.2,
            "pressureiosome": 0.75,
        },
        {"time": 100, "cpu": None, "loadavg": None},  # downtime row, out of order
    ]
    out = hoststats.series("hour")
    assert [p["t"] for p in out["points"]] == [100, 200]  # re-sorted
    live = out["points"][1]
    assert live["cpu"] == 25.0 and live["iowait"] == 0.5  # fractions → %
    assert live["load"] == 1.5 and live["arc"] == 50.0
    assert live["cpupsi"] == 0.1 and live["mempsi"] == 0.2 and live["iopsi"] == 0.75
    assert out["points"][0]["cpu"] is None  # gap preserved
    assert out["now"] == live  # last live point, skipping gaps


def test_series_uses_power_token_and_validates_timeframe(stub):
    hoststats.series("hour")
    assert _StubClient.last_role == "power"
    with pytest.raises(ValueError):
        hoststats.series("year")


def test_sparkline_shapes():
    s = hoststats.sparkline([0, 5, None, 10])
    assert s and s["line"].count(",") == 3  # gaps dropped, 3 points survive
    assert s["area"].startswith("2.0,28") and s["area"].endswith("98.0,28")  # closed to the baseline
    assert hoststats.sparkline([1]) is None  # not enough points → no spark
    assert hoststats.sparkline([None, None]) is None


# ---- overview shaping ----


def test_overview_admin_sees_everything(stub, db, seeded):
    stub.rows = RRD_HOUR
    mine, theirs, other = _fleet(db, seeded)
    seeded["user"].is_admin = True
    db.commit()
    ov = hoststats.overview(db, seeded["user"])
    assert ov["cores"] == 56 and ov["cpu_pct"] == 0.9
    assert ov["model"] == "2× Intel Xeon E5-2680 v4"
    assert ov["mem_pct"] == 21 and ov["pve"] == "7.0-14" and ov["kernel"] == "6.8.4-2-pve"
    assert ov["vms_running"] == 2 and ov["vms_total"] == 3 and ov["alloc_vcpus"] == 8
    assert ov["admin"] is True

    names = [s["name"] for s in ov["storage"]]
    assert names == ["vmpool", "vmpool-enc", "local"]  # curated order, inactive hidden
    assert ov["storage"][2]["pct"] == 48 and ov["storage"][2]["tone"] == ""

    vms = ov["vms"]
    assert [v["name"] for v in vms] == ["asleep", "portrait", "shared-site"]  # all, name-sorted
    portrait = vms[1]
    assert portrait["instance_id"] == mine.id and portrait["owner"] == "friend"
    assert portrait["running"] and portrait["cpu_pct"] == 0.3
    assert vms[0]["instance_id"] is None and vms[0]["cpu_pct"] is None  # unmapped + stopped

    # trends + composition come from the hour rrd
    assert ov["sparks"]["cpu"] and ov["sparks"]["net"]
    assert ov["net_now"] == "↓10 B/s ↑20 B/s"
    comp = ov["composition"]
    assert [c["label"].split(" ")[0] for c in comp] == ["VMs", "ZFS", "Free"]
    # top consumers ranked by host impact: shared-site burns 0.8 cores vs portrait's ~0.007
    assert ov["top_cpu"][0]["name"] == "shared-site" and ov["top_cpu"][0]["width"] == 100
    assert ov["top_mem"][0]["name"] == "shared-site"


def test_overview_member_sees_own_plus_shared(stub, db, seeded):
    mine, theirs, other = _fleet(db, seeded)
    ov = hoststats.overview(db, seeded["user"])
    assert ov["admin"] is False
    vms = ov["vms"]
    # asleep (unmapped) is hidden; own + the neighbor's Board share remain
    assert [v["name"] for v in vms] == ["portrait", "shared-site"]
    assert vms[0]["instance_id"] == mine.id  # own → linked
    assert vms[1]["instance_id"] is None and vms[1]["owner"] == "neighbor"  # shared → visible, not linked
    # capacity tallies still describe the whole host
    assert ov["vms_total"] == 3 and ov["alloc_vcpus"] == 8
    # top consumers only rank what the member can see
    assert all(b["name"] in ("portrait", "shared-site") for b in ov["top_cpu"] + ov["top_mem"])


def test_storage_tone_thresholds(stub, db, seeded):
    STORAGES.append({"storage": "hot", "type": "dir", "active": 1, "total": 100, "used": 92})
    try:
        ov = hoststats.overview(db, seeded["user"])
        hot = next(s for s in ov["storage"] if s["name"] == "hot")
        assert hot["tone"] == "bad" and hot["pct"] == 92
    finally:
        STORAGES.pop()


def test_fmt_helpers():
    assert hoststats.fmt_bytes(405597892608) == "377.7 GB"
    assert hoststats.fmt_bytes(None) == "—"
    assert hoststats.fmt_uptime(1150000) == "13d 7h"
    assert hoststats.fmt_uptime(59230) == "16h 27m"
    assert hoststats.fmt_uptime(0) == "—"


def test_snapshot(stub):
    s = hoststats.snapshot()
    assert s == {"cpu_pct": 0.9, "mem_pct": 21, "vms_running": 2, "vms_total": 3, "uptime": "13d 7h"}


# ---- routes ----


def test_host_routes_need_login(client, stub):
    assert client.get("/host", follow_redirects=False).status_code in (302, 303, 307)  # → /login
    assert client.get("/host/overview").status_code == 401
    assert client.get("/host/metrics.json").status_code == 401


def test_host_page_renders_for_member(client, db, seeded, stub):
    _fleet(db, seeded)
    _login(client, db, seeded["user"], admin=False)
    html = client.get("/host").text
    assert "Platform host" in html and "pve-test-node" in html
    assert 'data-chart="cpu"' in html and 'data-chart="pressure"' in html
    assert "vmpool" in html and "Servers on this host" in html
    assert "shared-site" in html and "asleep" not in html  # visibility filter reaches the page
    assert "other members' servers stay private" in html
    ids = re.findall(r'\bid="([^"]+)"', html)
    assert not {i for i in ids if ids.count(i) > 1}, "duplicate DOM ids"

    frag = client.get("/host/overview").text
    assert 'hx-swap-oob="innerHTML"' in frag and "ht-tiles" in frag
    # sortable VM table: headers declare sort keys, rows carry raw sortable values
    assert 'data-sort="cpu"' in frag and 'data-sort="uptime"' in frag
    assert 'data-mem="2147483648"' in frag and 'data-vmid="90002"' in frag

    data = client.get("/host/metrics.json").json()
    assert data["timeframe"] == "hour" and data["points"] == []
    assert client.get("/host/metrics.json?timeframe=year").status_code == 400


def test_board_shows_platform_health_strip(client, db, seeded, stub):
    _login(client, db, seeded["user"])
    html = client.get("/board").text
    assert "Platform host" in html and 'href="/host"' in html and "servers up" in html
