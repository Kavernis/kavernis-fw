"""Immutable static routing intent, referencing logical interface identities."""

from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network


@dataclass(frozen=True)
class Gateway:
    id: str
    interface: str
    address: IPv4Address | IPv6Address | None = None
    onlink: bool = False


@dataclass(frozen=True)
class Route:
    id: str
    network: IPv4Network | IPv6Network
    gateway: str


@dataclass(frozen=True)
class RoutingModel:
    gateways: tuple[Gateway, ...] = ()
    routes: tuple[Route, ...] = ()
