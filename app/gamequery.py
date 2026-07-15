"""Live player counts for game servers, queried over the public edge forwards.

Three wire protocols, all stdlib-only: Minecraft Java server-list ping (TCP),
Valve A2S_INFO (UDP — CS 1.6/CS2/Valheim), and RakNet unconnected ping (UDP —
Minecraft Bedrock). Results are cached for CACHE_TTL seconds per (host, port),
hits AND misses, so page renders may call players_for_slug() inline: the worst
case is one ~0.8s stall per unique endpoint per TTL window.
"""

import json
import socket
import struct
import threading
import time

CACHE_TTL = 30.0
SOCKET_TIMEOUT = 0.6

# image slug -> query protocol. Absent slug = no cheap query (factorio, zomboid, terraria).
KIND_BY_SLUG = {
    "minecraft": "minecraft",
    "cs": "a2s",
    "cs2": "a2s",
    "valheim": "a2s",
    "minecraft-bedrock": "bedrock",
}

_cache: dict[tuple[str, int], tuple[float, dict | None]] = {}
_cache_lock = threading.Lock()


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


# ---- Minecraft Java: server-list ping (TCP, VarInt-framed) ----


def _encode_varint(n: int) -> bytes:
    out = b""
    n &= 0xFFFFFFFF  # protocol VarInts are 32-bit; lets callers pass -1
    while True:
        b = n & 0x7F
        n >>= 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def _decode_varint(buf: bytes, off: int) -> tuple[int, int]:
    val = shift = 0
    while True:
        b = buf[off]
        off += 1
        val |= (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            return val, off
        if shift > 35:
            raise ValueError("varint too long")


def _read_varint_from_sock(sock: socket.socket) -> int:
    val = shift = 0
    while True:
        chunk = sock.recv(1)
        if not chunk:
            raise ValueError("eof in varint")
        b = chunk[0]
        val |= (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            return val
        if shift > 35:
            raise ValueError("varint too long")


def parse_minecraft_response(packet: bytes) -> dict | None:
    """Parse a status-response packet body (packet id + JSON length + JSON)."""
    pid, off = _decode_varint(packet, 0)
    if pid != 0x00:
        return None
    jlen, off = _decode_varint(packet, off)
    status = json.loads(packet[off : off + jlen].decode("utf-8", "replace"))
    players = status.get("players") or {}
    if "online" not in players or "max" not in players:
        return None
    out = {"online": int(players["online"]), "max": int(players["max"])}
    version = (status.get("version") or {}).get("name")
    if version:
        out["version"] = str(version)
    desc = status.get("description")
    if isinstance(desc, dict):
        desc = desc.get("text")
    if isinstance(desc, str) and desc.strip():
        out["name"] = desc.strip()
    return out


def _query_minecraft(host: str, port: int) -> dict | None:
    with socket.create_connection((host, port), timeout=SOCKET_TIMEOUT) as sock:
        sock.settimeout(SOCKET_TIMEOUT)
        ha = host.encode()
        handshake = b"\x00" + _encode_varint(-1) + _encode_varint(len(ha)) + ha + struct.pack(">H", port) + b"\x01"
        sock.sendall(_encode_varint(len(handshake)) + handshake + b"\x01\x00")  # + status request
        plen = _read_varint_from_sock(sock)
        if not 0 < plen <= 1 << 21:
            return None
        buf = b""
        while len(buf) < plen:
            chunk = sock.recv(plen - len(buf))
            if not chunk:
                return None
            buf += chunk
        return parse_minecraft_response(buf)


# ---- Valve A2S_INFO (UDP): CS 1.6 / CS2 / Valheim ----

_A2S_INFO = b"\xff\xff\xff\xffTSource Engine Query\x00"


def _read_cstring(data: bytes, off: int) -> tuple[str, int]:
    end = data.index(0, off)
    return data[off:end].decode("utf-8", "replace"), end + 1


def parse_a2s_info(data: bytes) -> dict | None:
    """Parse an A2S_INFO reply (after the FF FF FF FF header): 0x49 (Source) or 0x6D (legacy GoldSrc)."""
    if len(data) < 2:
        return None
    header, off = data[0], 1
    if header == 0x49:
        off += 1  # protocol version
        name, off = _read_cstring(data, off)
        for _ in range(3):  # map, folder, game
            _, off = _read_cstring(data, off)
        off += 2  # appid
    elif header == 0x6D:
        _, off = _read_cstring(data, off)  # server address
        name, off = _read_cstring(data, off)
        for _ in range(3):
            _, off = _read_cstring(data, off)
    else:
        return None
    if off + 2 > len(data):
        return None
    out = {"online": data[off], "max": data[off + 1]}
    if name.strip():
        out["name"] = name.strip()
    return out


def _a2s_exchange(sock: socket.socket, addr: tuple[str, int]) -> dict | None:
    sock.sendto(_A2S_INFO, addr)
    for _ in range(2):  # initial reply, plus one challenge round-trip
        data = sock.recv(4096)
        if len(data) < 5 or data[:4] != b"\xff\xff\xff\xff":
            return None
        if data[4] == 0x41:  # S2C_CHALLENGE: resend with the 4 challenge bytes appended
            sock.sendto(_A2S_INFO + data[5:9], addr)
            continue
        return parse_a2s_info(data[4:])
    return None


def _query_a2s(host: str, port: int) -> dict | None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(SOCKET_TIMEOUT)
        return _a2s_exchange(sock, (host, port))


# ---- Minecraft Bedrock: RakNet unconnected ping (UDP) ----

_RAKNET_MAGIC = bytes.fromhex("00ffff00fefefefefdfdfdfd12345678")


def parse_bedrock_pong(data: bytes) -> dict | None:
    """Parse an unconnected pong (0x1c): the payload is a ;-separated server-ID string."""
    if not data or data[0] != 0x1C:
        return None
    off = 1 + 8 + 8 + 16  # time, server guid, magic
    if len(data) < off + 2:
        return None
    (slen,) = struct.unpack(">H", data[off : off + 2])
    fields = data[off + 2 : off + 2 + slen].decode("utf-8", "replace").split(";")
    if len(fields) < 6:
        return None
    out = {"online": int(fields[4]), "max": int(fields[5])}
    if fields[1].strip():
        out["name"] = fields[1].strip()
    if len(fields) > 3 and fields[3].strip():
        out["version"] = fields[3].strip()
    return out


def _query_bedrock(host: str, port: int) -> dict | None:
    ping = b"\x01" + struct.pack(">q", int(time.time() * 1000)) + _RAKNET_MAGIC + struct.pack(">q", 0x77797172)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(SOCKET_TIMEOUT)
        sock.sendto(ping, (host, port))
        return parse_bedrock_pong(sock.recv(4096))


# ---- public API ----

_QUERIES = {"minecraft": _query_minecraft, "a2s": _query_a2s, "bedrock": _query_bedrock}


def query_players(host: str, port: int, kind: str) -> dict | None:
    """One live query, no cache. Returns {"online", "max", ["name"], ["version"]} or None. Never raises."""
    fn = _QUERIES.get(kind)
    if not fn:
        return None
    try:
        return fn(host, port)
    except Exception:  # noqa: BLE001 — any wire/parse/socket failure means "no data"
        return None


def players_for_slug(slug: str, host: str, port: int) -> dict | None:
    """Cached player count for an image slug; None fast for unsupported slugs or unreachable servers."""
    kind = KIND_BY_SLUG.get(slug)
    if not kind or not host:
        return None
    key = (host, port)
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
    result = query_players(host, port, kind)
    with _cache_lock:
        _cache[key] = (now + CACHE_TTL, result)
    return result
