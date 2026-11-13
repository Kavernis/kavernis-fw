"""Convert typed configuration into immutable network intent."""

from kavernis.config.addressing import IPv4Config, IPv6Config
from kavernis.config.gateways import GatewayConfig
from kavernis.config.network import (
    BridgeDeviceConfig,
    DevicesConfig,
    LogicalInterfacesConfig,
    RoutingConfig,
    VLANDeviceConfig,
)
from kavernis.config.routes import RouteConfig
from kavernis.models.device import (
    BridgeSettings,
    DeviceSettings,
    NetworkDevice,
    PhysicalSettings,
    VLANSettings,
)
from kavernis.models.interface import (
    DHCPv4Settings,
    InterfaceAddressing,
    IPv4AddressMode,
    IPv4Settings,
    IPv6AddressMode,
    IPv6Settings,
    NetworkInterface,
)
from kavernis.models.network import NetworkModel
from kavernis.models.routing import Gateway, Route, RoutingModel


def resolve_addressing(ipv4: IPv4Config, ipv6: IPv6Config) -> InterfaceAddressing:
    return InterfaceAddressing(
        IPv4Settings(
            IPv4AddressMode(ipv4.mode.value),
            ipv4.address,
            DHCPv4Settings(
                ipv4.dhcp.use_hostname,
                ipv4.dhcp.send_hostname,
                ipv4.dhcp.use_dns,
                ipv4.dhcp.use_routes,
                ipv4.dhcp.use_ntp,
                ipv4.dhcp.route_metric,
            )
            if ipv4.mode.value == "dhcp"
            else None,
        ),
        IPv6Settings(IPv6AddressMode(ipv6.mode.value), ipv6.address),
    )


def resolve_routing(
    gateways: tuple[GatewayConfig, ...],
    routes: tuple[RouteConfig, ...],
) -> RoutingModel:
    return RoutingModel(
        tuple(Gateway(g.id, g.interface, g.address, g.onlink) for g in gateways),
        tuple(Route(r.id, r.network, r.gateway) for r in routes),
    )


def resolve_network(
    devices: DevicesConfig, interfaces: LogicalInterfacesConfig, routing: RoutingConfig
) -> NetworkModel:
    resolved: list[NetworkDevice] = []
    for device in devices.devices:
        settings: DeviceSettings = PhysicalSettings()
        if isinstance(device, VLANDeviceConfig):
            settings = VLANSettings(device.vlan.parent, device.vlan.tag)
        elif isinstance(device, BridgeDeviceConfig):
            settings = BridgeSettings(
                tuple(sorted(device.bridge.members)), device.bridge.stp
            )
        resolved.append(NetworkDevice(device.id, device.name, settings))
    return NetworkModel(
        tuple(resolved),
        tuple(
            NetworkInterface(i.id, i.name, i.device, resolve_addressing(i.ipv4, i.ipv6))
            for i in interfaces.interfaces
        ),
        resolve_routing(routing.gateways, routing.routes),
    )
