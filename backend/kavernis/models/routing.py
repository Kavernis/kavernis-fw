"""Resolved backend-independent routing domain objects."""

from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network


@dataclass(frozen=True)
class ResolvedRoute:
    """A route attached to its resolved interface and optional next-hop."""

    id: str
    network: IPv4Network | IPv6Network
    gateway_address: IPv4Address | IPv6Address | None
    gateway_onlink: bool
