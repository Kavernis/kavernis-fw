from dataclasses import dataclass
from enum import Enum
from ipaddress import IPv4Interface, IPv6Interface


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
class DHCPv4Settings:
    """Resolved, backend-independent DHCPv4 client policy."""

    use_hostname: bool
    send_hostname: bool
    use_dns: bool
    use_routes: bool
    use_ntp: bool
    route_metric: int | None

    def __post_init__(self) -> None:
        flags = (
            self.use_hostname,
            self.send_hostname,
            self.use_dns,
            self.use_routes,
            self.use_ntp,
        )
        if any(type(value) is not bool for value in flags):
            raise TypeError("DHCPv4 policy flags must be bool")
        if self.route_metric is not None and (
            type(self.route_metric) is not int
            or not 0 <= self.route_metric <= 4294967295
        ):
            raise ValueError("DHCPv4 route metric must be an unsigned 32-bit integer")


@dataclass(frozen=True)
class IPv4Settings:
    mode: IPv4AddressMode
    address: IPv4Interface | None = None
    dhcp: DHCPv4Settings | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.mode, IPv4AddressMode):
            raise TypeError("IPv4 mode must be IPv4AddressMode")
        if self.address is not None and not isinstance(self.address, IPv4Interface):
            raise TypeError("IPv4 address must be IPv4Interface")
        if self.dhcp is not None:
            if not isinstance(self.dhcp, DHCPv4Settings):
                raise TypeError("DHCP policy must be DHCPv4Settings")
        if self.mode is IPv4AddressMode.STATIC and (
            self.address is None or self.dhcp is not None
        ):
            raise ValueError("static IPv4 requires an address and forbids DHCP policy")
        if self.mode is IPv4AddressMode.DHCP and (
            self.address is not None or self.dhcp is None
        ):
            raise ValueError("DHCP IPv4 forbids an address and requires DHCP policy")
        if self.mode is IPv4AddressMode.DISABLED and (
            self.address is not None or self.dhcp is not None
        ):
            raise ValueError("disabled IPv4 forbids address and DHCP policy")


@dataclass(frozen=True)
class IPv6Settings:
    mode: IPv6AddressMode
    address: IPv6Interface | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.mode, IPv6AddressMode):
            raise TypeError("IPv6 mode must be IPv6AddressMode")
        if self.address is not None and not isinstance(self.address, IPv6Interface):
            raise TypeError("IPv6 address must be IPv6Interface")
        if (self.mode is IPv6AddressMode.STATIC) != (self.address is not None):
            raise ValueError("static IPv6 requires an address; other modes forbid it")


@dataclass(frozen=True)
class InterfaceAddressing:
    ipv4: IPv4Settings = IPv4Settings(IPv4AddressMode.DISABLED)
    ipv6: IPv6Settings = IPv6Settings(IPv6AddressMode.DISABLED)


@dataclass(frozen=True)
class NetworkInterface:
    id: str
    name: str
    device: str
    addressing: InterfaceAddressing
