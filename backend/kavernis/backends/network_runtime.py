"""Typed Linux observations and bounded networkd activation verification.

No observation is desired configuration. Extra kernel/dynamic state is allowed;
only explicitly expected static properties and retired static values are checked.
"""

import json
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from ipaddress import ip_address, ip_interface, ip_network
from math import isfinite
from pathlib import Path
from typing import Any, Protocol

from kavernis.models.device import BridgeSettings, VLANSettings
from kavernis.models.interface import IPv4AddressMode, IPv6AddressMode
from kavernis.models.network import NetworkModel


class RuntimeVerificationError(RuntimeError):
    """Runtime inspection failed or required static properties did not converge."""


@dataclass(frozen=True)
class RuntimeLink:
    name: str
    kind: str | None = None
    parent: str | None = None
    vlan_id: int | None = None
    master: str | None = None
    stp: bool | None = None
    addresses: frozenset[str] = frozenset()
    network_file: str | None = None
    setup_state: str | None = None
    dropins: tuple[str, ...] = ()
    dynamic_addresses: frozenset[str] = frozenset()
    configured_static_addresses: frozenset[str] = frozenset()


@dataclass(frozen=True)
class RuntimeRoute:
    destination: str
    gateway: str | None
    device: str
    onlink: bool = False
    source_prefix: str | None = None
    tos: int = 0
    table: int = 254
    route_type: str = "unicast"
    metric: int | None = None
    scope: int | None = None
    preference: str = "medium"
    family: int = field(init=False)

    def __post_init__(self) -> None:
        destination = ip_network(self.destination)
        object.__setattr__(self, "destination", str(destination))
        object.__setattr__(self, "family", destination.version)
        if self.gateway is not None:
            gateway = ip_address(self.gateway)
            if gateway.version != destination.version:
                raise ValueError("route gateway family does not match destination")
            object.__setattr__(self, "gateway", str(gateway))
        if self.source_prefix is not None:
            source = ip_network(self.source_prefix)
            if source.version != destination.version:
                raise ValueError("route source family does not match destination")
            object.__setattr__(
                self, "source_prefix", str(source) if source.prefixlen else None
            )
        if self.metric is None:
            # networkd's unspecified IPv6 priority is IP6_RT_PRIO_USER.
            object.__setattr__(self, "metric", 1024 if self.family == 6 else 0)
        if self.scope is None:
            object.__setattr__(
                self, "scope", 253 if self.family == 4 and self.gateway is None else 0
            )


@dataclass(frozen=True)
class NetworkRuntimeSnapshot:
    links: tuple[RuntimeLink, ...]
    routes: frozenset[RuntimeRoute] = frozenset()


class NetworkRuntimeInspector(Protocol):
    def inspect(self, timeout: float) -> NetworkRuntimeSnapshot:
        """Return observations within the supplied total command budget."""
        ...


class LinuxNetworkRuntimeInspector:
    """Use iproute2 JSON and networkd's JSON status (systemd 257 baseline)."""

    def inspect(self, timeout: float) -> NetworkRuntimeSnapshot:
        deadline = time.monotonic() + timeout

        def query(arguments: list[str]) -> Any:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeVerificationError("runtime inspection timed out")
            result = subprocess.run(
                arguments,
                check=True,
                capture_output=True,
                text=True,
                timeout=remaining,
            )
            return json.loads(result.stdout)

        try:
            links = query(["ip", "-j", "-d", "link", "show"])
            addresses = query(["ip", "-j", "address", "show"])
            routes4 = query(["ip", "-j", "-4", "route", "show", "table", "main"])
            routes6 = query(["ip", "-j", "-6", "route", "show", "table", "main"])
            status = query(["networkctl", "--json=short", "status", "--all"])
            return parse_snapshot(links, addresses, routes4, routes6, status)
        except (
            OSError,
            subprocess.SubprocessError,
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
        ) as error:
            raise RuntimeVerificationError(
                f"cannot inspect Linux/networkd runtime: {error}"
            ) from error


def parse_snapshot(
    links: Any,
    addresses: Any,
    routes4: Any,
    routes6: Any,
    status: Any,
) -> NetworkRuntimeSnapshot:
    """Normalize tool-specific JSON once, at the backend boundary."""
    if not all(
        isinstance(value, list) for value in (links, addresses, routes4, routes6)
    ):
        raise ValueError("expected ip JSON arrays")
    statuses = {item["Index"]: item for item in status["Interfaces"]}
    names = {item["ifindex"]: item["ifname"] for item in links}
    assigned = {
        item["ifindex"]: frozenset(
            str(ip_interface(f"{addr['local']}/{addr['prefixlen']}"))
            for addr in item["addr_info"]
            if not addr.get("tentative") and not addr.get("dadfailed")
        )
        for item in addresses
    }
    dynamic = {
        item["ifindex"]: frozenset(
            str(ip_interface(f"{addr['local']}/{addr['prefixlen']}"))
            for addr in item["addr_info"]
            if addr.get("dynamic")
        )
        for item in addresses
    }
    observed = []
    for item in links:
        info = item.get("linkinfo", {})
        data = info.get("info_data", {})
        network = statuses.get(item["ifindex"], {})
        observed.append(
            RuntimeLink(
                name=item["ifname"],
                kind=info.get("info_kind"),
                parent=(item.get("link") or names.get(item.get("link_index")))
                if "link_netnsid" not in item
                else None,
                vlan_id=data.get("id"),
                master=item.get("master"),
                stp=bool(data["stp_state"]) if "stp_state" in data else None,
                addresses=assigned.get(item["ifindex"], frozenset()),
                network_file=network.get("NetworkFile"),
                setup_state=network.get("AdministrativeState"),
                dropins=tuple(network.get("NetworkFileDropins", [])),
                dynamic_addresses=dynamic.get(item["ifindex"], frozenset()),
                configured_static_addresses=_configured_static_addresses(network),
            )
        )
    routes = frozenset(
        normalized
        for family, entries in ((4, routes4), (6, routes6))
        for route in entries
        if (normalized := _static_route(family, route)) is not None
    )
    return NetworkRuntimeSnapshot(tuple(observed), routes)


def _configured_static_addresses(network: Any) -> frozenset[str]:
    """Only positive networkd per-address evidence counts; unknown is not static.

    ConfigState can be a comma-separated set. Accept configured alone, never
    requesting/configuring/marked/removing combined with an old configured bit.
    Kernel presence and DAD readiness are checked separately.
    """
    result = set()
    for address in network.get("Addresses", []):
        if (address.get("ConfigSource"), address.get("ConfigState")) != (
            "static",
            "configured",
        ):
            continue
        value = ip_address(bytes(address["Address"]))
        if address["Family"] != (2 if value.version == 4 else 10):
            raise ValueError("networkd address family does not match address")
        result.add(str(ip_interface(f"{value}/{address['PrefixLength']}")))
    return frozenset(result)


def _static_route(family: int, route: Any) -> RuntimeRoute | None:
    # Never collapse unsupported forwarding constructs onto a simple route.
    if route.get("protocol") not in ("static", 4, "4") or "dev" not in route:
        return None
    if any(
        key in route
        for key in (
            "multipath",
            "nexthops",
            "nhid",
            "encap",
            "via",
            "newdst",
            "to",
            "iif",
        )
    ):
        return None
    default = "0.0.0.0/0" if family == 4 else "::/0"
    destination = route["dst"]
    if destination == "default":
        destination = default
    if ip_network(destination).version != family:
        raise ValueError("route dump family does not match destination")
    # iproute2 emits RTA_SRC as 'from', or 'src' for a zero source prefix.
    # RTA_PREFSRC is 'prefsrc': it selects a local source, not matching traffic.
    source = route.get("from", route.get("src"))
    if source in ("all", "default"):
        source = default
    elif source and source.startswith("0/"):
        source = default.split("/")[0] + source[1:]
    tables = {"main": 254, "local": 255, "default": 253}
    scopes = {"global": 0, "universe": 0, "link": 253, "host": 254, "nowhere": 255}
    try:
        table = route.get("table", 254)
        table = tables[table] if table in tables else int(table)
        scope = route.get("scope")
        if scope is not None:
            scope = scopes[scope] if scope in scopes else int(scope)
        tos = route.get("tos", 0)
        tos = int(tos, 0) if isinstance(tos, str) else int(tos)
    except ValueError:
        # Custom symbolic table/TOS names cannot satisfy our unrestricted main
        # table routes. Do not guess or confuse them with defaults.
        return None
    route_type = route.get("type", "unicast")
    if source is not None and ip_network(source).version != family:
        return None
    return RuntimeRoute(
        destination,
        route.get("gateway"),
        route["dev"],
        "onlink" in route.get("flags", []),
        source,
        tos,
        table,
        "unicast" if route_type in (1, "1") else route_type,
        int(route["metric"]) if "metric" in route else None,
        scope,
        route.get("pref", "medium"),
    )


def static_addresses(model: NetworkModel) -> frozenset[tuple[str, str]]:
    names = {d.id: d.name for d in model.devices}
    return frozenset(
        (names[i.device], str(family.address))
        for i in model.interfaces
        for family in (i.addressing.ipv4, i.addressing.ipv6)
        if family.address is not None
    )


def static_routes(model: NetworkModel) -> frozenset[RuntimeRoute]:
    names = {d.id: d.name for d in model.devices}
    interfaces = {i.id: names[i.device] for i in model.interfaces}
    gateways = {g.id: g for g in model.routing.gateways}
    interface_models = {i.id: i for i in model.interfaces}
    return frozenset(
        RuntimeRoute(
            str(r.network),
            str(g.address) if g.address is not None else None,
            interfaces[g.interface],
            effective_onlink(interface_models[g.interface], g),
        )
        for r in model.routing.routes
        for g in (gateways[r.gateway],)
    )


def effective_onlink(interface: Any, gateway: Any) -> bool:
    """networkd infers GatewayOnLink for gateways on interfaces without addresses."""
    return gateway.onlink or (
        gateway.address is not None
        and gateway.address.version == 6
        and gateway.address.is_link_local
        and interface.addressing.ipv4.address is None
        and interface.addressing.ipv6.address is None
    )


def topology_issues(
    model: NetworkModel,
    snapshot: NetworkRuntimeSnapshot,
) -> list[str]:
    links = {link.name: link for link in snapshot.links}
    names = {d.id: d.name for d in model.devices}
    masters = {
        names[member]: d.name
        for d in model.devices
        if isinstance(d.settings, BridgeSettings)
        for member in d.settings.members
    }
    issues = []
    for device in model.devices:
        link = links.get(device.name)
        if link is None:
            issues.append(f"device '{device.id}' ({device.name}) is missing")
            continue
        settings = device.settings
        if isinstance(settings, VLANSettings):
            if link.kind != "vlan":
                issues.append(
                    f"VLAN {device.name}: expected vlan, observed {link.kind}"
                )
            if link.vlan_id != settings.tag:
                issues.append(
                    f"VLAN {device.name}: expected VLAN ID {settings.tag}, "
                    f"observed {link.vlan_id}"
                )
            if link.parent != names[settings.parent]:
                issues.append(
                    f"VLAN {device.name}: expected parent "
                    f"{names[settings.parent]}, observed {link.parent}"
                )
        elif isinstance(settings, BridgeSettings):
            expected_members = {names[member] for member in settings.members}
            observed_members = {
                member.name for member in snapshot.links if member.master == device.name
            }
            for member in sorted(expected_members - observed_members):
                issues.append(f"bridge {device.name}: missing member {member}")
            for member in sorted(observed_members - expected_members):
                issues.append(f"bridge {device.name}: unexpected member {member}")
            if link.kind != "bridge":
                issues.append(f"bridge {device.name}: observed type {link.kind}")
            if link.stp != settings.stp:
                issues.append(
                    f"bridge {device.name}: expected STP {settings.stp}, "
                    f"observed {link.stp}"
                )
        elif link.kind in ("vlan", "bridge"):
            issues.append(f"physical device {device.name}: observed type {link.kind}")
        if link.master != masters.get(device.name):
            issues.append(
                f"device {device.name}: expected bridge "
                f"{masters.get(device.name)}, observed {link.master}"
            )
    return issues


def convergence_issues(
    model: NetworkModel,
    snapshot: NetworkRuntimeSnapshot,
    directory: Path,
    previous: NetworkModel | None = None,
) -> list[str]:
    issues = topology_issues(model, snapshot)
    links = {link.name: link for link in snapshot.links}
    dynamic = {
        i.device
        for i in model.interfaces
        if i.addressing.ipv4.mode is IPv4AddressMode.DHCP
        or i.addressing.ipv6.mode in (IPv6AddressMode.DHCP6, IPv6AddressMode.SLAAC)
    }
    for device in model.devices:
        link = links.get(device.name)
        if link is None:
            continue
        expected = str(directory / f"10-kavernis-{device.id}.network")
        if link.network_file != expected:
            issues.append(
                f"device {device.name}: expected network file {expected}, "
                f"observed {link.network_file}"
            )
        if link.dropins:
            issues.append(
                f"device {device.name}: unverified networkd drop-ins {link.dropins}"
            )
        accepted = (
            {"configured", "configuring"} if device.id in dynamic else {"configured"}
        )
        if link.setup_state not in accepted:
            issues.append(f"device {device.name}: networkd setup {link.setup_state}")
    for name, address in sorted(static_addresses(model)):
        if name not in links or address not in links[name].addresses:
            issues.append(f"device {name}: missing static address {address}")
        elif (
            address in links[name].dynamic_addresses
            or address not in links[name].configured_static_addresses
        ):
            issues.append(
                f"device {name}: static address {address} lacks confirmed static "
                "networkd provenance/readiness (stale dynamic or pending configuration)"
            )
    for route in sorted(static_routes(model) - snapshot.routes, key=repr):
        issues.append(f"missing static route {route}")
    if previous is not None:
        for name, address in sorted(
            static_addresses(previous) - static_addresses(model)
        ):
            if (
                name in links
                and address in links[name].addresses
                and address not in links[name].dynamic_addresses
            ):
                issues.append(
                    f"device {name}: retired static address remains: {address}"
                )
        for route in sorted(
            (static_routes(previous) - static_routes(model)) & snapshot.routes, key=repr
        ):
            issues.append(f"retired static route remains: {route}")
    return issues


def verify_runtime(
    model: NetworkModel,
    inspector: NetworkRuntimeInspector,
    directory: Path,
    previous: NetworkModel | None = None,
    *,
    timeout: float = 15.0,
    interval: float = 0.1,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Verify activation/static intent, never DHCP/SLAAC acquisition or reachability."""
    if not all(isfinite(value) and value > 0 for value in (timeout, interval)):
        raise ValueError("verification timeout and interval must be positive")
    deadline = clock() + timeout
    issues = ["no runtime snapshot obtained"]
    while (remaining := deadline - clock()) > 0:
        snapshot = inspector.inspect(remaining)
        issues = convergence_issues(model, snapshot, directory, previous)
        if not issues and clock() <= deadline:
            return
        sleep(min(interval, max(0, deadline - clock())))
    raise RuntimeVerificationError(
        "network convergence timed out: " + "; ".join(issues)
    )
