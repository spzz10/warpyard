"""Edge sync API — the edge box's Caddy agent pulls its routing table from here.

Pull model (edge never needs inbound from the control plane): the edge agent polls
GET /edge/routes with a shared bearer token, renders Caddy snippets + the on-demand
authorizer allowlist, and reloads Caddy only when the content hash changes.

HTTP routes come from `http_routes`; raw TCP/UDP forwards from `edge_mappings`
(applied by the agent via a layer4 config — HTTP is what ships first).
"""

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import states
from app.config import get_settings
from app.database import get_db
from app.models import EdgeMapping, HttpRoute, Instance

router = APIRouter(prefix="/edge", tags=["edge"])


def _auth(authorization: str = Header(None)):
    token = get_settings().EDGE_SYNC_TOKEN
    if not token or not hmac.compare_digest(authorization or "", f"Bearer {token}"):
        raise HTTPException(401, "bad edge sync token")


@router.get("/routes", dependencies=[Depends(_auth)])
def routes(db: Session = Depends(get_db)):
    """Current desired edge state. Only routes for live instances with an assigned IP."""
    http_rows = db.execute(
        select(HttpRoute, Instance)
        .join(Instance, HttpRoute.instance_id == Instance.id)
        .where(Instance.status.notin_(states.TERMINAL_STATES), HttpRoute.status == "active")
    ).all()
    http = []
    for route, instance in http_rows:
        if instance.ip is None:
            continue
        # passthrough = the VM terminates its own TLS: the edge SNI-passes :443 straight to the
        # VM (never decrypts) and redirects :80→https. Otherwise the edge terminates and proxies
        # to the VM's :80 (legacy behaviour). One flag drives the agent's layer4 vs http rendering.
        # A gated instance MUST be edge-terminated (the edge has to see the request to forward-auth
        # it), so `gated` overrides passthrough while on.
        http.append(
            {
                "hostname": route.hostname,
                "upstream": f"{instance.ip.address}:{route.target_port}",
                "passthrough": bool(instance.tls_passthrough) and not instance.gated,
                "gated": bool(instance.gated),
                "https_upstream": f"{instance.ip.address}:443",
            }
        )

    tcp_rows = db.execute(
        select(EdgeMapping, Instance)
        .join(Instance, EdgeMapping.instance_id == Instance.id)
        .where(Instance.status.notin_(states.TERMINAL_STATES))
    ).all()
    l4 = [
        {
            "protocol": m.protocol,
            "public_port": m.public_port,
            "upstream": f"{instance.ip.address}:{m.target_port}",
        }
        for m, instance in tcp_rows
        if instance.ip is not None
    ]
    return {"http": http, "l4": l4}
