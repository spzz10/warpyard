"""Password hashing + invite tokens. bcrypt via passlib; legacy sha256 hashes are
verified and transparently upgraded on next login (so the dev seed keeps working)."""

import hashlib
import secrets

from passlib.context import CryptContext

_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(pw: str) -> str:
    return _ctx.hash(pw)


def verify_password(pw: str, stored: str | None) -> tuple[bool, str | None]:
    """Returns (ok, new_hash). new_hash is set when the stored hash should be
    upgraded (legacy sha256 → bcrypt) so the caller can persist it."""
    if not stored:
        return False, None
    # legacy dev hashes: bare 64-char hex sha256
    if len(stored) == 64 and all(c in "0123456789abcdef" for c in stored):
        if hashlib.sha256(pw.encode()).hexdigest() == stored:
            return True, hash_password(pw)
        return False, None
    ok = _ctx.verify(pw, stored)
    return ok, (hash_password(pw) if ok and _ctx.needs_update(stored) else None)


def new_invite_token() -> str:
    return secrets.token_urlsafe(24)
