"""Rate limiting on the unauthenticated auth endpoints — /login and /reset."""

import pytest

from app import ratelimit


@pytest.fixture(autouse=True)
def _fresh_limits():
    ratelimit.reset()
    yield
    ratelimit.reset()


def test_allow_window_and_limit(monkeypatch):
    assert all(ratelimit.allow("reset", "1.2.3.4") for _ in range(ratelimit.LIMITS["reset"]))
    assert ratelimit.allow("reset", "1.2.3.4") is False  # over the limit
    assert ratelimit.allow("reset", "5.6.7.8") is True  # other clients unaffected
    assert ratelimit.allow("login", "1.2.3.4") is True  # other buckets unaffected
    # window expiry: age out the old attempts and the client is welcome again
    now = ratelimit.time.monotonic()
    monkeypatch.setattr(ratelimit.time, "monotonic", lambda: now + ratelimit.WINDOW + 1)
    assert ratelimit.allow("reset", "1.2.3.4") is True


def test_login_throttles_after_repeated_failures(client, db, seeded):
    for _ in range(ratelimit.LIMITS["login"]):
        r = client.post("/login", data={"email": "nobody@warpyard.test", "password": "wrong"})
        assert r.status_code == 401
    r = client.post("/login", data={"email": "nobody@warpyard.test", "password": "wrong"})
    assert r.status_code == 429
    assert "Too many attempts" in r.text


def test_reset_throttle_keeps_identical_response(client, db, seeded):
    for _ in range(ratelimit.LIMITS["reset"] + 3):
        r = client.post("/reset", data={"email": "nobody@warpyard.test"}, follow_redirects=False)
        # throttled or not, the response never changes (no enumeration, no oracle)
        assert r.status_code == 303 and r.headers["location"] == "/reset?sent=1"


def test_client_key_prefers_proxy_appended_xff():
    class Req:
        headers = {"x-forwarded-for": "6.6.6.6, 203.0.113.9"}
        client = type("C", (), {"host": "10.10.66.2"})()

    # the LAST entry is what our own edge appended — the spoofable first entry is ignored
    assert ratelimit.client_key(Req()) == "203.0.113.9"
