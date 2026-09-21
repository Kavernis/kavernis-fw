from kavernis.config.interfaces import InterfaceConfig, InterfacesConfig
from kavernis.models.interface import (
    IPv4AddressMode,
    IPv4Settings,
    IPv6AddressMode,
    IPv6Settings,
    NetworkInterface,
)


def resolve_interface(config: InterfaceConfig) -> NetworkInterface:
    return NetworkInterface(
        uid=config.uid,
        id=config.id,
        name=config.name,
        device=config.device,
        ipv4=IPv4Settings(
            mode=IPv4AddressMode(config.ipv4.mode.value),
            address=config.ipv4.address,
        ),
        ipv6=IPv6Settings(
            mode=IPv6AddressMode(config.ipv6.mode.value),
            address=config.ipv6.address,
        ),
    )


def resolve_interfaces(
    config: InterfacesConfig,
) -> list[NetworkInterface]:
    """Convert the complete interfaces configuration into domain models."""

    return [resolve_interface(interface) for interface in config.interfaces]
