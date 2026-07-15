"""Warpyard dashboard — server-rendered (Jinja + HTMX). Session auth via a signed
cookie. Reuses the same domain logic as the JSON API; power actions post back HTMX
fragments so the server cards update in place."""

import contextlib
import os
import re
from datetime import UTC
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from itsdangerous import BadData, URLSafeTimedSerializer
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import favicon_gen, hoststats, mailer, poppaping, security, service, states
from app.config import get_settings
from app.database import get_db
from app.jobs import queue
from app.models import BoardComment, Event, Image, Instance, Invite, InviteRequest, Plan, SshKey, User, utcnow
from app.proxmox import ProxmoxError

router = APIRouter(tags=["web"])
templates = Jinja2Templates(directory="app/templates")
# every template can reference deployment identity (domains, URLs) without each
# route having to thread it through its context
templates.env.globals["settings"] = get_settings()

# status → (label, tone) for the status indicator
STATUS_TONE = {
    states.RUNNING: ("Running", "go"),
    states.PROVISIONING: ("Creating", "work"),
    states.BOOTING: ("Booting", "work"),
    states.STARTING: ("Starting", "work"),
    states.STOPPING: ("Stopping", "work"),
    states.REBOOTING: ("Rebooting", "work"),
    states.REBUILDING: ("Rebuilding", "work"),
    states.RESIZING: ("Resizing", "work"),
    states.RESTORING: ("Restoring", "work"),
    states.STOPPED: ("Stopped", "idle"),
    states.SUSPENDED: ("Suspended", "warn"),
    states.DESTROYING: ("Deleting", "warn"),
    states.ERROR: ("Error", "bad"),
}


def current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    uid = request.session.get("uid")
    return db.get(User, uid) if uid else None


def _require(request: Request, db: Session):
    user = current_user(request, db)
    return user


def _safe_next(nxt: str | None) -> str:
    # only allow same-origin relative paths (prevents open-redirect)
    return nxt if (nxt and nxt.startswith("/") and not nxt.startswith("//")) else "/"


def _login_redirect(request: Request) -> RedirectResponse:
    """Send a logged-out user to /login, remembering where they were headed (GET pages only —
    a POST path isn't revisitable after login)."""
    path = request.url.path
    if request.method == "GET" and path not in ("/", "/login", "/logout"):
        q = f"?{request.url.query}" if request.url.query else ""
        return RedirectResponse(f"/login?next={quote(path + q)}", status_code=303)
    return RedirectResponse("/login", status_code=303)


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = ""):
    return templates.TemplateResponse(request, "login.html", {"error": None, "next": _safe_next(next)})


@router.post("/login")
def login(
    request: Request,
    db: Session = Depends(get_db),
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form(""),
):
    user = db.scalar(select(User).where(User.email == email.strip().lower()))
    ok, upgraded = security.verify_password(password, user.password_hash if user else None)
    if not ok or user.status != "active":
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "That email and password don't match an account.", "next": _safe_next(next)},
            status_code=401,
        )
    if upgraded:  # transparently migrate legacy sha256 → bcrypt on successful login
        user.password_hash = upgraded
        db.commit()
    request.session["uid"] = user.id
    return RedirectResponse(_safe_next(next), status_code=303)


@router.get("/join/{token}", response_class=HTMLResponse)
def join_form(request: Request, token: str, db: Session = Depends(get_db)):
    invite = db.scalar(select(Invite).where(Invite.token == token, Invite.redeemed_by.is_(None)))
    if not invite:
        return templates.TemplateResponse(request, "join.html", {"invite": None, "token": token, "error": None})
    return templates.TemplateResponse(request, "join.html", {"invite": invite, "token": token, "error": None})


@router.post("/join/{token}")
def join_submit(
    request: Request,
    token: str,
    db: Session = Depends(get_db),
    email: str = Form(...),
    password: str = Form(...),
):
    invite = db.scalar(select(Invite).where(Invite.token == token, Invite.redeemed_by.is_(None)))
    email = email.strip().lower()
    err = None
    if not invite:
        err = "This invite has already been used or doesn't exist."
    elif invite.email and invite.email.strip().lower() != email:
        err = "This invite is pinned to a different email address."
    elif len(password) < 8:
        err = "Use a passphrase of at least 8 characters."
    elif db.scalar(select(User).where(User.email == email)):
        err = "There's already an account with that email."
    if err:
        return templates.TemplateResponse(
            request, "join.html", {"invite": invite, "token": token, "error": err}, status_code=422
        )
    user = User(email=email, password_hash=security.hash_password(password))
    db.add(user)
    db.flush()
    invite.redeemed_by = user.id
    invite.redeemed_at = utcnow()
    db.commit()
    request.session["uid"] = user.id
    return RedirectResponse("/", status_code=303)


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return _login_redirect(request)


@router.get("/favicon.ico", include_in_schema=False)
def favicon_ico():
    # browsers/tools that ignore the <link rel=icon> still probe this path
    return RedirectResponse("/static/favicon.svg", status_code=308)


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@router.get("/request-invite", response_class=HTMLResponse)
def request_invite_form(request: Request, sent: str = ""):
    """Public: the marketing site's 'Request an invite' lands here (nobody uses mailto)."""
    return templates.TemplateResponse(request, "request_invite.html", {"sent": bool(sent), "error": None})


@router.post("/request-invite")
def request_invite_submit(
    request: Request,
    db: Session = Depends(get_db),
    email: str = Form(""),
    message: str = Form(""),
    website: str = Form(""),  # honeypot — humans never see or fill it
):
    if website.strip():
        return RedirectResponse("/request-invite?sent=1", status_code=303)  # bot: pretend success
    email = email.strip().lower()[:255]
    if not _EMAIL_RE.fullmatch(email):
        return templates.TemplateResponse(
            request,
            "request_invite.html",
            {"sent": False, "error": "That doesn't look like an email address."},
            status_code=422,
        )
    # one pending request per address — repeat submits are a quiet no-op
    existing = db.scalar(select(InviteRequest).where(InviteRequest.email == email, InviteRequest.status == "pending"))
    if not existing:
        db.add(InviteRequest(email=email, message=message.strip()[:300] or None))
        db.commit()
        admins = db.scalars(select(User).where(User.is_admin.is_(True))).all()
        for admin in admins:
            if not admin.email.endswith("@" + get_settings().BASE_DOMAIN):  # system inboxes nobody reads
                mailer.send_invite_request_notice(admin.email, email, message.strip()[:300] or None)
    return RedirectResponse("/request-invite?sent=1", status_code=303)


@router.post("/invites/requests/{req_id}/approve")
def invite_request_approve(request: Request, req_id: int, db: Session = Depends(get_db)):
    user = _require(request, db)
    if not user:
        return _login_redirect(request)
    if not user.is_admin:
        return HTMLResponse("Admins only.", status_code=403)
    req = db.get(InviteRequest, req_id)
    if req and req.status == "pending":
        inv = Invite(token=security.new_invite_token(), email=req.email, note="requested via site", created_by=user.id)
        db.add(inv)
        req.status = "invited"
        db.commit()
        ok = mailer.send_invite(req.email, _invite_url(inv), None)
        return RedirectResponse(f"/invites?flash={'sent' if ok else 'sendfail'}", status_code=303)
    return RedirectResponse("/invites", status_code=303)


@router.post("/invites/requests/{req_id}/dismiss")
def invite_request_dismiss(request: Request, req_id: int, db: Session = Depends(get_db)):
    user = _require(request, db)
    if not user:
        return _login_redirect(request)
    if not user.is_admin:
        return HTMLResponse("Admins only.", status_code=403)
    req = db.get(InviteRequest, req_id)
    if req and req.status == "pending":
        req.status = "dismissed"
        db.commit()
    return RedirectResponse("/invites", status_code=303)


@router.get("/fav/{label}.png", include_in_schema=False)
def default_favicon(label: str):
    """Public: deterministic monogram favicon for a tenant site. The edge rewrites a
    tenant's 404ing /favicon.ico here so every site gets a tab icon by default."""
    label = label.lower()
    if not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", label):
        raise HTTPException(404)
    return Response(
        favicon_gen.monogram_png(label),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


_SETUP_SH = Path(__file__).resolve().parent.parent / "deploy" / "addons" / "setup.sh"


@router.get("/setup.sh")
def setup_script():
    # public add-on installer a user runs on their own server: curl … | sudo bash -s docker
    return PlainTextResponse(_SETUP_SH.read_text(), media_type="text/x-shellscript")


# ── password reset (email link) ────────────────────────────────────────────────
RESET_MAX_AGE = 3600  # 1 hour


def _reset_serializer() -> URLSafeTimedSerializer:
    secret = os.environ.get("SESSION_SECRET") or "warpyard-dev-secret"
    return URLSafeTimedSerializer(secret, salt="pw-reset")


def _reset_token(user: User) -> str:
    # embed a slice of the current hash so the link dies the moment the password changes (single-use)
    return _reset_serializer().dumps({"uid": user.id, "fp": user.password_hash[-16:]})


def _reset_user(db: Session, token: str) -> User | None:
    try:
        data = _reset_serializer().loads(token, max_age=RESET_MAX_AGE)
    except BadData:
        return None
    user = db.get(User, data.get("uid"))
    if not user or user.status != "active" or user.password_hash[-16:] != data.get("fp"):
        return None
    return user


@router.get("/reset", response_class=HTMLResponse)
def reset_request(request: Request, sent: str = ""):
    return templates.TemplateResponse(request, "reset_request.html", {"sent": bool(sent)})


@router.post("/reset")
def reset_send(request: Request, db: Session = Depends(get_db), email: str = Form(...)):
    user = db.scalar(select(User).where(User.email == email.strip().lower()))
    if user and user.status == "active":
        url = f"{get_settings().PUBLIC_URL.rstrip('/')}/reset/{_reset_token(user)}"
        mailer.send_password_reset(user.email, url)
    # identical response whether or not the email exists (no account enumeration)
    return RedirectResponse("/reset?sent=1", status_code=303)


@router.get("/reset/{token}", response_class=HTMLResponse)
def reset_form(request: Request, token: str, db: Session = Depends(get_db)):
    user = _reset_user(db, token)
    return templates.TemplateResponse(
        request, "reset_form.html", {"valid": user is not None, "token": token, "error": None}
    )


@router.post("/reset/{token}")
def reset_apply(request: Request, token: str, db: Session = Depends(get_db), password: str = Form(...)):
    user = _reset_user(db, token)
    if not user:
        return templates.TemplateResponse(request, "reset_form.html", {"valid": False, "token": token, "error": None})
    if len(password) < 8:
        return templates.TemplateResponse(
            request,
            "reset_form.html",
            {"valid": True, "token": token, "error": "Use a passphrase of at least 8 characters."},
            status_code=422,
        )
    user.password_hash = security.hash_password(password)
    db.commit()
    return RedirectResponse("/login?reset=1", status_code=303)


@router.get("/docs", response_class=HTMLResponse)
def api_docs(request: Request, db: Session = Depends(get_db)):
    # invite-only platform — the reference is for signed-in users, not anonymous visitors
    user = _require(request, db)
    if not user:
        return RedirectResponse("/login?next=/docs", status_code=303)
    return templates.TemplateResponse(request, "api_docs.html", {"user": user})


@router.get("/docs/platform", response_class=HTMLResponse)
def platform_docs(request: Request, db: Session = Depends(get_db)):
    # invite-only platform — the handbook is for signed-in users, not anonymous visitors
    user = _require(request, db)
    if not user:
        return RedirectResponse("/login?next=/docs/platform", status_code=303)
    return templates.TemplateResponse(request, "platform_docs.html", {"user": user})


def _account_ctx(user: User, db: Session, **extra) -> dict:
    from app.models import ApiKey, HttpRoute

    keys = db.scalars(select(ApiKey).where(ApiKey.user_id == user.id).order_by(ApiKey.id.desc())).all()
    # read-only overview of every custom domain across the user's servers (managed on each
    # server's page — a domain points to a specific server)
    rows = db.execute(
        select(HttpRoute, Instance)
        .join(Instance, HttpRoute.instance_id == Instance.id)
        .where(
            Instance.user_id == user.id,
            HttpRoute.kind == "custom",
            Instance.status.notin_(states.TERMINAL_STATES),
        )
        .order_by(HttpRoute.hostname)
    ).all()
    domains = [{"hostname": r.hostname, "status": r.status, "server": i.label, "server_id": i.id} for r, i in rows]
    my_invites = db.scalars(select(Invite).where(Invite.created_by == user.id).order_by(Invite.id.desc())).all()
    invites_left = None if user.is_admin else max(0, user.max_invites - len(my_invites))
    ctx = {
        "user": user,
        "error": None,
        "ok": False,
        "api_keys": keys,
        "new_key": None,
        "key_error": None,
        "domains": domains,
        "my_invites": my_invites,
        "invites_left": invites_left,
        "email_on": bool(get_settings().WARPYARD_RESEND_TOKEN),
        "base_url": get_settings().PUBLIC_URL.rstrip("/"),
        "partner_on": bool(get_settings().WARPYARD_POPPAPING_PARTNER_SECRET),
    }
    ctx.update(extra)
    return ctx


@router.post("/account/invites")
def account_invite_mint(request: Request, db: Session = Depends(get_db), email: str = Form(""), note: str = Form("")):
    """Members can invite a couple of friends; admins are unlimited (they also have /invites)."""
    user = _require(request, db)
    if not user:
        return _login_redirect(request)
    if not user.is_admin:
        used = db.scalar(select(func.count()).select_from(Invite).where(Invite.created_by == user.id)) or 0
        if used >= user.max_invites:
            return RedirectResponse("/account?inverr=1#invites", status_code=303)
    inv = Invite(
        token=security.new_invite_token(),
        email=(email.strip().lower() or None),
        note=(note.strip()[:120] or None),
        created_by=user.id,
    )
    db.add(inv)
    db.commit()
    if inv.email:
        mailer.send_invite(inv.email, _invite_url(inv), inv.note)
    return RedirectResponse("/account#invites", status_code=303)


@router.post("/account/poppaping/provision")
def account_poppaping_provision(request: Request, db: Session = Depends(get_db)):
    user = _require(request, db)
    if not user:
        return _login_redirect(request)
    secret = get_settings().WARPYARD_POPPAPING_PARTNER_SECRET
    if secret and not user.poppaping_api_key:
        try:
            key = poppaping.provision_account(secret, user.email)
        except poppaping.PoppaPingError as e:
            return RedirectResponse(f"/account?pperr={quote(e.message)}#monitoring", status_code=303)
        if key is None:
            msg = "A PoppaPing account with your email already exists — create an API key there and paste it here."
            return RedirectResponse(f"/account?pperr={quote(msg)}#monitoring", status_code=303)
        user.poppaping_api_key = key
        db.commit()
    return RedirectResponse("/account#monitoring", status_code=303)


@router.post("/account/poppaping")
def account_poppaping_key(
    request: Request, db: Session = Depends(get_db), api_key: str = Form(""), disconnect: str = Form("")
):
    user = _require(request, db)
    if not user:
        return _login_redirect(request)
    if disconnect:
        user.poppaping_api_key = None
    elif api_key.strip():
        user.poppaping_api_key = api_key.strip()[:100]
    db.commit()
    return RedirectResponse("/account#monitoring", status_code=303)


@router.get("/account", response_class=HTMLResponse)
def account_page(request: Request, db: Session = Depends(get_db)):
    user = _require(request, db)
    if not user:
        return _login_redirect(request)
    return templates.TemplateResponse(
        request, "account.html", _account_ctx(user, db, key_error=request.query_params.get("keyerr") or None)
    )


@router.post("/account/api-keys", response_class=HTMLResponse)
def api_key_create(request: Request, db: Session = Depends(get_db), name: str = Form("")):
    user = _require(request, db)
    if not user:
        return _login_redirect(request)
    from app.api_auth import generate_key
    from app.models import ApiKey

    full, prefix, key_hash = generate_key()
    db.add(ApiKey(user_id=user.id, name=(name.strip()[:64] or "api key"), prefix=prefix, key_hash=key_hash))
    db.commit()
    # show the full key exactly once
    return templates.TemplateResponse(request, "account.html", _account_ctx(user, db, new_key=full))


@router.post("/account/api-keys/{key_id}/delete")
def api_key_delete(request: Request, key_id: int, db: Session = Depends(get_db)):
    user = _require(request, db)
    if not user:
        return _login_redirect(request)
    from app.models import ApiKey

    key = db.get(ApiKey, key_id)
    if key and key.user_id == user.id:
        db.delete(key)
        db.commit()
    return RedirectResponse("/account", status_code=303)


@router.post("/account/password")
def change_password(request: Request, db: Session = Depends(get_db), current: str = Form(...), new: str = Form(...)):
    user = _require(request, db)
    if not user:
        return _login_redirect(request)
    ok, _ = security.verify_password(current, user.password_hash)
    error = None
    if not ok:
        error = "Your current password isn't right."
    elif len(new) < 8:
        error = "Use a new password of at least 8 characters."
    if error:
        return templates.TemplateResponse(request, "account.html", _account_ctx(user, db, error=error), status_code=422)
    user.password_hash = security.hash_password(new)
    db.commit()
    return templates.TemplateResponse(request, "account.html", _account_ctx(user, db, ok=True))


@router.post("/account/private-network")
def toggle_private_network(request: Request, db: Session = Depends(get_db), enabled: str = Form("")):
    user = _require(request, db)
    if not user:
        return _login_redirect(request)
    service.set_private_network(db, user, enabled == "on")
    return RedirectResponse("/account", status_code=303)


def _server(instance: Instance) -> dict:
    s = get_settings()
    label, tone = STATUS_TONE.get(instance.status, (instance.status, "idle"))
    ssh_map = next((m for m in instance.edge_mappings if m.protocol == "tcp" and m.target_port == 22), None)
    return {
        "id": instance.id,
        "label": instance.label,
        "hostname": instance.hostname or f"{instance.label}.{s.BASE_DOMAIN}",
        "status": instance.status,
        "status_label": label,
        "tone": tone,
        "ipv4": instance.ip.address if instance.ip else "—",  # private VLAN IP (internal only)
        "ssh": f"{s.EDGE_HOST}:{ssh_map.public_port}" if ssh_map else "—",  # public SSH endpoint
        "plan": instance.plan.slug,
        "image": instance.image.slug,
        "vmid": instance.vmid or "—",
        "busy": instance.status
        in {
            states.PROVISIONING,
            states.BOOTING,
            states.STARTING,
            states.STOPPING,
            states.REBOOTING,
            states.REBUILDING,
            states.RESIZING,
            states.RESTORING,
            states.DESTROYING,
        },
        "running": instance.status == states.RUNNING,
        "domains": [service.domain_json(r) for r in instance.http_routes if r.kind == "custom"],
        "deploy": service.deploy_info(instance),
        "connect": service.connect_info(instance),
        "image_guidance": instance.image.guidance,
        "backups_enabled": instance.backups_enabled,
        "last_backup_at": instance.last_backup_at,
        "shared": instance.shared,
        "shared_note": instance.shared_note or "",
        "restart_enabled": instance.restart_enabled,
        "restart_at": instance.restart_at or "",
        "monitor_id": instance.poppaping_monitor_id,
        "app_install_secs": _app_install_secs(instance),
        "addons_url": f"{s.PUBLIC_URL.rstrip('/')}/setup.sh",
        "pushdeploy_remote": (
            f"ssh://{s.SSH_LOGIN_USER}@{s.EDGE_HOST}:{ssh_map.public_port}/srv/site.git" if ssh_map else None
        ),
        # copy-paste one-liners run from the user's own machine (installer executes on the VM over SSH)
        "addon_setup": {
            name: (
                f"ssh -p {ssh_map.public_port} {s.SSH_LOGIN_USER}@{s.EDGE_HOST} "
                f'"curl -fsSL {s.PUBLIC_URL.rstrip("/")}/setup.sh | bash -s {name}"'
                if ssh_map
                else None
            )
            for name in ("docker", "pushdeploy")
        },
    }


# first-boot pull+setup window for one-click apps (minutes) — display heuristic like the game one
APP_INSTALL_MINUTES = {"ollama-webui": 10, "nextcloud": 6, "jellyfin": 5, "wordpress": 4, "ghost": 4}
APP_INSTALL_DEFAULT = 4


def _app_install_secs(instance: Instance) -> int:
    """Seconds left in an app's estimated first-boot setup (0 for non-apps / elapsed)."""
    if instance.image.category != "app" or instance.status != states.RUNNING or not instance.created_at:
        return 0
    mins = APP_INSTALL_MINUTES.get(instance.image.slug, APP_INSTALL_DEFAULT)
    created = instance.created_at if instance.created_at.tzinfo else instance.created_at.replace(tzinfo=UTC)
    return max(0, int(mins * 60 - (utcnow() - created).total_seconds()))


def _servers_and_stats(user: User, db: Session):
    rows = db.scalars(
        select(Instance)
        .where(Instance.user_id == user.id, Instance.status.notin_(states.TERMINAL_STATES))
        .order_by(Instance.id)
    ).all()
    servers = [_server(i) for i in rows]
    stats = {
        "total": len(servers),
        "running": sum(1 for b in servers if b["running"]),
        "limit": user.max_instances,
    }
    return servers, stats


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    user = _require(request, db)
    if not user:
        return _login_redirect(request)
    servers, stats = _servers_and_stats(user, db)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"user": user, "servers": servers, "stats": stats, "base_domain": get_settings().BASE_DOMAIN},
    )


@router.get("/board", response_class=HTMLResponse)
def board(request: Request, db: Session = Depends(get_db)):
    """Share board: every member's opted-in servers, listed only while they're running.
    Members-only (behind login) — nothing here is public."""
    user = _require(request, db)
    if not user:
        return _login_redirect(request)
    rows = db.scalars(
        select(Instance)
        .where(Instance.shared.is_(True), Instance.status == states.RUNNING)
        .order_by(Instance.id.desc())
    ).all()
    entries = [
        {
            "id": i.id,
            "label": i.label,
            "hostname": i.hostname,
            "url": f"https://{i.hostname}" if i.hostname else None,
            "owner": i.user.email.split("@")[0],
            "mine": i.user_id == user.id,
            "image": i.image.name,
            "category": i.image.category,
            "note": i.shared_note,
            "connect": service.connect_info(i),
            "comments": _board_comments_ctx(i, user),
        }
        for i in rows
    ]
    try:
        host = hoststats.snapshot()
    except ProxmoxError:
        host = None
    return templates.TemplateResponse(request, "board.html", {"user": user, "entries": entries, "host": host})


def _board_comments_ctx(instance: Instance, user: User, open_: bool = False) -> dict:
    """Template context for one listing's comment section (rendered inline on the board
    and re-rendered as the HTMX fragment after a post/delete — `open_` keeps the
    <details> expanded across those swaps)."""
    return {
        "iid": instance.id,
        "open": open_,
        "items": [
            {
                "id": c.id,
                "author": c.user.email.split("@")[0],
                "when": c.created_at.strftime("%b %d").replace(" 0", " "),
                "body": c.body,
                # a comment can be removed by its author or by the server's owner
                "can_delete": c.user_id == user.id or instance.user_id == user.id,
            }
            for c in instance.board_comments
        ],
    }


def _shared_instance_or_404(db: Session, iid: int) -> Instance:
    instance = db.get(Instance, iid)
    if instance is None or not instance.shared:
        raise HTTPException(status_code=404, detail="Not on the board")
    return instance


@router.post("/board/{iid}/comments", response_class=HTMLResponse)
def board_comment_add(iid: int, request: Request, body: str = Form(""), db: Session = Depends(get_db)):
    user = _require(request, db)
    if not user:
        return _login_redirect(request)
    instance = _shared_instance_or_404(db, iid)
    text = body.strip()
    if text:
        db.add(BoardComment(instance_id=instance.id, user_id=user.id, body=text[:500]))
        db.commit()
        db.refresh(instance)
    return templates.TemplateResponse(
        request, "_board_comments.html", {"user": user, "c": _board_comments_ctx(instance, user, open_=True)}
    )


@router.post("/board/{iid}/comments/{cid}/delete", response_class=HTMLResponse)
def board_comment_delete(iid: int, cid: int, request: Request, db: Session = Depends(get_db)):
    user = _require(request, db)
    if not user:
        return _login_redirect(request)
    instance = _shared_instance_or_404(db, iid)
    comment = db.get(BoardComment, cid)
    if comment and comment.instance_id == instance.id:
        if comment.user_id != user.id and instance.user_id != user.id:
            raise HTTPException(status_code=403, detail="Not your comment")
        db.delete(comment)
        db.commit()
        db.refresh(instance)
    return templates.TemplateResponse(
        request, "_board_comments.html", {"user": user, "c": _board_comments_ctx(instance, user, open_=True)}
    )


@router.get("/servers/cards", response_class=HTMLResponse)
def servers_cards(request: Request, db: Session = Depends(get_db)):
    """Live fragment the dashboard grid polls: the cards + an out-of-band tally update."""
    user = _require(request, db)
    if not user:
        return HTMLResponse("", status_code=204)
    servers, stats = _servers_and_stats(user, db)
    return templates.TemplateResponse(request, "_grid_live.html", {"servers": servers, "stats": stats})


@router.get("/new", response_class=HTMLResponse)
def new_form(request: Request, db: Session = Depends(get_db)):
    user = _require(request, db)
    if not user:
        return _login_redirect(request)
    plans = db.scalars(select(Plan).where(Plan.is_active.is_(True)).order_by(Plan.price_cents)).all()
    images = db.scalars(select(Image).where(Image.status == "active")).all()
    return templates.TemplateResponse(
        request,
        "new.html",
        {"user": user, "plans": plans, "images": images, "error": None, "certs_week": _certs_this_week(db)},
    )


def _certs_this_week(db: Session) -> int:
    """Estimate of Let's Encrypt certs issued for *.<BASE_DOMAIN> in the last 7 days — one per
    end-to-end-TLS server created (renewals not counted). Surfaced so users see how much of the
    50-certs/week LE limit is in play before turning the toggle on."""
    from datetime import datetime, timedelta

    from sqlalchemy import func

    since = datetime.now(UTC) - timedelta(days=7)
    return (
        db.scalar(
            select(func.count())
            .select_from(Instance)
            .where(Instance.tls_passthrough.is_(True), Instance.created_at >= since)
        )
        or 0
    )


@router.post("/new")
def new_submit(
    request: Request,
    db: Session = Depends(get_db),
    label: str = Form(...),
    plan: str = Form(...),
    image: str = Form(...),
    public_key: str = Form(""),
    key_name: str = Form(""),
    tls_e2e: str = Form(""),
    encrypt: str = Form(""),
):
    user = _require(request, db)
    if not user:
        return _login_redirect(request)
    err = None
    if public_key.strip():  # optional inline SSH key — added to the account before provisioning
        try:
            service.add_ssh_key(db, user, key_name or "key", public_key)
        except service.ServiceError as e:
            err = e.message
    if not err:
        # one source of truth: same validation/quotas/name-collision rules as the API + MCP
        try:
            service.create_server(db, user, label, plan, image, tls_passthrough=bool(tls_e2e), encrypted=bool(encrypt))
        except service.ServiceError as e:
            err = e.message
    if err:
        plans = db.scalars(select(Plan).where(Plan.is_active.is_(True)).order_by(Plan.price_cents)).all()
        images = db.scalars(select(Image).where(Image.status == "active")).all()
        return templates.TemplateResponse(
            request,
            "new.html",
            {"user": user, "plans": plans, "images": images, "error": err, "certs_week": _certs_this_week(db)},
            status_code=422,
        )
    return RedirectResponse("/", status_code=303)


@router.get("/servers/{instance_id}", response_class=HTMLResponse)
def server_detail(request: Request, instance_id: int, db: Session = Depends(get_db)):
    user = _require(request, db)
    if not user:
        return _login_redirect(request)
    instance = db.get(Instance, instance_id)
    if not instance or instance.user_id != user.id:
        return HTMLResponse("Not found", status_code=404)
    events = db.scalars(select(Event).where(Event.instance_id == instance_id).order_by(Event.id.desc()).limit(8)).all()
    s = get_settings()
    ssh_map = next((m for m in instance.edge_mappings if m.protocol == "tcp" and m.target_port == 22), None)
    access = {
        "web_url": f"https://{instance.hostname}" if instance.hostname else None,
        "ssh_cmd": (f"ssh -p {ssh_map.public_port} {s.SSH_LOGIN_USER}@{s.EDGE_HOST}" if ssh_map else None),
        "ssh_port": ssh_map.public_port if ssh_map else None,
        "key_names": [k.name for k in user.ssh_keys],
        "edge_host": s.EDGE_HOST,
    }
    snapshots = []
    with contextlib.suppress(service.ServiceError):
        snapshots = [
            {"name": s["name"], "descr": s["description"]} for s in service.list_snapshots(db, user, instance.id)
        ]
    backups, backups_err = [], None
    if instance.vmid and instance.backups_enabled:
        try:
            backups = service.list_backups(db, user, instance.id)
        except service.ServiceError as e:
            backups_err = e.message
    return templates.TemplateResponse(
        request,
        "server.html",
        {
            "user": user,
            "b": _server(instance),
            "events": events,
            "access": access,
            "snapshots": snapshots,
            "derr": request.query_params.get("derr", ""),
            "merr": request.query_params.get("merr", ""),
            "partner_on": bool(get_settings().WARPYARD_POPPAPING_PARTNER_SECRET),
            "backups": backups,
            "backups_err": backups_err,
        },
    )


@router.post("/servers/{instance_id}/domains")
def server_domain_add(request: Request, instance_id: int, db: Session = Depends(get_db), hostname: str = Form(...)):
    user = _require(request, db)
    if not user:
        return _login_redirect(request)
    try:
        service.add_domain(db, user, instance_id, hostname)
    except service.ServiceError as e:
        return RedirectResponse(f"/servers/{instance_id}?derr={quote(e.message)}#domains", status_code=303)
    return RedirectResponse(f"/servers/{instance_id}#domains", status_code=303)


@router.post("/servers/{instance_id}/domains/delete")
def server_domain_delete(request: Request, instance_id: int, db: Session = Depends(get_db), hostname: str = Form(...)):
    user = _require(request, db)
    if not user:
        return _login_redirect(request)
    with contextlib.suppress(service.ServiceError):
        service.remove_domain(db, user, instance_id, hostname)
    return RedirectResponse(f"/servers/{instance_id}#domains", status_code=303)


@router.get("/servers/{instance_id}/controls", response_class=HTMLResponse)
def server_controls(request: Request, instance_id: int, db: Session = Depends(get_db)):
    """Live fragment the detail page polls: power controls + an out-of-band status chip."""
    user = _require(request, db)
    if not user:
        return HTMLResponse("", status_code=204)
    instance = db.get(Instance, instance_id)
    if not instance or instance.user_id != user.id:
        return HTMLResponse("", status_code=204)
    return templates.TemplateResponse(request, "_server_controls.html", {"b": _server(instance)})


def _owned_running(request: Request, db: Session, instance_id: int) -> tuple[User | None, Instance | None]:
    user = _require(request, db)
    if not user:
        return None, None
    instance = db.get(Instance, instance_id)
    if not instance or instance.user_id != user.id:
        return user, None
    return user, instance


@router.post("/servers/{instance_id}/snapshot")
def snapshot_create(request: Request, instance_id: int, db: Session = Depends(get_db), name: str = Form("")):
    user, instance = _owned_running(request, db, instance_id)
    if not user:
        return _login_redirect(request)
    if instance:
        with contextlib.suppress(service.ServiceError):
            service.create_snapshot(db, user, instance_id, name)
    return RedirectResponse(f"/servers/{instance_id}#snapshots", status_code=303)


@router.post("/servers/{instance_id}/snapshot/{snapname}/delete")
def snapshot_delete(request: Request, instance_id: int, snapname: str, db: Session = Depends(get_db)):
    user, instance = _owned_running(request, db, instance_id)
    if not user:
        return _login_redirect(request)
    if instance:
        with contextlib.suppress(service.ServiceError):
            service.delete_snapshot(db, user, instance_id, snapname)
    return RedirectResponse(f"/servers/{instance_id}#snapshots", status_code=303)


@router.post("/servers/{instance_id}/snapshot/{snapname}/rollback")
def snapshot_rollback(request: Request, instance_id: int, snapname: str, db: Session = Depends(get_db)):
    user, instance = _owned_running(request, db, instance_id)
    if not user:
        return _login_redirect(request)
    if instance:
        with contextlib.suppress(service.ServiceError):
            service.rollback_snapshot(db, user, instance_id, snapname)
    return RedirectResponse(f"/servers/{instance_id}#snapshots", status_code=303)


@router.post("/servers/{instance_id}/backups/toggle")
def backups_toggle(request: Request, instance_id: int, db: Session = Depends(get_db)):
    user, instance = _owned_running(request, db, instance_id)
    if not user:
        return _login_redirect(request)
    if instance:
        with contextlib.suppress(service.ServiceError):
            service.set_backups(db, user, instance_id, not instance.backups_enabled)
    return RedirectResponse(f"/servers/{instance_id}#backups", status_code=303)


@router.post("/servers/{instance_id}/backups/now")
def backups_now(request: Request, instance_id: int, db: Session = Depends(get_db)):
    user, instance = _owned_running(request, db, instance_id)
    if not user:
        return _login_redirect(request)
    if instance:
        try:
            service.backup_now(db, user, instance_id)
        except service.ServiceError as e:
            return RedirectResponse(f"/servers/{instance_id}?derr={quote(e.message)}#backups", status_code=303)
    return RedirectResponse(f"/servers/{instance_id}#backups", status_code=303)


@router.post("/servers/{instance_id}/backups/restore")
def backups_restore(request: Request, instance_id: int, db: Session = Depends(get_db), volid: str = Form(...)):
    user, instance = _owned_running(request, db, instance_id)
    if not user:
        return _login_redirect(request)
    if instance:
        try:
            service.restore_backup(db, user, instance_id, volid)
        except service.ServiceError as e:
            return RedirectResponse(f"/servers/{instance_id}?derr={quote(e.message)}#backups", status_code=303)
    return RedirectResponse(f"/servers/{instance_id}#backups", status_code=303)


@router.get("/servers/{instance_id}/metrics.json")
def server_metrics(request: Request, instance_id: int, timeframe: str = "hour", db: Session = Depends(get_db)):
    """CPU/memory/network history for the owner's server (feeds the Metrics tab charts)."""
    user = _require(request, db)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        return JSONResponse(service.server_metrics(db, user, instance_id, timeframe))
    except service.ServiceError as e:
        if e.status == 502:
            return JSONResponse({"error": "unavailable"}, status_code=502)
        raise HTTPException(e.status, e.message) from e


@router.post("/servers/{instance_id}/monitor")
def monitor_create(request: Request, instance_id: int, db: Session = Depends(get_db)):
    user = _require(request, db)
    if not user:
        return _login_redirect(request)
    instance = db.get(Instance, instance_id)
    if not instance or instance.user_id != user.id:
        raise HTTPException(404)
    # this form is only offered to users who already connected a PoppaPing key — never
    # provision an account from here (that's the one-click /monitor/enable button)
    if not user.poppaping_api_key or instance.poppaping_monitor_id:
        return RedirectResponse(f"/servers/{instance_id}#monitoring", status_code=303)
    try:
        service.enable_monitoring(db, user, instance_id)
    except service.ServiceError as e:
        return RedirectResponse(f"/servers/{instance_id}?merr={quote(e.message)}#monitoring", status_code=303)
    return RedirectResponse(f"/servers/{instance_id}#monitoring", status_code=303)


@router.post("/servers/{instance_id}/monitor/enable")
def monitor_enable(request: Request, instance_id: int, db: Session = Depends(get_db)):
    """True one-click: no PoppaPing account needed — provision one for the user's email via
    the partner endpoint, store the key, then monitor this server."""
    user = _require(request, db)
    if not user:
        return _login_redirect(request)
    instance = db.get(Instance, instance_id)
    if not instance or instance.user_id != user.id:
        raise HTTPException(404)
    try:
        service.enable_monitoring(db, user, instance_id)
    except service.ServiceError as e:
        return RedirectResponse(f"/servers/{instance_id}?merr={quote(e.message)}#monitoring", status_code=303)
    return RedirectResponse(f"/servers/{instance_id}#monitoring", status_code=303)


@router.get("/servers/{instance_id}/monitor/data.json")
def monitor_data(request: Request, instance_id: int, period: str = "24h", db: Session = Depends(get_db)):
    """Uptime + response-time history for the Monitoring tab charts (owner-gated)."""
    user = _require(request, db)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if period not in service.MONITOR_PERIODS:
        period = "24h"
    try:
        return JSONResponse(service.monitoring_data(db, user, instance_id, period))
    except service.ServiceError as e:
        if e.status == 502:
            return JSONResponse({"error": "unavailable"}, status_code=502)
        if e.status == 404:
            return JSONResponse({"error": "not_monitored"}, status_code=404)
        raise HTTPException(e.status, e.message) from e


@router.post("/servers/{instance_id}/monitor/delete")
def monitor_delete(request: Request, instance_id: int, db: Session = Depends(get_db)):
    user = _require(request, db)
    if not user:
        return _login_redirect(request)
    instance = db.get(Instance, instance_id)
    if not instance or instance.user_id != user.id:
        raise HTTPException(404)
    with contextlib.suppress(service.ServiceError):
        service.disable_monitoring(db, user, instance_id)
    return RedirectResponse(f"/servers/{instance_id}#monitoring", status_code=303)


@router.post("/servers/{instance_id}/restart-schedule")
def restart_schedule(
    request: Request,
    instance_id: int,
    db: Session = Depends(get_db),
    enabled: str = Form(""),
    at: str = Form(""),
):
    user = _require(request, db)
    if not user:
        return _login_redirect(request)
    with contextlib.suppress(service.ServiceError):
        # a malformed time is dropped (the form's <input type=time> makes it near-impossible)
        valid_at = at if service.RESTART_AT_RE.match(at or "") else None
        service.set_restart_schedule(db, user, instance_id, enabled == "on", valid_at)
    return RedirectResponse(f"/servers/{instance_id}#access", status_code=303)


@router.post("/servers/{instance_id}/share")
def share_toggle(
    request: Request,
    instance_id: int,
    db: Session = Depends(get_db),
    enabled: str = Form(""),
    note: str = Form(""),
):
    user = _require(request, db)
    if not user:
        return _login_redirect(request)
    with contextlib.suppress(service.ServiceError):
        service.set_share(db, user, instance_id, enabled == "on", note)
    return RedirectResponse(f"/servers/{instance_id}#access", status_code=303)


def _resize_quota_error(db: Session, user: User, instance: Instance, new_plan: Plan) -> str | None:
    """Would resizing this instance to new_plan exceed the user's quota? (delta vs others)"""
    others = db.scalars(
        select(Instance).where(
            Instance.user_id == user.id,
            Instance.id != instance.id,
            Instance.status.notin_(states.TERMINAL_STATES),
        )
    ).all()
    vcpus = sum(i.plan.vcpus for i in others) + new_plan.vcpus
    disk = sum(i.plan.disk_gb for i in others) + new_plan.disk_gb
    if vcpus > user.max_vcpus or disk > user.max_disk_gb:
        return "That size would exceed your account limits."
    return None


@router.get("/servers/{instance_id}/resize", response_class=HTMLResponse)
def resize_form(request: Request, instance_id: int, db: Session = Depends(get_db)):
    user = _require(request, db)
    if not user:
        return _login_redirect(request)
    instance = db.get(Instance, instance_id)
    if not instance or instance.user_id != user.id:
        return HTMLResponse("Not found", status_code=404)
    # only bigger-or-equal-disk plans (disk is grow-only), excluding the current one
    options = db.scalars(
        select(Plan).where(Plan.is_active.is_(True), Plan.disk_gb >= instance.plan.disk_gb).order_by(Plan.price_cents)
    ).all()
    options = [p for p in options if p.id != instance.plan_id]
    return templates.TemplateResponse(
        request,
        "resize.html",
        {"user": user, "b": _server(instance), "current": instance.plan, "options": options, "error": None},
    )


@router.post("/servers/{instance_id}/resize")
def resize_submit(request: Request, instance_id: int, db: Session = Depends(get_db), plan: str = Form(...)):
    user = _require(request, db)
    if not user:
        return _login_redirect(request)
    instance = db.get(Instance, instance_id)
    if not instance or instance.user_id != user.id:
        return HTMLResponse("Not found", status_code=404)
    new_plan = db.scalar(select(Plan).where(Plan.slug == plan, Plan.is_active.is_(True)))
    err = None
    if new_plan is None or new_plan.id == instance.plan_id:
        err = "Pick a different size."
    elif new_plan.disk_gb < instance.plan.disk_gb:
        err = "A server's disk can only grow, not shrink."
    elif not states.can_enqueue("instance.resize", instance.status):
        err = f"Can't resize a server that's {instance.status}."
    else:
        err = _resize_quota_error(db, user, instance, new_plan)
    if err:
        options = db.scalars(
            select(Plan)
            .where(Plan.is_active.is_(True), Plan.disk_gb >= instance.plan.disk_gb)
            .order_by(Plan.price_cents)
        ).all()
        options = [p for p in options if p.id != instance.plan_id]
        return templates.TemplateResponse(
            request,
            "resize.html",
            {"user": user, "b": _server(instance), "current": instance.plan, "options": options, "error": err},
            status_code=422,
        )
    try:
        queue.enqueue(db, "instance.resize", instance_id=instance.id, payload={"plan_id": new_plan.id})
    except queue.JobConflict as e:
        raise HTTPException(409, str(e)) from e
    db.add(
        Event(
            user_id=user.id,
            instance_id=instance.id,
            action="instance.resize",
            status="started",
            detail={"to": new_plan.slug},
        )
    )
    db.commit()
    return RedirectResponse(f"/servers/{instance.id}", status_code=303)


ACTIONS = {
    "start": "instance.start",
    "stop": "instance.stop",
    "reboot": "instance.reboot",
    "rebuild": "instance.rebuild",
    "destroy": "instance.destroy",
}


@router.post("/servers/{instance_id}/{action}", response_class=HTMLResponse)
def server_action(request: Request, instance_id: int, action: str, db: Session = Depends(get_db)):
    user = _require(request, db)
    if not user:
        return _login_redirect(request)
    instance = db.get(Instance, instance_id)
    verb = ACTIONS.get(action)
    if not instance or instance.user_id != user.id or not verb:
        return HTMLResponse("Not found", status_code=404)
    if states.can_enqueue(verb, instance.status):
        try:
            queue.enqueue(db, verb, instance_id=instance.id)
            db.add(Event(user_id=user.id, instance_id=instance.id, action=verb, status="started", detail={}))
            db.commit()
        except queue.JobConflict:
            db.rollback()
    # return the updated card fragment for HTMX swap
    db.refresh(instance)
    return templates.TemplateResponse(request, "_card.html", {"b": _server(instance)})


# SSH keys live on the Account page (under API keys). /keys stays as a redirect for old links.
@router.get("/keys")
def keys_page(request: Request):
    return RedirectResponse("/account", status_code=307)


@router.post("/keys")
def keys_add(request: Request, db: Session = Depends(get_db), name: str = Form(...), public_key: str = Form(...)):
    user = _require(request, db)
    if not user:
        return _login_redirect(request)
    try:
        service.add_ssh_key(db, user, name, public_key)
    except service.ServiceError as e:
        return RedirectResponse(f"/account?keyerr={quote(e.message)}", status_code=303)
    return RedirectResponse("/account", status_code=303)


@router.post("/keys/{key_id}/delete")
def keys_delete(request: Request, key_id: int, db: Session = Depends(get_db)):
    user = _require(request, db)
    if not user:
        return _login_redirect(request)
    key = db.get(SshKey, key_id)
    if key and key.user_id == user.id:
        db.delete(key)
        db.commit()
    return RedirectResponse("/account", status_code=303)


# ---- platform host metrics (members; per-VM detail follows the Board share model) ----
@router.get("/host", response_class=HTMLResponse)
def host_page(request: Request, db: Session = Depends(get_db)):
    """Live metrics for the hypervisor that runs the whole tenant fleet."""
    user = _require(request, db)
    if not user:
        return _login_redirect(request)
    try:
        ov = hoststats.overview(db, user)
    except ProxmoxError:
        ov = None
    return templates.TemplateResponse(request, "host.html", {"user": user, "nav": "host", "ov": ov})


@router.get("/host/overview", response_class=HTMLResponse)
def host_overview(request: Request, db: Session = Depends(get_db)):
    """HTMX poll fragment: stat tiles + (OOB) composition/consumers/storage/VM sections."""
    user = _require(request, db)
    if not user:
        return HTMLResponse("Sign in first.", status_code=401)
    try:
        ov = hoststats.overview(db, user)
    except ProxmoxError:
        ov = None
    return templates.TemplateResponse(request, "_host_overview.html", {"ov": ov, "oob": True})


@router.get("/host/metrics.json")
def host_metrics(request: Request, timeframe: str = "hour", db: Session = Depends(get_db)):
    """Node CPU/load/memory/network/pressure history (feeds the Host page charts)."""
    user = _require(request, db)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if timeframe not in hoststats.TIMEFRAMES:
        raise HTTPException(400, "timeframe must be hour, day or week")
    try:
        return JSONResponse(hoststats.series(timeframe))
    except ProxmoxError:
        return JSONResponse({"error": "unavailable"}, status_code=502)


# ---- admin: create invites ----
def _invite_url(inv: Invite) -> str:
    # always use the configured public URL, not however the admin reached the panel
    return f"{get_settings().PUBLIC_URL.rstrip('/')}/join/{inv.token}"


@router.get("/invites", response_class=HTMLResponse)
def invites_page(request: Request, db: Session = Depends(get_db), flash: str = ""):
    user = _require(request, db)
    if not user:
        return _login_redirect(request)
    if not user.is_admin:
        return HTMLResponse("Admins only.", status_code=403)
    invites = db.scalars(select(Invite).order_by(Invite.id.desc()).limit(50)).all()
    rows = [
        {
            "id": inv.id,
            "note": inv.note or "—",
            "email": inv.email or "anyone",
            "has_email": inv.email is not None,
            "url": _invite_url(inv),
            "redeemed": inv.redeemed_by is not None,
        }
        for inv in invites
    ]
    pending = db.scalars(
        select(InviteRequest).where(InviteRequest.status == "pending").order_by(InviteRequest.id)
    ).all()
    return templates.TemplateResponse(
        request,
        "invites.html",
        {
            "user": user,
            "invites": rows,
            "requests": pending,
            "email_on": bool(get_settings().WARPYARD_RESEND_TOKEN),
            "flash": flash,
        },
    )


@router.post("/invites")
def invites_mint(
    request: Request,
    db: Session = Depends(get_db),
    note: str = Form(""),
    email: str = Form(""),
):
    user = _require(request, db)
    if not user:
        return _login_redirect(request)
    if not user.is_admin:
        return HTMLResponse("Admins only.", status_code=403)
    inv = Invite(
        token=security.new_invite_token(),
        email=(email.strip().lower() or None),
        note=(note.strip()[:120] or None),
        created_by=user.id,
    )
    db.add(inv)
    db.commit()
    flash = ""
    if inv.email:  # pinned to an address → email the link straight away
        flash = "sent" if mailer.send_invite(inv.email, _invite_url(inv), inv.note) else "sendfail"
    return RedirectResponse(f"/invites?flash={flash}" if flash else "/invites", status_code=303)


@router.post("/invites/{invite_id}/send")
def invite_send(request: Request, invite_id: int, db: Session = Depends(get_db)):
    user = _require(request, db)
    if not user:
        return _login_redirect(request)
    if not user.is_admin:
        return HTMLResponse("Admins only.", status_code=403)
    inv = db.get(Invite, invite_id)
    if not inv or not inv.email or inv.redeemed_by is not None:
        return RedirectResponse("/invites", status_code=303)
    ok = mailer.send_invite(inv.email, _invite_url(inv), inv.note)
    return RedirectResponse(f"/invites?flash={'sent' if ok else 'sendfail'}", status_code=303)
