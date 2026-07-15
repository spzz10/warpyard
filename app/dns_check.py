"""DNS ownership/reachability pre-check for custom domains. A domain can only get an ACME
HTTP-01 cert at the edge if it actually resolves there, so 'does it resolve to the edge IP?'
is both the rate-limit guard and the de-facto proof-of-ownership. A CNAME to the edge host
resolves through to the same A record, so a single 'resolves to EDGE_IP' check covers both the
subdomain (CNAME) and apex (A) cases."""

import socket

from app.config import get_settings


def resolves_to_edge(hostname: str) -> bool:
    """True if `hostname` resolves (A / through CNAME) to the edge's public IP."""
    edge_ip = get_settings().EDGE_IP
    try:
        infos = socket.getaddrinfo(hostname, None, family=socket.AF_INET, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, OSError):
        return False
    return any(info[4][0] == edge_ip for info in infos)


def required_record(hostname: str) -> dict:
    """The DNS record the user must create to point `hostname` at us. Apex (one label before
    the public suffix, roughly) needs an A record; anything deeper uses a CNAME to the edge."""
    s = get_settings()
    is_apex = hostname.count(".") <= 1
    if is_apex:
        return {"type": "A", "name": hostname, "value": s.EDGE_IP}
    return {"type": "CNAME", "name": hostname, "value": s.EDGE_HOST}
