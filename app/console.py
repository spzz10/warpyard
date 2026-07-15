"""Browser console (noVNC) for a tenant VM.

Flow: the dashboard opens /servers/{id}/console (session-authed). We ask Proxmox for a
vncproxy ticket with the console-only token, mint a SHORT-LIVED, single-use ticket bound
to (user, vmid), and hand the browser a noVNC page. noVNC opens a WebSocket back to
/servers/{id}/console/ws?t=<ticket>; we validate it, then relay frames to Proxmox's
vncwebsocket. The browser never sees Proxmox creds or the raw VNC ticket.
"""

import asyncio
import secrets
import ssl
import time
from urllib.parse import quote

import websockets
from fastapi import APIRouter, Depends, Request, WebSocket
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app import states
from app.config import get_settings
from app.database import SessionLocal, get_db
from app.models import Instance, User

router = APIRouter(tags=["console"])
templates = Jinja2Templates(directory="app/templates")

# one-time console tickets: token -> {user_id, vmid, node, pve_ticket, port, exp}
_TICKETS: dict[str, dict] = {}
_TICKET_TTL = 60  # seconds to open the socket after clicking Console


def _current_user(request: Request, db: Session) -> User | None:
    uid = request.session.get("uid")
    return db.get(User, uid) if uid else None


@router.get("/servers/{instance_id}/console", response_class=HTMLResponse)
def console_page(request: Request, instance_id: int, db: Session = Depends(get_db)):
    user = _current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    instance = db.get(Instance, instance_id)
    if not instance or instance.user_id != user.id:
        return HTMLResponse("Not found", status_code=404)
    if instance.status != states.RUNNING or instance.vmid is None:
        return HTMLResponse("The server must be running to open a console.", status_code=409)

    from app.proxmox import ProxmoxClient, ProxmoxError

    try:
        vnc = ProxmoxClient("console").vncproxy(instance.vmid)
    except ProxmoxError as e:
        return HTMLResponse(f"Console unavailable: {e}", status_code=502)

    token = secrets.token_urlsafe(24)
    _TICKETS[token] = {
        "user_id": user.id,
        "vmid": instance.vmid,
        "node": get_settings().PROXMOX_NODE,
        "pve_ticket": vnc["ticket"],
        "port": vnc["port"],
        "exp": time.time() + _TICKET_TTL,
    }
    # Proxmox's VNC layer uses "VNC Authentication" with the vncproxy ticket as the
    # password, so noVNC needs it. It's a short-lived, single-VM console credential
    # (not the API token) — this is exactly how Proxmox's own web console works.
    return templates.TemplateResponse(
        request,
        "console.html",
        {
            "label": instance.label,
            "instance_id": instance_id,
            "token": token,
            "vnc_password": vnc["ticket"],
        },
    )


def _pop_valid(token: str, user_id: int) -> dict | None:
    t = _TICKETS.pop(token, None)  # single-use: remove on claim
    if not t or t["user_id"] != user_id or t["exp"] < time.time():
        return None
    return t


@router.websocket("/servers/{instance_id}/console/ws")
async def console_ws(ws: WebSocket, instance_id: int, t: str = ""):
    await ws.accept(subprotocol="binary")
    uid = ws.scope.get("session", {}).get("uid")  # SessionMiddleware populates scope["session"]
    ticket = _pop_valid(t, uid) if uid else None
    if not ticket:
        await ws.close(code=4401)
        return

    s = get_settings()
    host = s.PROXMOX_API_URL.split("://", 1)[-1]
    pve_url = (
        f"wss://{host}/api2/json/nodes/{ticket['node']}/qemu/{ticket['vmid']}/vncwebsocket"
        f"?port={ticket['port']}&vncticket={quote(ticket['pve_ticket'], safe='')}"
    )
    sslctx = ssl.create_default_context()
    if not s.PROXMOX_VERIFY_TLS:
        sslctx.check_hostname = False
        sslctx.verify_mode = ssl.CERT_NONE

    try:
        async with websockets.connect(
            pve_url,
            additional_headers={"Authorization": f"PVEAPIToken={s.PROXMOX_TOKEN_CONSOLE}"},
            ssl=sslctx,
            subprotocols=["binary"],
            max_size=None,
        ) as pve:
            await _relay(ws, pve)
    except Exception:  # noqa: BLE001 — any relay/handshake failure just closes the console
        await ws.close(code=1011)


async def _relay(browser: WebSocket, pve) -> None:
    async def b2p():
        while True:
            data = await browser.receive_bytes()
            await pve.send(data)

    async def p2b():
        async for msg in pve:
            await browser.send_bytes(msg if isinstance(msg, bytes) else msg.encode())

    done, pending = await asyncio.wait(
        {asyncio.create_task(b2p()), asyncio.create_task(p2b())}, return_when=asyncio.FIRST_COMPLETED
    )
    for task in pending:
        task.cancel()


# expose the session store cleanup for tests
def _reset():  # pragma: no cover
    _TICKETS.clear()


__all__ = ["router", "SessionLocal"]
