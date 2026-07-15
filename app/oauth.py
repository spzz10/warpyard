"""OAuth 2.1 authorization server for the MCP server (and any OAuth client). Public clients
with PKCE + dynamic client registration. The AS lives on the control plane (PUBLIC_URL)
where user sessions already exist; the MCP resource server validates the tokens it issues."""

import base64
import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode, urlparse

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models import OAuthClient, OAuthCode, OAuthToken, User

router = APIRouter(tags=["oauth"])
templates = Jinja2Templates(directory="app/templates")

CODE_TTL = 300  # seconds
TOKEN_TTL = 30 * 24 * 3600  # 30 days
SCOPE = "servers"  # single scope: manage the user's servers


def _issuer() -> str:
    return get_settings().PUBLIC_URL.rstrip("/")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


# ── discovery metadata ─────────────────────────────────────────────────
@router.get("/.well-known/oauth-authorization-server")
def as_metadata():
    iss = _issuer()
    return JSONResponse(
        {
            "issuer": iss,
            "authorization_endpoint": f"{iss}/oauth/authorize",
            "token_endpoint": f"{iss}/oauth/token",
            "registration_endpoint": f"{iss}/oauth/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": [SCOPE],
        }
    )


@router.get("/.well-known/oauth-protected-resource")
def rs_metadata(request: Request):
    # the resource server is whatever host the client reached (e.g. the MCP_URL hostname)
    host = request.headers.get("host", request.url.netloc)
    return JSONResponse(
        {
            "resource": f"https://{host}",
            "authorization_servers": [_issuer()],
            "scopes_supported": [SCOPE],
            "bearer_methods_supported": ["header"],
        }
    )


# also expose the RS metadata under the /mcp path form some clients probe
@router.get("/.well-known/oauth-protected-resource/mcp")
def rs_metadata_mcp(request: Request):
    return rs_metadata(request)


# ── dynamic client registration (RFC 7591) ─────────────────────────────
@router.post("/oauth/register")
async def register(request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    redirect_uris = body.get("redirect_uris") or []
    if not isinstance(redirect_uris, list) or not redirect_uris:
        return JSONResponse({"error": "invalid_redirect_uri"}, status_code=400)
    client_id = "wyc_" + secrets.token_urlsafe(18)
    db.add(
        OAuthClient(
            client_id=client_id,
            client_name=(body.get("client_name") or "")[:128] or None,
            redirect_uris="\n".join(redirect_uris),
        )
    )
    db.commit()
    return JSONResponse(
        {
            "client_id": client_id,
            "redirect_uris": redirect_uris,
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code"],
            "response_types": ["code"],
        },
        status_code=201,
    )


# ── authorization endpoint ─────────────────────────────────────────────
def _current_user(request: Request, db: Session) -> User | None:
    uid = request.session.get("uid")
    return db.get(User, uid) if uid else None


@router.get("/oauth/authorize")
def authorize(request: Request, db: Session = Depends(get_db)):
    q = request.query_params
    client = db.scalar(select(OAuthClient).where(OAuthClient.client_id == q.get("client_id", "")))
    redirect_uri = q.get("redirect_uri", "")
    if not client or redirect_uri not in client.redirect_uris.split("\n"):
        return HTMLResponse("Invalid client or redirect_uri.", status_code=400)
    if q.get("response_type") != "code" or q.get("code_challenge_method") != "S256" or not q.get("code_challenge"):
        return _deny_redirect(redirect_uri, q.get("state"), "invalid_request")

    user = _current_user(request, db)
    if not user:
        # log in first, then come back to this exact authorize URL
        nxt = f"/oauth/authorize?{urlencode(dict(q))}"
        return RedirectResponse("/login?" + urlencode({"next": nxt}), status_code=303)

    return templates.TemplateResponse(
        request,
        "consent.html",
        {
            "user": user,
            "client_name": client.client_name or "An application",
            "params": dict(q),
        },
    )


@router.post("/oauth/authorize")
def authorize_decision(
    request: Request,
    db: Session = Depends(get_db),
    client_id: str = Form(...),
    redirect_uri: str = Form(...),
    code_challenge: str = Form(...),
    state: str = Form(""),
    scope: str = Form(""),
    decision: str = Form(...),
):
    user = _current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    client = db.scalar(select(OAuthClient).where(OAuthClient.client_id == client_id))
    if not client or redirect_uri not in client.redirect_uris.split("\n"):
        return HTMLResponse("Invalid client.", status_code=400)
    if decision != "approve":
        return _deny_redirect(redirect_uri, state, "access_denied")
    code = secrets.token_urlsafe(32)
    db.add(
        OAuthCode(
            code=code,
            client_id=client_id,
            user_id=user.id,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            scope=SCOPE,
            expires_at=datetime.now(UTC) + timedelta(seconds=CODE_TTL),
        )
    )
    db.commit()
    sep = "&" if "?" in redirect_uri else "?"
    callback = f"{redirect_uri}{sep}{urlencode({'code': code, 'state': state})}"
    if _is_loopback(redirect_uri):
        # Loopback clients (e.g. the Claude Code CLI) run a local listener that the browser
        # can only reach if it's on the same machine. Rather than dump the user on a browser
        # "can't connect to localhost" error, show a guidance page that copies the code and
        # also tries to hand it off in the background (seamless when it IS the same machine).
        return templates.TemplateResponse(
            request,
            "oauth_complete.html",
            {
                "callback_url": callback,
                "code": code,
                "minutes": CODE_TTL // 60,
            },
        )
    return RedirectResponse(callback, status_code=303)


def _is_loopback(redirect_uri: str) -> bool:
    host = (urlparse(redirect_uri).hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "::1"}


def _deny_redirect(redirect_uri: str, state: str | None, error: str):
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{sep}{urlencode({'error': error, 'state': state or ''})}", status_code=303)


# ── token endpoint ─────────────────────────────────────────────────────
@router.post("/oauth/token")
def token(
    db: Session = Depends(get_db),
    grant_type: str = Form(...),
    code: str = Form(...),
    code_verifier: str = Form(...),
    client_id: str = Form(...),
    redirect_uri: str = Form(""),
):
    if grant_type != "authorization_code":
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
    row = db.scalar(select(OAuthCode).where(OAuthCode.code == code))
    if not row or row.used or row.client_id != client_id or row.expires_at.replace(tzinfo=UTC) < datetime.now(UTC):
        return JSONResponse({"error": "invalid_grant"}, status_code=400)
    # PKCE: base64url(sha256(verifier)) must equal the stored challenge
    if _b64(hashlib.sha256(code_verifier.encode()).digest()) != row.code_challenge:
        return JSONResponse({"error": "invalid_grant", "error_description": "PKCE failed"}, status_code=400)
    row.used = True
    access = "wyt_" + secrets.token_urlsafe(32)
    db.add(
        OAuthToken(
            token=access,
            client_id=client_id,
            user_id=row.user_id,
            scope=row.scope,
            expires_at=datetime.now(UTC) + timedelta(seconds=TOKEN_TTL),
        )
    )
    db.commit()
    return JSONResponse(
        {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": TOKEN_TTL,
            "scope": row.scope,
        }
    )


def user_for_token(db: Session, token_str: str) -> User | None:
    """Used by the MCP resource server to validate a Bearer token."""
    row = db.scalar(select(OAuthToken).where(OAuthToken.token == token_str))
    if not row:
        return None
    if row.expires_at and row.expires_at.replace(tzinfo=UTC) < datetime.now(UTC):
        return None
    if row.user.status != "active":
        return None
    return row.user
