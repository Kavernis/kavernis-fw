from dataclasses import replace
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network

from kavernis.config.gateways import GatewaysConfig
from kavernis.config.interfaces import InterfaceConfig, InterfacesConfig
from kavernis.config.routes import RoutesConfig
from kavernis.models.interface import (
    BridgeSettings,
    DHCPv4Settings,
    IPv4AddressMode,
    IPv4Settings,
    IPv6AddressMode,
    IPv6Settings,
    NetworkInterface,
    VLANSettings,
)
from kavernis.models.routing import ResolvedRoute


def resolve_interface(config: InterfaceConfig) -> NetworkInterface:
    return NetworkInterface(
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
            dhcp=(
                DHCPv4Settings(
                    use_hostname=config.ipv4.dhcp.use_hostname,
                    send_hostname=config.ipv4.dhcp.send_hostname,
                    use_dns=config.ipv4.dhcp.use_dns,
                    use_routes=config.ipv4.dhcp.use_routes,
                    use_ntp=config.ipv4.dhcp.use_ntp,
                    route_metric=config.ipv4.dhcp.route_metric,
                )
                if config.ipv4.mode.value == "dhcp"
                else None
            ),
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


def resolve_network(
    interfaces_config: InterfacesConfig,
    gateways_config: GatewaysConfig,
    routes_config: RoutesConfig,
) -> list[NetworkInterface]:
    """Resolve logical routing references and deterministic route intent."""
    interfaces = resolve_interfaces(interfaces_config)
    by_interface = {interface.id: interface for interface in interfaces}
    gateways: dict[
        str, tuple[NetworkInterface, IPv4Address | IPv6Address | None, bool]
    ] = {}
    for gateway in gateways_config.gateways:
        interface = by_interface.get(gateway.interface)
        if interface is None:
            raise ValueError(
                f"gateway {gateway.id}: interface '{gateway.interface}' does not exist"
            )
        if gateway.address is not None and not gateway.onlink:
            networks = _static_networks(interface, gateway.address)
            if not any(gateway.address in network for network in networks):
                raise ValueError(
                    f"gateway {gateway.id}: address {gateway.address} is not in a "
                    f"statically configured subnet on interface {gateway.interface}; "
                    "set onlink: true to explicitly allow it"
                )
        gateways[gateway.id] = (interface, gateway.address, gateway.onlink)

    resolved: dict[str, list[ResolvedRoute]] = {
        interface.id: [] for interface in interfaces
    }
    for route in routes_config.routes:
        selected = gateways.get(route.gateway)
        if selected is None:
            raise ValueError(
                f"route {route.id}: gateway '{route.gateway}' does not exist"
            )
        interface, address, onlink = selected
        if address is not None and address.version != route.network.version:
            raise ValueError(
                f"route {route.id}: network family does not match gateway "
                f"{route.gateway} address family"
            )
        if address is None and not _supports_family(interface, route.network):
            raise ValueError(
                f"route {route.id}: interface {interface.id} cannot route "
                f"IPv{route.network.version}"
            )
        resolved[interface.id].append(
            ResolvedRoute(route.id, route.network, address, onlink)
        )
    return [
        replace(
            interface,
            routes=tuple(sorted(resolved[interface.id], key=lambda route: route.id)),
        )
        for interface in interfaces
    ]


def _static_networks(
    interface: NetworkInterface, address: IPv4Address | IPv6Address
) -> tuple[IPv4Network | IPv6Network, ...]:
    networks: list[IPv4Network | IPv6Network] = []
    if isinstance(address, IPv4Address) and interface.ipv4.address is not None:
        networks.append(interface.ipv4.address.network)
    if isinstance(address, IPv6Address) and interface.ipv6.address is not None:
        networks.append(interface.ipv6.address.network)
    return tuple(networks)


def _supports_family(
    interface: NetworkInterface, network: IPv4Network | IPv6Network
) -> bool:
    if isinstance(network, IPv4Network):
        return interface.ipv4.mode is not IPv4AddressMode.DISABLED
    return interface.ipv6.mode is not IPv6AddressMode.DISABLED
