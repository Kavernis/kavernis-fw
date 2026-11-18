"""Preserved addressing modes and explicit DHCPv4 client policy."""

from ipaddress import IPv4Interface, IPv6Interface

import pytest
from kavernis.backends.networkd import generate
from kavernis.core.network import plan_network
from kavernis.models.interface import (
    DHCPv4Settings,
    IPv4AddressMode,
    IPv4Settings,
    IPv6AddressMode,
    IPv6Settings,
)
from network_helpers import desired, interface, physical
from network_helpers import resolved_model as model


def content(ipv4=None, ipv6=None):
    families = {}
    if ipv4 is not None:
        families["ipv4"] = ipv4
    if ipv6 is not None:
        families["ipv6"] = ipv6
    return plan_network(desired([physical()], [interface(**families)])).files[
        "10-kavernis-nic.network"
    ]


@pytest.mark.parametrize(
    "ipv4,ipv6,expected",
    [
        ({"mode": "static", "address": "192.0.2.1/24"}, None, "Address=192.0.2.1/24\n"),
        (
            None,
            {"mode": "static", "address": "2001:db8::1/64"},
            "Address=2001:db8::1/64\nIPv6AcceptRA=no\n",
        ),
        ({"mode": "dhcp"}, None, "DHCP=ipv4\n"),
        (None, {"mode": "dhcp6"}, "DHCP=ipv6\n"),
        ({"mode": "dhcp"}, {"mode": "dhcp6"}, "DHCP=yes\n"),
        (None, {"mode": "slaac"}, "IPv6AcceptRA=yes\nDHCP=no\n"),
        (None, None, "IPv6AcceptRA=no\nDHCP=no\nLinkLocalAddressing=no\n"),
    ],
)
def test_modes(ipv4, ipv6, expected):
    assert expected in content(ipv4, ipv6)


def test_omitted_and_disabled_families_are_equivalent():
    assert content() == content({"mode": "disabled"}, {"mode": "disabled"})


def test_dhcp_defaults_and_explicit_defaults_are_equivalent():
    defaults = {
        "use_hostname": False,
        "send_hostname": True,
        "use_dns": True,
        "use_routes": True,
        "use_ntp": True,
    }
    implicit = content({"mode": "dhcp"})
    assert implicit == content({"mode": "dhcp", "dhcp": defaults})
    assert "[DHCPv4]\nUseHostname=no\nSendHostname=yes\nUseDNS=yes\n" in implicit
    assert "UseRoutes=yes\nUseNTP=yes\n" in implicit
    assert "RouteMetric=" not in implicit
    assert "[Route]" not in implicit


def test_dhcp_overrides_render_every_option():
    native = content(
        {
            "mode": "dhcp",
            "dhcp": {
                "use_hostname": True,
                "send_hostname": False,
                "use_dns": False,
                "use_routes": False,
                "use_ntp": False,
                "route_metric": 200,
            },
        }
    )
    assert "UseHostname=yes\nSendHostname=no\nUseDNS=no\n" in native
    assert "UseRoutes=no\nUseNTP=no\nRouteMetric=200\n" in native


def test_incomplete_domain_addressing_cannot_generate():
    with pytest.raises(ValueError, match="requires an address"):
        IPv4Settings(IPv4AddressMode.STATIC)


@pytest.mark.parametrize("address", [IPv6Interface("2001:db8::1/64")])
def test_ipv4_static_requires_ipv4_interface(address):
    with pytest.raises((TypeError, ValueError)):
        IPv4Settings(IPv4AddressMode.STATIC, address)


@pytest.mark.parametrize("mode", [IPv4AddressMode.DHCP, IPv4AddressMode.DISABLED])
def test_nonstatic_ipv4_modes_reject_address(mode):
    policy = (
        DHCPv4Settings(False, True, True, True, True, None)
        if mode is IPv4AddressMode.DHCP
        else None
    )
    with pytest.raises(ValueError):
        IPv4Settings(mode, IPv4Interface("192.0.2.1/24"), policy)


@pytest.mark.parametrize("mode", [IPv4AddressMode.STATIC, IPv4AddressMode.DISABLED])
def test_non_dhcp_ipv4_rejects_dhcp_policy(mode):
    address = IPv4Interface("192.0.2.1/24") if mode is IPv4AddressMode.STATIC else None
    policy = DHCPv4Settings(False, True, True, True, True, None)
    with pytest.raises(ValueError):
        IPv4Settings(mode, address, policy)


def test_ipv4_dhcp_requires_typed_policy():
    with pytest.raises(ValueError):
        IPv4Settings(IPv4AddressMode.DHCP)
    with pytest.raises(TypeError):
        IPv4Settings(IPv4AddressMode.DHCP, dhcp=object())


@pytest.mark.parametrize("metric", [-1, 4294967296, True, "10"])
def test_dhcp_metric_must_be_unsigned_32_bit_integer(metric):
    with pytest.raises((TypeError, ValueError)):
        DHCPv4Settings(False, True, True, True, True, metric)


def test_dhcp_policy_flags_must_be_booleans():
    with pytest.raises(TypeError):
        DHCPv4Settings(1, True, True, True, True, None)


def test_ipv6_modes_enforce_address_presence_and_family():
    with pytest.raises(ValueError):
        IPv6Settings(IPv6AddressMode.STATIC)
    for mode in (
        IPv6AddressMode.DHCP6,
        IPv6AddressMode.SLAAC,
        IPv6AddressMode.DISABLED,
    ):
        with pytest.raises(ValueError):
            IPv6Settings(mode, IPv6Interface("2001:db8::1/64"))
    with pytest.raises(TypeError):
        IPv6Settings(IPv6AddressMode.STATIC, IPv4Interface("192.0.2.1/24"))


def test_contradictory_domain_addressing_cannot_reach_generator():
    resolved = model(desired([physical()], [interface()]))
    with pytest.raises(ValueError):
        IPv4Settings(IPv4AddressMode.STATIC)
    assert generate(resolved).files
