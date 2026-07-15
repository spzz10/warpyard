"""Minimal PBS API client — used only to forget a destroyed server's backup group so a
reused datastore never leaks one tenant's restore points to the next. Authenticates as the
same auth-id that OWNS the groups (PBS lets Datastore.Backup remove own snapshots), so no
extra privileges exist anywhere for this. Best-effort: backups are prunable garbage after
the VM is gone, not state we depend on."""

import httpx

from app.config import get_settings


def forget_group(vmid: int) -> bool:
    """Delete the whole vm/<vmid> backup group. Returns False (never raises) when PBS is
    unreachable, the token is unset, or the group doesn't exist."""
    s = get_settings()
    if not s.PBS_TOKEN:
        return False
    try:
        resp = httpx.delete(
            f"{s.PBS_API_URL}/api2/json/admin/datastore/{s.PBS_DATASTORE}/groups",
            params={"backup-type": "vm", "backup-id": str(vmid)},
            headers={"Authorization": f"PBSAPIToken={s.PBS_TOKEN}"},
            verify=s.PBS_VERIFY_TLS,
            timeout=30.0,
        )
        return resp.status_code < 400
    except httpx.HTTPError:
        return False
