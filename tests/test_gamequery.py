"""Wire-format parsers, A2S challenge round-trip, and the TTL cache — no network."""

import json
import socket
import struct
import time

from app import gamequery
from app.gamequery import (
    _a2s_exchange,
    _decode_varint,
    _encode_varint,
    parse_a2s_info,
    parse_bedrock_pong,
    parse_minecraft_response,
    players_for_slug,
)


def setup_function():
    gamequery.clear_cache()


# ---- varints ----


def test_varint_roundtrip():
    for n in (0, 1, 127, 128, 300, 2**21, -1):
        buf = _encode_varint(n)
        val, off = _decode_varint(buf, 0)
        assert off == len(buf)
        assert val == (n & 0xFFFFFFFF)


# ---- minecraft ----


def _mc_packet(status: dict) -> bytes:
    payload = json.dumps(status).encode()
    return b"\x00" + _encode_varint(len(payload)) + payload


def test_minecraft_parse():
    pkt = _mc_packet(
        {
            "players": {"online": 3, "max": 20},
            "version": {"name": "1.21.6"},
            "description": {"text": "A Warpyard server"},
        }
    )
    assert parse_minecraft_response(pkt) == {
        "online": 3,
        "max": 20,
        "version": "1.21.6",
        "name": "A Warpyard server",
    }


def test_minecraft_parse_rejects_bad():
    assert parse_minecraft_response(_mc_packet({"no": "players"})) is None
    assert parse_minecraft_response(b"\x05" + b"junk") is None  # wrong packet id


# ---- a2s ----


def _a2s_source_reply(name: str, online: int, maxp: int) -> bytes:
    body = b"\x49\x11" + name.encode() + b"\x00" + b"de_dust2\x00cstrike\x00Counter-Strike\x00"
    body += struct.pack("<H", 10) + bytes([online, maxp, 0])
    return b"\xff\xff\xff\xff" + body


def test_a2s_parse_source():
    data = _a2s_source_reply("cs16 fun", 5, 32)
    assert parse_a2s_info(data[4:]) == {"online": 5, "max": 32, "name": "cs16 fun"}


def test_a2s_parse_goldsrc_legacy():
    body = b"\x6d" + b"1.2.3.4:27015\x00srv\x00de_aztec\x00valve\x00Half-Life\x00" + bytes([2, 16])
    assert parse_a2s_info(body) == {"online": 2, "max": 16, "name": "srv"}


class _FakeUdp:
    """Scripted UDP socket: queued recv() payloads, records sendto() calls."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.sent = []

    def sendto(self, data, addr):
        self.sent.append(data)

    def recv(self, n):
        if not self.replies:
            raise TimeoutError
        return self.replies.pop(0)


def test_a2s_challenge_roundtrip():
    challenge = b"\xff\xff\xff\xff\x41ABCD"
    sock = _FakeUdp([challenge, _a2s_source_reply("x", 1, 10)])
    out = _a2s_exchange(sock, ("h", 1))
    assert out == {"online": 1, "max": 10, "name": "x"}
    assert sock.sent[1].endswith(b"ABCD")  # challenge bytes appended on retry


def test_a2s_gives_up_after_two_challenges():
    challenge = b"\xff\xff\xff\xff\x41ABCD"
    assert _a2s_exchange(_FakeUdp([challenge, challenge]), ("h", 1)) is None


# ---- bedrock ----


def test_bedrock_parse():
    sid = "MCPE;Warpyard Bedrock;800;1.21.100;4;11;123;world;Survival"
    data = b"\x1c" + b"\x00" * 16 + gamequery._RAKNET_MAGIC + struct.pack(">H", len(sid)) + sid.encode()
    assert parse_bedrock_pong(data) == {"online": 4, "max": 11, "name": "Warpyard Bedrock", "version": "1.21.100"}
    assert parse_bedrock_pong(b"\x05junk") is None


# ---- cache + failure paths ----


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_unsupported_slug_fast_none():
    assert players_for_slug("factorio", "example.invalid", 1) is None
    assert gamequery._cache == {}  # unsupported slugs never touch the cache


def test_closed_port_returns_none_fast_and_negative_caches(monkeypatch):
    port = _closed_port()
    t0 = time.monotonic()
    assert players_for_slug("minecraft", "127.0.0.1", port) is None
    assert time.monotonic() - t0 < 2
    assert gamequery._cache[("127.0.0.1", port)][1] is None
    # second call is a pure cache hit: break the query fn to prove it isn't called
    monkeypatch.setattr(gamequery, "query_players", None)
    assert players_for_slug("minecraft", "127.0.0.1", port) is None


def test_cache_ttl_expiry(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(gamequery.time, "monotonic", lambda: clock[0])
    calls = []

    def fake_query(host, port, kind):
        calls.append(kind)
        return {"online": 7, "max": 20}

    monkeypatch.setattr(gamequery, "query_players", fake_query)
    assert players_for_slug("minecraft", "h", 1)["online"] == 7
    assert players_for_slug("minecraft", "h", 1)["online"] == 7
    assert len(calls) == 1  # within TTL: cached
    clock[0] += gamequery.CACHE_TTL + 1
    assert players_for_slug("minecraft", "h", 1)["online"] == 7
    assert len(calls) == 2  # expired: re-queried
