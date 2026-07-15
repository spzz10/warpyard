from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings, session_secret
from app.console import router as console_router
from app.mcp_server import mcp, mcp_app
from app.oauth import router as oauth_router
from app.routes.api_v1 import router as api_v1_router
from app.routes.edge import router as edge_router
from app.routes.instances import router as instances_router
from app.web import router as web_router

_STATIC = Path(__file__).parent / "static"
_settings = get_settings()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # The MCP Streamable-HTTP session manager must run for the mounted /mcp app to work.
    async with mcp.session_manager.run():
        yield


app = FastAPI(
    title="Warpyard",
    description=f"Community VM leasing — control plane API. Instances get <label>.{_settings.BASE_DOMAIN} out of the box.",
    version="0.1.0",
    lifespan=lifespan,
    docs_url=None,  # the auto Swagger UI exposes internal routes; we ship a curated /docs page instead
    redoc_url=None,
    openapi_url=None,  # and the raw schema itself — it maps the internal /instances, /edge routes
)
# Session cookie for the dashboard. SESSION_SECRET should be set in prod; a random
# per-process secret is fine for dev (just logs everyone out on restart).
# https_only: the cookie is Secure — sessions only work over HTTPS (the public dashboard URL);
# plain-HTTP LAN access to :8000 still renders pages but won't hold a login.
app.add_middleware(
    SessionMiddleware,
    secret_key=session_secret(),
    https_only=True,
)
app.include_router(instances_router)
app.include_router(edge_router)
app.include_router(web_router)
app.include_router(console_router)
app.include_router(api_v1_router)
app.include_router(oauth_router)
app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")


@app.get("/healthz", tags=["meta"])
def healthz():
    return {"status": "ok"}


# A browser hitting the MCP endpoint directly gets an OAuth 401 (the dashboard session cookie
# doesn't authenticate here — the AI client does the OAuth dance). Show a friendly page for
# that case instead of a raw JSON error. Pure-ASGI (not BaseHTTPMiddleware) so it never buffers
# the MCP SSE stream — it only short-circuits a browser navigation and passes everything else
# through untouched.
_MCP_LANDING = """<!doctype html><meta charset="utf-8"><title>Warpyard MCP</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
 body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0a0c10;color:#e7eaf0;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,system-ui,sans-serif;
  background-image:radial-gradient(900px 340px at 50% -140px,rgba(106,125,255,.10),transparent 70%)}
 .card{max-width:480px;padding:2.4rem;text-align:center}
 .mark{width:46px;height:46px;border-radius:12px;margin:0 auto 1.1rem;display:grid;place-items:center;
  background:linear-gradient(150deg,#8a99ff,#4c5bf0);box-shadow:0 10px 30px -10px #6a7dff}
 .mark svg{width:26px;height:26px}
 h1{font-size:1.4rem;font-weight:600;margin:0 0 .5rem}
 p{color:#97a1b2;line-height:1.6;margin:0 0 1rem}
 code{font-family:ui-monospace,Menlo,monospace;background:#161922;border:1px solid #2b313c;color:#93a2ff;
  padding:.55rem .7rem;border-radius:8px;display:block;word-break:break-all;margin:0 0 1.3rem}
 .btns{display:flex;gap:.6rem;justify-content:center;flex-wrap:wrap}
 a.btn{text-decoration:none;font-weight:600;font-size:.9rem;padding:.6rem 1.1rem;border-radius:9px}
 a.pri{background:linear-gradient(180deg,#7c8bff,#5a6dff);color:#fff}
 a.gho{border:1px solid #2b313c;color:#e7eaf0}
 small{color:#616a79;display:block;margin-top:1.4rem;line-height:1.5}
</style>
<div class="card">
  <div class="mark"><svg viewBox="0 0 24 24"><path d="M4.5 8 8 17 12 5 16 17 19.5 8" fill="none" stroke="#fff" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/></svg></div>
  <h1>This is an MCP endpoint</h1>
  <p>You've reached Warpyard's MCP server. It's not meant to be opened in a browser — add this URL
     to Claude or Cursor and your AI can manage your servers.</p>
  <code>__MCP_URL__/mcp</code>
  <div class="btns">
    <a class="btn pri" href="__PUBLIC_URL__/docs">How to connect</a>
    <a class="btn gho" href="__PUBLIC_URL__/account">Your account</a>
  </div>
  <small>Seeing a login error? That's expected — a browser can't authenticate here.
    Your AI client does the sign-in for you.</small>
</div>"""
_MCP_LANDING = _MCP_LANDING.replace("__MCP_URL__", _settings.MCP_URL.rstrip("/")).replace(
    "__PUBLIC_URL__", _settings.PUBLIC_URL.rstrip("/")
)


class MCPBrowserLanding:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] == "GET" and scope["path"] == "/mcp":
            headers = dict(scope.get("headers") or [])
            accept = headers.get(b"accept", b"").decode("latin-1")
            if "text/html" in accept and b"authorization" not in headers:
                await HTMLResponse(_MCP_LANDING)(scope, receive, send)
                return
        await self.app(scope, receive, send)


app.add_middleware(MCPBrowserLanding)


class SecurityHeaders:
    """Defense-in-depth response headers. Pure-ASGI (not BaseHTTPMiddleware, which
    buffers and would break the MCP SSE stream) — only rewrites the response-start
    headers, never the body. CSP is intentionally just frame-ancestors: a script/style
    policy would break the inline styles + HTMX attributes the templates rely on."""

    _ADD = [
        (b"x-frame-options", b"DENY"),
        (b"x-content-type-options", b"nosniff"),
        (b"referrer-policy", b"same-origin"),
        (b"content-security-policy", b"frame-ancestors 'none'"),
    ]

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                present = {k.lower() for k, _ in headers}
                headers.extend((k, v) for k, v in self._ADD if k not in present)
            await send(message)

        await self.app(scope, receive, send_wrapper)


app.add_middleware(SecurityHeaders)


_NOTFOUND_HTML = """<!doctype html><meta charset="utf-8"><title>Not found — Warpyard</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
 body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0a0c10;color:#e7eaf0;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,system-ui,sans-serif;
  background-image:radial-gradient(900px 340px at 50% -140px,rgba(106,125,255,.10),transparent 70%)}
 .card{max-width:420px;padding:2.4rem;text-align:center}
 .mark{width:46px;height:46px;border-radius:12px;margin:0 auto 1.1rem;display:grid;place-items:center;
  background:linear-gradient(150deg,#8a99ff,#4c5bf0);box-shadow:0 10px 30px -10px #6a7dff}
 .mark svg{width:26px;height:26px}
 h1{font-size:1.4rem;font-weight:600;margin:0 0 .5rem}
 p{color:#97a1b2;line-height:1.6;margin:0 0 1.4rem}
 a.btn{text-decoration:none;font-weight:600;font-size:.9rem;padding:.6rem 1.1rem;border-radius:9px;
  background:linear-gradient(180deg,#7c8bff,#5a6dff);color:#fff}
</style>
<div class="card">
  <div class="mark"><svg viewBox="0 0 24 24"><path d="M4.5 8 8 17 12 5 16 17 19.5 8" fill="none" stroke="#fff" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/></svg></div>
  <h1>Page not found</h1>
  <p>That page doesn't exist — it may have been removed, or the address has a typo.</p>
  <a class="btn" href="/">Back to your servers</a>
</div>"""


class BrandedNotFound:
    """Replace bare 404s with a branded page for browser navigations. Pure ASGI; JSON/API
    clients, static files and the MCP stream are passed through untouched."""

    _SKIP = ("/api/", "/mcp", "/static/", "/edge/", "/instances")

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "GET" or scope["path"].startswith(self._SKIP):
            await self.app(scope, receive, send)
            return
        accept = dict(scope.get("headers") or []).get(b"accept", b"").decode("latin-1")
        if "text/html" not in accept:
            await self.app(scope, receive, send)
            return
        swallow = False

        async def send_wrapper(message):
            nonlocal swallow
            if message["type"] == "http.response.start" and message["status"] == 404:
                swallow = True
                await HTMLResponse(_NOTFOUND_HTML, status_code=404)(scope, receive, send)
                return
            if not swallow:
                await send(message)

        await self.app(scope, receive, send_wrapper)


app.add_middleware(BrandedNotFound)


# Mounted LAST so it only catches paths no FastAPI route claimed (i.e. /mcp). The MCP app
# validates OAuth Bearer tokens itself and serves the protected-resource metadata.
app.mount("/", mcp_app)
