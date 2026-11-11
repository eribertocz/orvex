"""The client address of a server-mode request (#438): the socket peer, unless that peer is a
trusted proxy.

`X-Forwarded-For` is believed only when the peer is in the administrator's `trusted_proxies`
(none by default), and then only its right-most address that is not itself a trusted proxy:
every address to the left of that one was written by the client and may be anything. Every
header line counts, in order, as one list. A header that cannot be read leaves the peer as the
address (fail closed: the proxy's own counter).
"""

from __future__ import annotations

import ipaddress
from collections.abc import Sequence

type Network = ipaddress.IPv4Network | ipaddress.IPv6Network
_LONGEST_HEADER = 4096


def _address(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(text.strip())
    except ValueError:
        return None


def _trusted(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address, proxies: Sequence[Network]
) -> bool:
    return any(address.version == network.version and address in network for network in proxies)


def client_address(
    peer: str | None, lines: Sequence[str], proxies: Sequence[Network]
) -> str | None:
    """The address the failed-sign-in delay counts this request under. `lines` is every
    `X-Forwarded-For` line, in order: a proxy may add its own line rather than extend one."""
    found = _address(peer or "")
    forwarded = ", ".join(lines)
    if found is None or not _trusted(found, proxies) or not forwarded:
        return peer
    if len(forwarded) > _LONGEST_HEADER:
        return peer
    hops = [_address(hop) for hop in forwarded.split(",")]
    for hop in reversed(hops):
        if hop is None:
            return peer
        if not _trusted(hop, proxies):
            return str(hop)
    return str(hops[0]) if hops and hops[0] is not None else peer
