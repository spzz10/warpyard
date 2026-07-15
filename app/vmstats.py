"""Per-VM resource metrics for the dashboard graphs, shaped from Proxmox rrddata.

Read-only. Uses the power token purely because it's the least dangerous token that
can read rrddata (VM.Audit comes with the shared pool role on every token)."""

from app.proxmox import ProxmoxClient

TIMEFRAMES = ("hour", "day", "week")


def _num(row: dict, key: str) -> float | None:
    """A numeric field, or None for gaps (missing key, null, NaN — VM stopped etc.)."""
    v = row.get(key)
    if isinstance(v, int | float) and not isinstance(v, bool) and v == v:
        return v
    return None


def series(vmid: int, timeframe: str = "hour") -> dict:
    """Chart-ready payload: {timeframe, points: [{t, cpu%, mem, maxmem, netin, netout,
    diskread, diskwrite}], now: <last live point>}. Gaps stay as nulls so charts can
    show them honestly instead of interpolating over downtime."""
    if timeframe not in TIMEFRAMES:
        raise ValueError(f"timeframe must be one of {', '.join(TIMEFRAMES)}")
    rows = ProxmoxClient("power").vm_rrddata(vmid, timeframe)
    points = []
    for row in sorted(rows, key=lambda r: r.get("time") or 0):
        t = row.get("time")
        if not t:
            continue
        cpu = _num(row, "cpu")
        points.append(
            {
                "t": int(t),
                "cpu": round(cpu * 100, 1) if cpu is not None else None,  # rrd gives 0..1
                "mem": _num(row, "mem"),
                "maxmem": _num(row, "maxmem"),
                "netin": _num(row, "netin"),
                "netout": _num(row, "netout"),
                "diskread": _num(row, "diskread"),
                "diskwrite": _num(row, "diskwrite"),
            }
        )
    now = next((p for p in reversed(points) if p["cpu"] is not None), None)
    return {"timeframe": timeframe, "points": points, "now": now}
