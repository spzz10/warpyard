"""API-key auth for the public REST API. Keys look like `wy_<secret>`; only their sha256
is stored. `api_user` is a FastAPI dependency that resolves the Bearer key to a User."""

import hashlib
import secrets

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import ApiKey, User, utcnow

KEY_PREFIX = "wy_"


def generate_key() -> tuple[str, str, str]:
    """Return (full_key, prefix, hash). full_key is shown to the user once."""
    secret = secrets.token_urlsafe(32)
    full = f"{KEY_PREFIX}{secret}"
    return full, full[:12], hashlib.sha256(full.encode()).hexdigest()


def _hash(full_key: str) -> str:
    return hashlib.sha256(full_key.encode()).hexdigest()


def api_user(authorization: str = Header(None), db: Session = Depends(get_db)) -> User:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing bearer token. Use: Authorization: Bearer wy_...")
    token = authorization.removeprefix("Bearer ").strip()
    row = db.scalar(select(ApiKey).where(ApiKey.key_hash == _hash(token)))
    if row is None or row.user.status != "active":
        raise HTTPException(401, "Invalid or revoked API key.")
    row.last_used_at = utcnow()
    db.commit()
    return row.user
