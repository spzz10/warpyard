"""Thin PoppaPing REST client — one-click uptime monitoring for servers. The user brings
their own PoppaPing API key (or we provision an account via the partner endpoint) and we
create/delete monitors on their account, storing only the monitor id.

PoppaPing wraps API responses in {"data": ...}."""

import httpx

from app.config import get_settings


def _base() -> str:
    return get_settings().POPPAPING_BASE_URL.rstrip("/")


TIMEOUT = 8.0


class PoppaPingError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _headers(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}"}


def _err_detail(r: httpx.Response) -> str:
    try:
        data = r.json()
        return data.get("message") or data.get("detail") or r.text[:200]
    except Exception:
        return r.text[:200] or f"HTTP {r.status_code}"


def _request(method: str, url: str, api_key: str, **kwargs) -> httpx.Response:
    try:
        r = httpx.request(method, url, headers=_headers(api_key), timeout=TIMEOUT, **kwargs)
    except httpx.HTTPError as e:
        raise PoppaPingError(f"Couldn't reach PoppaPing ({e.__class__.__name__}).") from e
    if r.status_code == 401:
        raise PoppaPingError("PoppaPing rejected the API key — reconnect it on your Account page.")
    return r


def create_monitor(
    api_key: str,
    name: str,
    *,
    type_: str,
    url: str | None = None,
    host: str | None = None,
    port: int | None = None,
    alert_channel_ids: list | None = None,
) -> str:
    """Create a monitor, return its id. Raises PoppaPingError with a user-showable message."""
    body: dict = {"name": name, "type": type_}
    if url:
        body["url"] = url
    if host:
        body["host"] = host
    if port:
        body["port"] = port
    if alert_channel_ids:
        body["alert_channel_ids"] = alert_channel_ids
    r = _request("POST", f"{_base()}/api/v1/monitors", api_key, json=body)
    if r.status_code >= 400:
        raise PoppaPingError(f"PoppaPing: {_err_detail(r)}")
    mid = ((r.json() or {}).get("data") or {}).get("id")
    if not mid:
        raise PoppaPingError("PoppaPing returned no monitor id.")
    return str(mid)


def find_monitor(api_key: str, name: str) -> str | None:
    """Adopt an existing monitor by exact name (heals double-creates), else None."""
    r = _request("GET", f"{_base()}/api/v1/monitors", api_key, params={"per_page": 100})
    if r.status_code >= 400:
        raise PoppaPingError(f"PoppaPing: {_err_detail(r)}")
    for m in (r.json() or {}).get("data") or []:
        if m.get("name") == name:
            return str(m["id"])
    return None


def ensure_email_channel(api_key: str, email: str) -> str | None:
    """Find or create an email alert channel for `email`; returns its id, or None when the
    key's scopes can't manage channels (we degrade to an unalerted monitor)."""
    r = _request("GET", f"{_base()}/api/v1/alerts", api_key, params={"per_page": 100})
    if r.status_code == 403:
        return None
    if r.status_code < 400:
        for ch in (r.json() or {}).get("data") or []:
            cfg = ch.get("config") or {}
            # channel config carries {"addresses": [...]} (older shapes used {"email": ...})
            addresses = [a.lower() for a in (cfg.get("addresses") or [])]
            if cfg.get("email"):
                addresses.append(cfg["email"].lower())
            if ch.get("channel_type") == "email" and email.lower() in addresses:
                return str(ch["id"])
    r = _request("POST", f"{_base()}/api/v1/alert-channels", api_key, json={"channel_type": "email", "email": email})
    if r.status_code == 403:
        return None
    if r.status_code >= 400:
        raise PoppaPingError(f"PoppaPing: {_err_detail(r)}")
    ch_id = ((r.json() or {}).get("data") or {}).get("id")
    return str(ch_id) if ch_id else None


def delete_monitor(api_key: str, monitor_id: str) -> None:
    """Best-effort delete — a monitor already gone (404) is success."""
    r = _request("DELETE", f"{_base()}/api/v1/monitors/{monitor_id}", api_key)
    if r.status_code >= 400 and r.status_code != 404:
        raise PoppaPingError(f"PoppaPing: {_err_detail(r)}")


def monitor_history(api_key: str, monitor_id: str, period: str = "24h") -> dict:
    """Uptime summary + recent checks for a monitor, shaped for the Monitoring-tab charts:
    {uptime_pct, avg_ms, up, down, total, points:[{t:<unix s>, ms, up:bool}] oldest→newest}."""
    up = _request("GET", f"{_base()}/api/v1/monitors/{monitor_id}/uptime", api_key, params={"period": period})
    if up.status_code >= 400:
        raise PoppaPingError(f"PoppaPing: {_err_detail(up)}")
    summ = (up.json() or {}).get("data") or {}
    # newest-first, up to 200 recent checks; reverse to oldest→newest for the time axis
    per = {"24h": 200, "7d": 300, "30d": 400, "90d": 500}.get(period, 200)
    ck = _request("GET", f"{_base()}/api/v1/monitors/{monitor_id}/checks", api_key, params={"per_page": per})
    points = []
    if ck.status_code < 400:
        from datetime import datetime

        for c in reversed((ck.json() or {}).get("data") or []):
            ts = c.get("checked_at")
            if not ts:
                continue
            try:
                t = int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())
            except (ValueError, AttributeError):
                continue
            points.append({"t": t, "ms": c.get("response_time_ms"), "up": c.get("status") == "up"})
    return {
        "uptime_pct": summ.get("uptime_percentage"),
        "avg_ms": summ.get("avg_response_time_ms"),
        "up": summ.get("up_checks", 0),
        "down": summ.get("down_checks", 0),
        "total": summ.get("total_checks", 0),
        "points": points,
    }


def provision_account(partner_secret: str, email: str) -> str | None:
    """Create a PoppaPing account for a Warpyard user via the partner endpoint. Returns the
    new account's API key, or None when an account with that email already exists (PoppaPing
    never hands out keys for existing accounts — the user pastes their own)."""
    try:
        r = httpx.post(
            f"{_base()}/partner/warpyard/provision",
            json={"email": email},
            headers={"X-Partner-Secret": partner_secret},
            timeout=TIMEOUT,
        )
    except httpx.HTTPError as e:
        raise PoppaPingError(f"Couldn't reach PoppaPing ({e.__class__.__name__}).") from e
    if r.status_code == 200 and (r.json() or {}).get("status") == "exists":
        return None
    if r.status_code == 201:
        key = (r.json() or {}).get("api_key")
        if key:
            return str(key)
    raise PoppaPingError(f"PoppaPing provisioning failed: {_err_detail(r)}")
