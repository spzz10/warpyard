"""In-process rate limiting for the unauthenticated auth endpoints (/login, /reset).

Deliberately simple: a sliding window per (bucket, client) held in process memory —
correct for the documented single-process deployment (docs/ARCHITECTURE.md). bcrypt
cost and non-enumerating responses do most of the defensive work; this just turns
unthrottled online guessing into a dead end."""

import time
from collections import defaultdict, deque

from fastapi import Request

WINDOW = 300.0  # seconds
LIMITS = {"login": 15, "reset": 5}  # attempts per client per window
_MAX_KEYS = 10_000  # memory backstop — prune expired entries past this

_hits: dict[tuple[str, str], deque] = defaultdict(deque)


def client_key(request: Request) -> str:
    """The client's address for throttling. Behind the edge, Caddy appends the real
    client to X-Forwarded-For, so the LAST entry is the one our own proxy wrote;
    direct (LAN) requests fall back to the socket peer."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


def allow(bucket: str, key: str) -> bool:
    """Record one attempt; False when the client is over the bucket's limit."""
    now = time.monotonic()
    cutoff = now - WINDOW
    if len(_hits) > _MAX_KEYS:
        for k in [k for k, q in _hits.items() if not q or q[-1] < cutoff]:
            del _hits[k]
    q = _hits[(bucket, key)]
    while q and q[0] < cutoff:
        q.popleft()
    if len(q) >= LIMITS[bucket]:
        return False
    q.append(now)
    return True


def reset() -> None:
    """Test helper — forget all recorded attempts."""
    _hits.clear()
