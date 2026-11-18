from dataclasses import replace

import pytest
from kavernis.core.network import plan_network
from network_helpers import desired, interface, physical
from pydantic import ValidationError


def configuration(gateways=None, routes=None):
    return desired(
        [physical(), physical("servers-nic", "eth1")],
        [
            interface("wan", ipv4={"mode": "static", "address": "192.168.0.1/24"}),
            interface(
                "servers",
                "servers-nic",
                ipv4={"mode": "static", "address": "192.168.40.254/24"},
                ipv6={"mode": "static", "address": "2001:db8:40::1/64"},
            ),
        ],
        gateways
        if gateways is not None
        else [
            {"id": "internet", "interface": "wan", "address": "192.168.0.254"},
            {
                "id": "onlink",
                "interface": "servers",
                "address": "10.200.0.1",
                "onlink": True,
            },
            {"id": "router-v6", "interface": "servers", "address": "2001:db8:40::fe"},
            {"id": "direct", "interface": "servers"},
        ],
        routes
        if routes is not None
        else [
            {"id": "default-v4", "network": "0.0.0.0/0", "gateway": "internet"},
            {"id": "remote-v4", "network": "10.20.0.0/16", "gateway": "onlink"},
            {"id": "remote-v6", "network": "::/0", "gateway": "router-v6"},
            {"id": "direct-route", "network": "10.50.0.0/16", "gateway": "direct"},
        ],
    )


def test_routes_render_on_device_of_logical_interface_and_are_deterministic():
    config = configuration()
    candidate = plan_network(config)
    assert candidate.files["10-kavernis-nic.network"].endswith(
        "[Route]\nDestination=0.0.0.0/0\nGateway=192.168.0.254\n"
    )
    servers = candidate.files["10-kavernis-servers-nic.network"]
    assert (
        "Destination=10.20.0.0/16\nGateway=10.200.0.1\nGatewayOnLink=yes\n" in servers
    )
    assert "Destination=::/0\nGateway=2001:db8:40::fe\n" in servers
    assert "[Route]\nDestination=10.50.0.0/16\n\n[Route]" in servers
    reverse = replace(
        config,
        routing=config.routing.model_copy(
            update={
                "routes": tuple(reversed(config.routing.routes)),
                "gateways": tuple(reversed(config.routing.gateways)),
            }
        ),
    )
    assert candidate == plan_network(reverse)


@pytest.mark.parametrize(
    "gateway,error",
    [
        ({"id": "bad", "interface": "wan", "address": "10.0.0.1"}, "onlink: true"),
        ({"id": "bad", "interface": "missing"}, "does not exist"),
        ({"id": "bad", "interface": "nic"}, "does not exist"),
        (
            {"id": "bad", "interface": "wan", "address": "2001:db8::1", "onlink": True},
            "family is disabled",
        ),
    ],
)
def test_gateway_references_reachability_and_families(gateway, error):
    with pytest.raises(ValueError, match=error):
        plan_network(configuration(gateways=[gateway], routes=[]))


@pytest.mark.parametrize(
    "route,error",
    [
        ({"id": "bad", "network": "::/0", "gateway": "internet"}, "family"),
        ({"id": "bad", "network": "0.0.0.0/0", "gateway": "missing"}, "does not exist"),
    ],
)
def test_route_references_and_family(route, error):
    with pytest.raises(ValueError, match=error):
        plan_network(configuration(routes=[route]))


def test_direct_route_requires_enabled_family():
    config = configuration(
        gateways=[{"id": "direct", "interface": "wan"}],
        routes=[{"id": "bad", "network": "::/0", "gateway": "direct"}],
    )
    with pytest.raises(ValueError, match="family"):
        plan_network(config)


@pytest.mark.parametrize(
    "gateway",
    [
        {"id": "bad", "interface": "wan", "onlink": True},
        {"id": "bad", "interface": "wan", "onlink": "true"},
        {"id": "bad", "interface": "wan", "name": "  "},
        {"id": "bad", "interface": "wan", "description": ""},
        {"id": "bad", "interface": "wan", "address": "not-an-ip"},
    ],
)
def test_invalid_gateway_fields(gateway):
    with pytest.raises(ValidationError):
        configuration(gateways=[gateway], routes=[])


def test_duplicate_routing_ids_are_rejected():
    gateway = {"id": "direct", "interface": "wan"}
    route = {"id": "remote", "network": "10.0.0.0/8", "gateway": "direct"}
    with pytest.raises(ValidationError, match="duplicate"):
        configuration(gateways=[gateway, gateway], routes=[])
    with pytest.raises(ValidationError, match="duplicate"):
        configuration(gateways=[gateway], routes=[route, route])


def test_dhcp_use_routes_does_not_suppress_static_routes():
    config = desired(
        [physical()],
        [interface(ipv4={"mode": "dhcp", "dhcp": {"use_routes": False}})],
        [{"id": "direct", "interface": "lan"}],
        [{"id": "private", "network": "10.0.0.0/8", "gateway": "direct"}],
    )
    native = plan_network(config).files["10-kavernis-nic.network"]
    assert "UseRoutes=no\n" in native
    assert "[Route]\nDestination=10.0.0.0/8\n" in native


def test_dhcp_gateway_requires_explicit_onlink_without_static_subnet():
    devices = [physical()]
    interfaces = [interface(ipv4={"mode": "dhcp"})]
    gateway = {"id": "router", "interface": "lan", "address": "192.0.2.1"}
    with pytest.raises(ValueError, match="onlink"):
        plan_network(desired(devices, interfaces, [gateway]))
    plan_network(desired(devices, interfaces, [{**gateway, "onlink": True}]))


@pytest.mark.parametrize(
    "address,gateway",
    [
        ("192.168.10.0/31", "192.168.10.1"),
    ],
)
def test_ipv4_rfc3021_peer_gateway_is_valid(address, gateway):
    config = desired(
        [physical()],
        [interface(ipv4={"mode": "static", "address": address})],
        [{"id": "router", "interface": "lan", "address": gateway}],
        [],
    )
    assert "Address=" + address in plan_network(config).files[
        "10-kavernis-nic.network"
    ]


def test_ipv4_rfc3021_gateway_cannot_be_interface_address():
    config = desired(
        [physical()],
        [interface(ipv4={"mode": "static", "address": "192.168.10.0/31"})],
        [{"id": "router", "interface": "lan", "address": "192.168.10.0"}],
        [],
    )
    with pytest.raises(ValueError, match="cannot be interface address"):
        plan_network(config)


@pytest.mark.parametrize("onlink", [False, True])
def test_ipv4_host_prefix_gateway_cannot_be_interface_address(onlink):
    config = desired(
        [physical()],
        [interface(ipv4={"mode": "static", "address": "192.168.10.1/32"})],
        [
            {
                "id": "router",
                "interface": "lan",
                "address": "192.168.10.1",
                "onlink": onlink,
            }
        ],
        [],
    )
    with pytest.raises(ValueError, match="cannot be interface address"):
        plan_network(config)


def test_ipv4_host_prefix_external_gateway_requires_onlink():
    config = desired(
        [physical()],
        [interface(ipv4={"mode": "static", "address": "192.168.10.1/32"})],
        [
            {
                "id": "router",
                "interface": "lan",
                "address": "192.168.10.254",
                "onlink": True,
            }
        ],
        [],
    )
    plan_network(config)
    without_onlink = desired(
        [physical()],
        [interface(ipv4={"mode": "static", "address": "192.168.10.1/32"})],
        [{"id": "router", "interface": "lan", "address": "192.168.10.254"}],
        [],
    )
    with pytest.raises(ValueError, match="onlink: true"):
        plan_network(without_onlink)


@pytest.mark.parametrize(
    "address,gateway",
    [
        ("192.0.2.1/24", "192.0.2.0"),
        ("192.0.2.1/24", "192.0.2.255"),
        ("192.0.2.1/24", "192.0.2.1"),
        ("192.0.2.1/24", "0.0.0.0"),
        ("192.0.2.1/24", "224.0.0.1"),
        ("192.0.2.1/24", "255.255.255.255"),
    ],
)
def test_onlink_does_not_bypass_invalid_ipv4_next_hop(address, gateway):
    config = desired(
        [physical()],
        [interface(ipv4={"mode": "static", "address": address})],
        [{"id": "router", "interface": "lan", "address": gateway, "onlink": True}],
        [],
    )
    with pytest.raises(ValueError, match="invalid|cannot be interface"):
        plan_network(config)


def test_ipv6_link_local_gateway_is_valid_without_onlink_and_renders_on_interface():
    config = desired(
        [physical()],
        [interface(ipv6={"mode": "static", "address": "2001:db8::2/64"})],
        [{"id": "router", "interface": "lan", "address": "fe80::1"}],
        [{"id": "default", "network": "::/0", "gateway": "router"}],
    )
    native = plan_network(config).files["10-kavernis-nic.network"]
    assert "Gateway=fe80::1\n" in native


@pytest.mark.parametrize(
    "gateway",
    ["2001:db8::2", "fe80::1"],
)
def test_distinct_ipv6_gateway_is_valid(gateway):
    config = desired(
        [physical()],
        [interface(ipv6={"mode": "static", "address": "2001:db8::1/64"})],
        [{"id": "router", "interface": "lan", "address": gateway}],
        [],
    )
    assert plan_network(config).files["10-kavernis-nic.network"]


def test_ipv6_gateway_cannot_be_interface_address():
    config = desired(
        [physical()],
        [interface(ipv6={"mode": "static", "address": "2001:db8::1/64"})],
        [{"id": "router", "interface": "lan", "address": "2001:db8::1"}],
        [],
    )
    with pytest.raises(ValueError, match="cannot be interface address"):
        plan_network(config)


@pytest.mark.parametrize("gateway", ["::", "ff02::1"])
def test_onlink_does_not_bypass_invalid_ipv6_next_hop(gateway):
    config = desired(
        [physical()],
        [interface(ipv6={"mode": "static", "address": "2001:db8::2/64"})],
        [{"id": "router", "interface": "lan", "address": gateway, "onlink": True}],
        [],
    )
    with pytest.raises(ValueError, match="invalid next-hop"):
        plan_network(config)
