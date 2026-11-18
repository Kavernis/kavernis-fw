"""Validate network references and produce deterministic dependency order."""

from dataclasses import replace
from graphlib import CycleError, TopologicalSorter
from ipaddress import IPv4Address, IPv4Network, IPv6Address

from kavernis.models.device import BridgeSettings, PhysicalSettings, VLANSettings
from kavernis.models.interface import IPv4AddressMode, IPv6AddressMode
from kavernis.models.network import NetworkModel


def resolve_topology(model: NetworkModel) -> NetworkModel:
    """Validate the whole intent, including callers constructing domain objects."""
    for label, values in (
        ("device id", [d.id for d in model.devices]),
        ("kernel device name", [d.name for d in model.devices]),
        ("interface id", [i.id for i in model.interfaces]),
        ("L3 assignment", [i.device for i in model.interfaces]),
        ("gateway id", [g.id for g in model.routing.gateways]),
        ("route id", [r.id for r in model.routing.routes]),
    ):
        if len(values) != len(set(values)):
            raise ValueError(f"duplicate {label}")
    devices = {d.id: d for d in model.devices}
    dependencies: dict[str, set[str]] = {}
    for device in model.devices:
        settings = device.settings
        refs = (
            {settings.parent}
            if isinstance(settings, VLANSettings)
            else set(settings.members)
            if isinstance(settings, BridgeSettings)
            else set()
        )
        for ref in refs:
            if ref not in devices:
                raise ValueError(
                    f"device {device.id}: dependency '{ref}' does not exist"
                )
        dependencies[device.id] = refs
    sorter = TopologicalSorter(dependencies)
    try:
        sorter.prepare()
    except CycleError as error:
        raise ValueError(f"device dependency cycle: {error.args[1]}") from error
    order: list[str] = []
    while sorter.is_active():
        ready = sorted(sorter.get_ready())
        order.extend(ready)
        sorter.done(*ready)

    pairs: set[tuple[str, int]] = set()
    parents = {
        d.settings.parent for d in model.devices if isinstance(d.settings, VLANSettings)
    }
    owners: dict[str, str] = {}
    for device in model.devices:
        settings = device.settings
        if isinstance(settings, VLANSettings):
            if not isinstance(devices[settings.parent].settings, PhysicalSettings):
                raise ValueError(f"device {device.id}: VLAN parent must be physical")
            pair = (settings.parent, settings.tag)
            if pair in pairs:
                raise ValueError(f"device {device.id}: duplicate VLAN parent/tag")
            if not 1 <= settings.tag <= 4094:
                raise ValueError(f"device {device.id}: invalid VLAN tag")
            pairs.add(pair)
        elif isinstance(settings, BridgeSettings):
            if not settings.members or len(settings.members) != len(
                set(settings.members)
            ):
                raise ValueError(f"device {device.id}: invalid bridge members")
            for member in settings.members:
                if isinstance(devices[member].settings, BridgeSettings):
                    raise ValueError(
                        f"device {device.id}: bridge member cannot be a bridge"
                    )
                if member in owners:
                    raise ValueError(f"device {member}: conflicting bridge ownership")
                if member in parents:
                    raise ValueError(
                        f"device {member}: VLAN parent cannot be a bridge member"
                    )
                owners[member] = device.id
    for interface in model.interfaces:
        if interface.device not in devices:
            raise ValueError(
                f"interface {interface.id}: device '{interface.device}' does not exist"
            )
        if interface.device in owners:
            raise ValueError(
                f"interface {interface.id}: bridge member cannot have an L3 assignment"
            )
    _validate_routing(model)
    return replace(
        model,
        devices=tuple(devices[key] for key in order),
        interfaces=tuple(sorted(model.interfaces, key=lambda item: item.id)),
        routing=replace(
            model.routing,
            gateways=tuple(sorted(model.routing.gateways, key=lambda item: item.id)),
            routes=tuple(sorted(model.routing.routes, key=lambda item: item.id)),
        ),
    )


def _validate_routing(model: NetworkModel) -> None:
    interfaces = {i.id: i for i in model.interfaces}
    gateways = {g.id: g for g in model.routing.gateways}
    for gateway in model.routing.gateways:
        interface = interfaces.get(gateway.interface)
        if interface is None:
            raise ValueError(
                f"gateway {gateway.id}: interface '{gateway.interface}' does not exist"
            )
        if gateway.address is None:
            if gateway.onlink:
                raise ValueError(f"gateway {gateway.id}: onlink requires an address")
            continue
        family = (
            interface.addressing.ipv4
            if isinstance(gateway.address, IPv4Address)
            else interface.addressing.ipv6
        )
        if family.mode in (IPv4AddressMode.DISABLED, IPv6AddressMode.DISABLED):
            raise ValueError(
                f"gateway {gateway.id}: interface address family is disabled"
            )
        address = gateway.address
        if address.is_unspecified or address.is_multicast or (
            isinstance(address, IPv4Address)
            and address == IPv4Address("255.255.255.255")
        ):
            raise ValueError(f"gateway {gateway.id}: invalid next-hop address")
        if family.address is not None and address == family.address.ip:
            raise ValueError(
                f"gateway {gateway.id}: gateway cannot be interface address"
            )
        if isinstance(address, IPv6Address) and address.is_link_local:
            # IPv6 link-local scope is inherently tied to this interface. The
            # generated [Route] already binds to the interface's .network file.
            continue
        if isinstance(address, IPv4Address) and family.address is not None:
            subnet = family.address.network
            # RFC 3021 makes both /31 addresses usable point-to-point endpoints;
            # a /32 has no distinct subnet or broadcast address to exclude.
            if subnet.prefixlen <= 30 and address in (
                subnet.network_address,
                subnet.broadcast_address,
            ):
                raise ValueError(f"gateway {gateway.id}: invalid IPv4 subnet next hop")
        if not gateway.onlink and (
            family.address is None or address not in family.address.network
        ):
            raise ValueError(
                f"gateway {gateway.id}: address is not in a static subnet "
                f"on interface {interface.id}; set onlink: true to explicitly allow it"
            )
    for route in model.routing.routes:
        selected = gateways.get(route.gateway)
        if selected is None:
            raise ValueError(
                f"route {route.id}: gateway '{route.gateway}' does not exist"
            )
        if (
            selected.address is not None
            and selected.address.version != route.network.version
        ):
            raise ValueError(
                f"route {route.id}: network family does not match gateway family"
            )
        interface = interfaces[selected.interface]
        mode = (
            interface.addressing.ipv4.mode
            if isinstance(route.network, IPv4Network)
            else interface.addressing.ipv6.mode
        )
        if mode in (IPv4AddressMode.DISABLED, IPv6AddressMode.DISABLED):
            raise ValueError(
                f"route {route.id}: interface {interface.id} cannot route this family"
            )
