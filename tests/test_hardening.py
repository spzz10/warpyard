"""External-pentest hardening: schema disclosure off, security headers on,
OAuth dynamic registration throttled."""

from app import ratelimit


def test_openapi_schema_disabled(client):
    # /openapi.json maps the internal /instances + /edge routes — must not be served
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/docs", follow_redirects=False).status_code == 303  # curated /docs is login-gated
    assert client.get("/redoc").status_code == 404


def test_security_headers_present(client):
    r = client.get("/login")
    assert r.headers.get("x-frame-options") == "DENY"
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert r.headers.get("referrer-policy") == "same-origin"
    assert "frame-ancestors 'none'" in r.headers.get("content-security-policy", "")


def test_security_headers_do_not_clobber_existing(client):
    # a normal page still renders (headers are additive, body untouched)
    r = client.get("/login")
    assert r.status_code == 200 and b"<form" in r.content


def test_oauth_register_is_rate_limited(client):
    body = {"redirect_uris": ["http://localhost:9999/cb"], "client_name": "probe"}
    for _ in range(ratelimit.LIMITS["oauth_reg"]):
        assert client.post("/oauth/register", json=body).status_code == 201
    r = client.post("/oauth/register", json=body)
    assert r.status_code == 429  # further registrations throttled


def test_oauth_register_still_works_within_limit(client):
    r = client.post("/oauth/register", json={"redirect_uris": ["http://localhost:1/cb"]})
    assert r.status_code == 201 and r.json()["client_id"].startswith("wyc_")
