"""Generated default favicons: valid deterministic PNGs, public route, label hygiene."""

from app import favicon_gen


def test_monogram_is_valid_deterministic_png():
    a = favicon_gen.monogram_png("jsmc")
    b = favicon_gen.monogram_png("jsmc")
    c = favicon_gen.monogram_png("futures")
    assert a[:8] == b"\x89PNG\r\n\x1a\n"
    assert a == b  # same label -> same icon, always
    assert a != c  # different labels -> different icons


def test_every_glyph_renders():
    for ch in "abcdefghijklmnopqrstuvwxyz0123456789":
        out = favicon_gen.monogram_png(ch + "x")
        assert out[:8] == b"\x89PNG\r\n\x1a\n"


def test_route_is_public_and_sane(client):
    r = client.get("/fav/jsmc.png")  # no login
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert "max-age" in r.headers.get("cache-control", "")
    assert client.get("/fav/Bad_Label!.png").status_code == 404
    assert client.get("/fav/-leadinghyphen.png").status_code == 404
