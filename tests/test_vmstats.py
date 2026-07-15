"""vmstats.series shaping: cpu scaling, gap handling, ordering, timeframe validation.

Real rrddata rows (captured live from a real PVE node, 2026-07-13, all four tokens
could read it — VM.Audit rides on the shared pool role) look like:
  {"time": 1783901460, "cpu": 0.00155947074779998, "mem": 1644699648,
   "maxmem": 4294967296, "maxcpu": 2, "memhost": 2073925768.53333,
   "disk": 0, "maxdisk": 10737418240, "netin": 38.24, "netout": 469.723333333333,
   "diskread": 0, "diskwrite": 4410.02666666667, "pressurecpusome": 0, ...}
  {"time": 1783901520, "cpu": 0.00122735023065346, "mem": 1644699648, ...}
Rows from downtime windows omit cpu/mem/net keys or carry nulls.
"""

import math

import pytest

from app import vmstats


class _StubClient:
    rows: list[dict] = []
    roles: list[str] = []

    def __init__(self, role: str = "config"):
        _StubClient.roles.append(role)

    def vm_rrddata(self, vmid: int, timeframe: str = "hour") -> list[dict]:
        return _StubClient.rows


@pytest.fixture()
def stub(monkeypatch):
    _StubClient.rows = []
    _StubClient.roles = []
    monkeypatch.setattr(vmstats, "ProxmoxClient", _StubClient)
    return _StubClient


def test_shapes_and_scales_cpu(stub):
    stub.rows = [
        {
            "time": 1783901460,
            "cpu": 0.00155947074779998,
            "mem": 1644699648,
            "maxmem": 4294967296,
            "netin": 38.24,
            "netout": 469.723333333333,
            "diskread": 0,
            "diskwrite": 4410.02666666667,
            "maxcpu": 2,
            "pressurecpusome": 0,
        },
        {"time": 1783901520, "cpu": 0.5, "mem": 2147483648, "maxmem": 4294967296, "netin": 3.2, "netout": 4},
    ]
    out = vmstats.series(90002, "hour")
    assert out["timeframe"] == "hour"
    assert len(out["points"]) == 2
    p0 = out["points"][0]
    assert p0["t"] == 1783901460
    assert p0["cpu"] == 0.2  # 0.00155.. * 100 rounded to 1 decimal
    assert p0["mem"] == 1644699648 and p0["maxmem"] == 4294967296
    assert p0["diskwrite"] == 4410.02666666667
    assert out["points"][1]["cpu"] == 50.0
    assert out["now"] == out["points"][1]
    assert set(p0) == {"t", "cpu", "mem", "maxmem", "netin", "netout", "diskread", "diskwrite"}


def test_gaps_missing_keys_and_nulls(stub):
    stub.rows = [
        {"time": 100, "cpu": 0.01, "mem": 10, "maxmem": 100, "netin": 1, "netout": 2, "diskread": 0, "diskwrite": 0},
        {"time": 160},  # downtime row: keys absent
        {"time": 220, "cpu": None, "mem": None, "netin": None},  # explicit nulls
        {"time": 280, "cpu": float("nan"), "mem": 5},  # NaN must not leak into JSON
        {"cpu": 0.5},  # no timestamp -> dropped
    ]
    out = vmstats.series(1, "day")
    assert [p["t"] for p in out["points"]] == [100, 160, 220, 280]
    assert out["points"][1]["cpu"] is None and out["points"][1]["mem"] is None
    assert out["points"][2]["cpu"] is None and out["points"][2]["netin"] is None
    assert out["points"][3]["cpu"] is None and out["points"][3]["mem"] == 5
    assert not any(isinstance(v, float) and math.isnan(v) for p in out["points"] for v in p.values() if v is not None)
    # "now" skips back past the gap rows to the last point with live cpu
    assert out["now"]["t"] == 100


def test_rows_sorted_by_time(stub):
    stub.rows = [{"time": 300, "cpu": 0.3}, {"time": 100, "cpu": 0.1}, {"time": 200, "cpu": 0.2}]
    out = vmstats.series(1, "week")
    assert [p["t"] for p in out["points"]] == [100, 200, 300]
    assert out["now"]["cpu"] == 30.0


def test_empty_and_all_gap_series(stub):
    stub.rows = []
    out = vmstats.series(1, "hour")
    assert out["points"] == [] and out["now"] is None
    stub.rows = [{"time": 100}, {"time": 160}]
    out = vmstats.series(1, "hour")
    assert len(out["points"]) == 2 and out["now"] is None


def test_timeframe_validated_before_any_api_call(stub):
    with pytest.raises(ValueError):
        vmstats.series(1, "month")
    with pytest.raises(ValueError):
        vmstats.series(1, "")
    assert stub.roles == []  # never constructed a client


def test_uses_power_token(stub):
    stub.rows = []
    vmstats.series(1, "hour")
    assert stub.roles == ["power"]
