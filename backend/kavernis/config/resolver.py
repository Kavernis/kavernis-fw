from kavernis.config.interfaces import InterfaceConfig, InterfacesConfig
from kavernis.models.interface import (
    BridgeSettings,
    IPv4AddressMode,
    IPv4Settings,
    IPv6AddressMode,
    IPv6Settings,
    NetworkInterface,
    VLANSettings,
)


def resolve_interface(config: InterfaceConfig) -> NetworkInterface:
    return NetworkInterface(
        uid=config.uid,
        id=config.id,
        name=config.name,
        device=config.device,
        bridge=(
            BridgeSettings(members=config.bridge.members, stp=config.bridge.stp)
            if config.bridge is not None
            else None
        ),
        vlan=(
            VLANSettings(parent=config.vlan.parent, tag=config.vlan.tag)
            if config.vlan is not None
            else None
        ),
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
