from copy import deepcopy

import pytest
from kavernis.config.gateways import GatewaysConfig
from kavernis.config.interfaces import InterfacesConfig
from kavernis.config.resolver import resolve_network
from kavernis.config.routes import RoutesConfig
from kavernis.core.network import NetworkDesiredState, plan_network
from pydantic import ValidationError


def interfaces() -> InterfacesConfig:
    return InterfacesConfig.model_validate(
        {
            "version": 1,
            "interfaces": [
                {
                    "id": "wan",
                    "name": "WAN",
                    "device": "eth0",
                    "ipv4": {"mode": "static", "address": "192.168.0.1/24"},
                    "ipv6": {"mode": "disabled"},
                },
                {
                    "id": "servers",
                    "name": "Servers",
                    "device": "eth1",
                    "ipv4": {"mode": "static", "address": "192.168.40.254/24"},
                    "ipv6": {"mode": "static", "address": "2001:db8:40::1/64"},
                },
                {
                    "id": "vpn",
                    "name": "VPN",
                    "device": "wg0",
                    "ipv4": {"mode": "static", "address": "10.50.0.1/24"},
                    "ipv6": {"mode": "disabled"},
                },
            ],
        }
    )


def gateways() -> GatewaysConfig:
    return GatewaysConfig.model_validate(
        {
            "version": 1,
            "gateways": [
                {
                    "id": "bbox",
                    "name": "Bbox",
                    "interface": "wan",
                    "address": "192.168.0.254",
                },
                {
                    "id": "special",
                    "name": "Special",
                    "interface": "servers",
                    "address": "10.200.0.1",
                    "onlink": True,
                },
                {
                    "id": "router-v6",
                    "name": "IPv6",
                    "interface": "servers",
                    "address": "2001:db8:40::fe",
                },
                {"id": "vpn-direct", "name": "VPN", "interface": "vpn"},
            ],
        }
    )


def routes() -> RoutesConfig:
    return RoutesConfig.model_validate(
        {
            "version": 1,
            "routes": [
                {"id": "default-v4", "network": "0.0.0.0/0", "gateway": "bbox"},
                {"id": "remote-v4", "network": "10.20.0.0/16", "gateway": "special"},
                {
                    "id": "remote-v6",
                    "network": "2001:db8:20::/48",
                    "gateway": "router-v6",
                },
                {
                    "id": "vpn-network",
                    "network": "10.50.0.0/16",
                    "gateway": "vpn-direct",
                },
            ],
        }
    )


def test_gateway_and_route_config_validation() -> None:
    assert gateways().gateways[0].onlink is False
    assert str(routes().routes[2].network) == "2001:db8:20::/48"
    bad = deepcopy(gateways().model_dump(mode="json"))
    bad["gateways"].append(bad["gateways"][0])
    with pytest.raises(ValidationError, match="duplicate"):
        GatewaysConfig.model_validate(bad)
    with pytest.raises(ValidationError, match="onlink"):
        GatewaysConfig.model_validate(
            {
                "version": 1,
                "gateways": [
                    {
                        "id": "direct",
                        "name": "Direct",
                        "interface": "wan",
                        "onlink": True,
                    }
                ],
            }
        )


def test_resolver_validates_references_reachability_and_families() -> None:
    resolved = resolve_network(interfaces(), gateways(), routes())
    assert [route.id for route in resolved[1].routes] == ["remote-v4", "remote-v6"]
    bad_gateway = GatewaysConfig.model_validate(
        {
            "version": 1,
            "gateways": [
                {"id": "bad", "name": "Bad", "interface": "wan", "address": "10.0.0.1"}
            ],
        }
    )
    with pytest.raises(ValueError, match="onlink: true"):
        resolve_network(interfaces(), bad_gateway, RoutesConfig(routes=[]))
    bad_routes = RoutesConfig.model_validate(
        {"version": 1, "routes": [{"id": "bad", "network": "::/0", "gateway": "bbox"}]}
    )
    with pytest.raises(ValueError, match="family"):
        resolve_network(interfaces(), gateways(), bad_routes)
    unknown = GatewaysConfig.model_validate(
        {
            "version": 1,
            "gateways": [{"id": "bad", "name": "Bad", "interface": "missing"}],
        }
    )
    with pytest.raises(ValueError, match="does not exist"):
        resolve_network(interfaces(), unknown, RoutesConfig(routes=[]))


def test_networkd_renders_deterministic_routes() -> None:
    desired = NetworkDesiredState(interfaces(), gateways(), routes(), {})
    candidate = plan_network(desired)
    assert candidate.files["10-kavernis-wan.network"].endswith(
        "[Route]\nDestination=0.0.0.0/0\nGateway=192.168.0.254\n"
    )
    servers = candidate.files["10-kavernis-servers.network"]
    assert (
        "Destination=10.20.0.0/16\nGateway=10.200.0.1\nGatewayOnLink=yes\n" in servers
    )
    assert "Destination=2001:db8:20::/48\nGateway=2001:db8:40::fe\n" in servers
    assert candidate.files["10-kavernis-vpn.network"].endswith(
        "[Route]\nDestination=10.50.0.0/16\n"
    )
    assert candidate == plan_network(desired)
