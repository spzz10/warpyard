"""Platform-auth gate for tenant web ingress ("Require Warpyard login" toggle).

When an instance is `gated`, the edge Caddy forward-auths every request to
`GET /edge/gate` before proxying to the VM. Flow for an unauthenticated visitor:

    browser -> https://<label>.<base>/           (no gate cookie)
      edge forward_auth -> CP /edge/gate          -> 302 to app /gate?next=...
      browser -> app.<base>/gate                  (session-gated; log in as a member)
      CP /gate mints a signed, host-scoped token  -> 302 to https://<label>.<base>/__wygate?token=..
      browser -> <label>.<base>/__wygate           (edge bypasses auth for this one path)
      CP /__wygate sets the wy_gate cookie          -> 302 back to the original URL
      browser -> https://<label>.<base>/           (now carries wy_gate) -> forward_auth 200 -> VM

The gate token is a separate, host-bound, signed cookie — never the platform session
cookie (that stays host-only on app.<base> so tenant VMs can never see it). Policy:
any active member passes (that's what "require Warpyard login" means)."""

from urllib.parse import urlencode, urlparse

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response
from itsdangerous import BadData, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from app.config import get_settings, session_secret
from app.database import get_db
from app.models import User

router = APIRouter(tags=["gate"])

GATE_COOKIE = "wy_gate"
GATE_TTL = 12 * 3600  # re-auth twice a day


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(session_secret(), salt="edge-gate")


def _mint(host: str, uid: int) -> str:
    return _serializer().dumps({"h": host, "u": uid})


def _verify(token: str | None, host: str) -> int | None:
    if not token:
        return None
    try:
        data = _serializer().loads(token, max_age=GATE_TTL)
    except BadData:
        return None
    return data["u"] if data.get("h") == host else None


def _is_platform_host(host: str) -> bool:
    """A same-platform hostname (prevents the next-param being an open redirect)."""
    base = get_settings().BASE_DOMAIN.lower()
    host = (host or "").lower()
    return host == base or host.endswith("." + base)


def _safe_target(url: str) -> str | None:
    """Validate a redirect target is https on a platform host; return its host or None."""
    try:
        p = urlparse(url)
    except ValueError:
        return None
    if p.scheme != "https" or not p.hostname or not _is_platform_host(p.hostname):
        return None
    return p.hostname


@router.get("/edge/gate")
def edge_gate(request: Request):
    """Forward-auth endpoint the edge calls before proxying. 200 = allow (valid cookie for
    this host); otherwise 302 the visitor to the platform login handoff. Reachable only from
    the edge (the app vhost blocks /edge*), so it never needs the sync token."""
    host = request.headers.get("x-wy-host", "")
    uri = request.headers.get("x-wy-uri", "/")
    if _verify(request.cookies.get(GATE_COOKIE), host) is not None:
        return Response(status_code=200)
    # bounce to the platform login handoff, remembering where they were going
    nxt = f"https://{host}{uri}"
    login = get_settings().PUBLIC_URL.rstrip("/") + "/gate?" + urlencode({"next": nxt})
    return RedirectResponse(login, status_code=302)


@router.get("/gate")
def gate(request: Request, next: str = "", db: Session = Depends(get_db)):
    """Runs on app.<base> (session cookie is present here). A logged-in member gets a
    host-scoped gate token planted on the target host; a logged-out visitor is sent to /login
    and returned here afterward."""
    target = _safe_target(next)
    if target is None:
        return RedirectResponse(get_settings().PUBLIC_URL.rstrip("/"), status_code=303)
    uid = request.session.get("uid")
    user = db.get(User, uid) if uid else None
    if not user or user.status != "active":
        back = "/gate?" + urlencode({"next": next})
        return RedirectResponse("/login?" + urlencode({"next": back}), status_code=303)
    # plant the token on the target host (a Set-Cookie from app.<base> can't scope to it)
    token = _mint(target, user.id)
    handoff = f"https://{target}/__wygate?" + urlencode({"token": token, "next": next})
    return RedirectResponse(handoff, status_code=303)


@router.get("/__wygate")
def wygate_set(token: str = "", next: str = ""):
    """Runs on the target tenant host (the edge routes this one path around forward-auth).
    Validates the token, sets the host-scoped gate cookie, and returns to the original URL."""
    target = _safe_target(next)
    uid = _verify(token, target) if target else None
    if uid is None:
        return RedirectResponse(get_settings().PUBLIC_URL.rstrip("/"), status_code=303)
    resp = RedirectResponse(next, status_code=303)
    resp.set_cookie(
        GATE_COOKIE,
        token,
        max_age=GATE_TTL,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    return resp
