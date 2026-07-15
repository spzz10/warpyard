"""Platform-host (the tenant hypervisor) metrics for the admin Host page, shaped from the
Proxmox node API.

Read-only, member-facing (per-VM detail follows the Board share model; admins see
the whole fleet). Uses the power token: it carries WYNodeAudit
(Sys.Audit + Datastore.Audit on /nodes/<node> and /storage — see docs/PVE-SETUP.md)
for node status/rrddata and storage usage, plus the pool-wide VM.Audit it always
had for per-VM stats."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.proxmox import ProxmoxClient
from app.vmstats import _num

TIMEFRAMES = ("hour", "day", "week")

# what each storage is for, in human terms; anything unlisted falls back to its type
STORAGE_NOTES = {
    "vmpool": "Tenant VM disks (ZFS)",
    "vmpool-enc": "Tenant VM disks, encrypted at rest",
    "warpyard-pbs": "Off-host backups (PBS quota)",
    "local": "System, templates & ISOs",
    "local-lvm": "Host system pool",
    "lvm2": "Spare thin pool",
}
_STORAGE_ORDER = list(STORAGE_NOTES)


def fmt_bytes(v: float | None) -> str:
    """Base-1024, matching the chart axis units."""
    if v is None:
        return "—"
    for unit in ("B", "KB", "MB", "GB"):
        if abs(v) < 1024:
            return f"{v:.0f} {unit}" if unit == "B" else f"{v:.1f} {unit}"
        v /= 1024
    return f"{v:.1f} TB"


def fmt_uptime(secs: float | None) -> str:
    if not secs:
        return "—"
    secs = int(secs)
    d, h, m = secs // 86400, secs % 86400 // 3600, secs % 3600 // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def series(timeframe: str = "hour") -> dict:
    """Chart-ready node history. cpu/iowait are fractions of the whole box (0..1 in rrd,
    scaled to %); mem/arc/memtotal in bytes; net in B/s; iopsi is the PSI 'some' %
    already. Gaps stay as nulls so the charts show them honestly."""
    if timeframe not in TIMEFRAMES:
        raise ValueError(f"timeframe must be one of {', '.join(TIMEFRAMES)}")
    rows = ProxmoxClient("power").node_rrddata(timeframe)
    points = []
    for row in sorted(rows, key=lambda r: r.get("time") or 0):
        t = row.get("time")
        if not t:
            continue
        cpu = _num(row, "cpu")
        iowait = _num(row, "iowait")
        points.append(
            {
                "t": int(t),
                "cpu": round(cpu * 100, 1) if cpu is not None else None,
                "iowait": round(iowait * 100, 2) if iowait is not None else None,
                "load": _num(row, "loadavg"),
                "mem": _num(row, "memused"),
                "arc": _num(row, "arcsize"),
                "memtotal": _num(row, "memtotal"),
                "netin": _num(row, "netin"),
                "netout": _num(row, "netout"),
                "cpupsi": _num(row, "pressurecpusome"),
                "mempsi": _num(row, "pressurememorysome"),
                "iopsi": _num(row, "pressureiosome"),
            }
        )
    now = next((p for p in reversed(points) if p["cpu"] is not None), None)
    return {"timeframe": timeframe, "points": points, "now": now}


def sparkline(values: list[float | None], width: int = 100, height: int = 28, pad: int = 2) -> dict | None:
    """SVG polyline/polygon point strings for a tile trend (rendered server-side so
    HTMX swaps and the Board strip need no chart JS). Gaps are dropped, the y-scale
    is 0..max — a spark shows shape, not exact values."""
    pts = [v for v in values if v is not None]
    if len(pts) < 2:
        return None
    top = max(pts) or 1
    step = (width - 2 * pad) / (len(pts) - 1)
    xy = [(pad + i * step, height - pad - (v / top) * (height - 2 * pad)) for i, v in enumerate(pts)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in xy)
    area = f"{xy[0][0]:.1f},{height} {line} {xy[-1][0]:.1f},{height}"
    return {"line": line, "area": area}


def _rate(v: float | None) -> str:
    """Whole-number transfer rate for the Network tile — decimals don't fit there."""
    if v is None:
        return "—"
    for unit in ("B", "KB", "MB", "GB"):
        if abs(v) < 1024:
            return f"{v:.0f} {unit}/s"
        v /= 1024
    return f"{v:.0f} TB/s"


def _cpu_model(cpuinfo: dict) -> str:
    model = cpuinfo.get("model", "").replace("(R)", "").replace("(TM)", "").replace(" CPU", "")
    model = model.split("@")[0].strip()
    sockets = cpuinfo.get("sockets") or 1
    return f"{sockets}× {model}" if sockets > 1 and model else model


def overview(db: Session, user) -> dict:
    """Everything on the Host page except the charts: stat tiles (with trend sparks),
    memory composition, top consumers, storage usage, and the live tenant-VM table.

    Visible to every member; the per-VM table follows the Board's share model —
    admins see the whole fleet, members see their own servers plus ones whose
    owners opted onto the Board (Instance.shared)."""
    from app.models import Instance  # local import: hoststats is imported by tests without app.main

    px = ProxmoxClient("power")
    st = px.node_status()
    mem = st.get("memory", {})
    load = st.get("loadavg", ["—", "—", "—"])
    cores = st.get("cpuinfo", {}).get("cpus") or 0

    # one hour of history feeds the tile sparklines + the ARC share of memory
    hour = [r for r in sorted(px.node_rrddata("hour"), key=lambda r: r.get("time") or 0)]
    sparks = {
        "cpu": sparkline([(_num(r, "cpu") or 0) * 100 if _num(r, "cpu") is not None else None for r in hour]),
        "load": sparkline([_num(r, "loadavg") for r in hour]),
        "mem": sparkline([_num(r, "memused") for r in hour]),
        "net": sparkline(
            [
                (_num(r, "netin") or 0) + (_num(r, "netout") or 0)
                if (_num(r, "netin") is not None or _num(r, "netout") is not None)
                else None
                for r in hour
            ]
        ),
    }
    arc = next((_num(r, "arcsize") for r in reversed(hour) if _num(r, "arcsize") is not None), None)
    last_net = next((r for r in reversed(hour) if _num(r, "netin") is not None), None)
    net_now = f"↓{_rate(_num(last_net, 'netin'))} ↑{_rate(_num(last_net, 'netout'))}" if last_net else "—"

    storage = []
    for s in sorted(
        px.node_storage(),
        key=lambda s: (
            _STORAGE_ORDER.index(s["storage"]) if s["storage"] in _STORAGE_ORDER else 99,
            s["storage"],
        ),
    ):
        if not s.get("active") or not s.get("total"):
            continue
        pct = round(100 * (s.get("used") or 0) / s["total"])
        storage.append(
            {
                "name": s["storage"],
                "note": STORAGE_NOTES.get(s["storage"], s.get("type", "")),
                "used": fmt_bytes(s.get("used")),
                "total": fmt_bytes(s["total"]),
                "pct": pct,
                "tone": "bad" if pct >= 90 else "warn" if pct >= 80 else "",
            }
        )

    admin = bool(user.is_admin)
    raw_vms = px.cluster_vms()
    vmids = [v["vmid"] for v in raw_vms]
    by_vmid = {i.vmid: i for i in db.scalars(select(Instance).where(Instance.vmid.in_(vmids))).all()}
    vms = []
    for v in sorted(raw_vms, key=lambda v: v.get("name") or ""):
        inst = by_vmid.get(v["vmid"])
        mine = bool(inst and inst.user_id == user.id)
        # members see the fleet through the Board's lens: their own + opted-in shares
        if not admin and not mine and not (inst and inst.shared):
            continue
        running = v.get("status") == "running"
        maxmem = v.get("maxmem") or 0
        cpu = (v.get("cpu") or 0) if running else 0
        vms.append(
            {
                "vmid": v["vmid"],
                "name": v.get("name") or str(v["vmid"]),
                "instance_id": inst.id if (inst and (mine or admin)) else None,
                "owner": inst.user.email.split("@")[0] if inst else "—",
                "running": running,
                "cpu_pct": round(cpu * 100, 1) if running else None,
                "maxcpu": v.get("maxcpu") or 0,
                "cores_used": cpu * (v.get("maxcpu") or 0),
                "mem_bytes": (v.get("mem") or 0) if running else 0,
                "mem": fmt_bytes(v.get("mem")) if running else "—",
                "maxmem": fmt_bytes(maxmem),
                "mem_pct": round(100 * (v.get("mem") or 0) / maxmem) if running and maxmem else 0,
                "uptime": fmt_uptime(v.get("uptime")) if running else "—",
                "uptime_secs": int(v.get("uptime") or 0) if running else 0,
            }
        )

    # top consumers among the visible fleet, ranked by impact on the host
    def _bars(rows, key, fmt):
        rows = [r for r in rows if r["running"] and r[key] > 0]
        rows.sort(key=lambda r: r[key], reverse=True)
        rows = rows[:5]
        top = rows[0][key] if rows else 1
        return [
            {
                "name": r["name"],
                "instance_id": r["instance_id"],
                "width": max(round(100 * r[key] / top), 2),
                "value": fmt(r),
            }
            for r in rows
        ]

    top_cpu = _bars(vms, "cores_used", lambda r: f"{r['cores_used']:.2f} cores")
    top_mem = _bars(vms, "mem_bytes", lambda r: fmt_bytes(r["mem_bytes"]))

    mem_total = mem.get("total") or 0
    mem_used = mem.get("used") or 0
    composition = None
    if mem_total and arc is not None:
        apps = max(mem_used - arc, 0)
        free = max(mem_total - mem_used, 0)
        composition = [
            {"label": "VMs & system", "bytes": fmt_bytes(apps), "pct": 100 * apps / mem_total, "cls": "seg-a"},
            {
                "label": "ZFS ARC (releasable cache)",
                "bytes": fmt_bytes(arc),
                "pct": 100 * arc / mem_total,
                "cls": "seg-b",
            },
            {"label": "Free", "bytes": fmt_bytes(free), "pct": 100 * free / mem_total, "cls": "seg-c"},
        ]
    return {
        "node": px.node,
        "cpu_pct": round((st.get("cpu") or 0) * 100, 1),
        "cores": cores,
        "model": _cpu_model(st.get("cpuinfo", {})),
        "load1": load[0],
        "load5": load[1],
        "load15": load[2],
        "mem_used": fmt_bytes(mem_used),
        "mem_total": fmt_bytes(mem_total),
        "mem_pct": round(100 * mem_used / mem_total) if mem_total else 0,
        "uptime": fmt_uptime(st.get("uptime")),
        "pve": (st.get("pveversion") or "").split("/")[1] if "/" in (st.get("pveversion") or "") else "",
        # kversion looks like "Linux 6.8.4-2-pve #1 SMP ..." — keep just the release
        "kernel": (st.get("kversion") or "").split()[1] if len((st.get("kversion") or "").split()) > 1 else "",
        # fleet tallies always cover the whole host (they're capacity facts, not per-VM data)
        "vms_running": sum(1 for r in raw_vms if r.get("status") == "running"),
        "vms_total": len(raw_vms),
        "alloc_vcpus": sum((r.get("maxcpu") or 0) for r in raw_vms),
        "alloc_mem": fmt_bytes(sum((r.get("maxmem") or 0) for r in raw_vms)),
        "admin": admin,
        "net_now": net_now,
        "sparks": sparks,
        "composition": composition,
        "top_cpu": top_cpu,
        "top_mem": top_mem,
        "storage": storage,
        "vms": vms,
    }


def snapshot() -> dict:
    """Tiny host summary for the Board's Platform-health strip (2 cheap reads)."""
    px = ProxmoxClient("power")
    st = px.node_status()
    mem = st.get("memory", {})
    fleet = px.cluster_vms()
    return {
        "cpu_pct": round((st.get("cpu") or 0) * 100, 1),
        "mem_pct": round(100 * (mem.get("used") or 0) / mem["total"]) if mem.get("total") else 0,
        "vms_running": sum(1 for v in fleet if v.get("status") == "running"),
        "vms_total": len(fleet),
        "uptime": fmt_uptime(st.get("uptime")),
    }
