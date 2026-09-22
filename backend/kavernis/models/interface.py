from dataclasses import dataclass
from enum import Enum
from ipaddress import IPv4Interface, IPv6Interface
from uuid import UUID


class IPv4AddressMode(Enum):
    STATIC = "static"
    DHCP = "dhcp"
    DISABLED = "disabled"


class IPv6AddressMode(Enum):
    STATIC = "static"
    DHCP6 = "dhcp6"
    SLAAC = "slaac"
    DISABLED = "disabled"


@dataclass(frozen=True)
class IPv4Settings:
    mode: IPv4AddressMode
    address: IPv4Interface | None = None


@dataclass(frozen=True)
class IPv6Settings:
    mode: IPv6AddressMode
    address: IPv6Interface | None = None


@dataclass(frozen=True)
class VLANSettings:
    parent: str
    tag: int


@dataclass(frozen=True)
class BridgeSettings:
    members: tuple[str, ...]
    stp: bool = True


@dataclass(frozen=True)
class NetworkInterface:
    uid: UUID
    id: str
    name: str
    device: str
    ipv4: IPv4Settings
    ipv6: IPv6Settings
    vlan: VLANSettings | None = None
    bridge: BridgeSettings | None = None
