"""Strict L3 addressing schemas, independent of device topology."""

import pytest
from kavernis.config.errors import ConfigurationError
from kavernis.config.network import (
    DevicesConfig,
    LogicalInterfacesConfig,
    RoutingConfig,
    parse_document,
)
from network_helpers import interface
from pydantic import ValidationError


def parse(**families):
    return LogicalInterfacesConfig.model_validate(
        {"version": 2, "interfaces": [interface(**families)]}
    ).interfaces[0]


@pytest.mark.parametrize(
    "model", [DevicesConfig, LogicalInterfacesConfig, RoutingConfig]
)
@pytest.mark.parametrize("version", [1, 3, True, "2", 2.0, None])
def test_strict_version(model, version):
    with pytest.raises(ValidationError, match="version"):
        model.model_validate({"version": version})


@pytest.mark.parametrize(
    "families",
    [
        {"ipv4": {"mode": "static"}},
        {"ipv4": {"mode": "dhcp", "address": "192.0.2.1/24"}},
        {"ipv4": {"mode": "disabled", "address": "192.0.2.1/24"}},
        {"ipv4": {"mode": "static", "address": "2001:db8::1/64"}},
        {"ipv6": {"mode": "static"}},
        {"ipv6": {"mode": "static", "address": "192.0.2.1/24"}},
        {"ipv6": {"mode": "slaac", "address": "2001:db8::1/64"}},
        {"ipv6": {"mode": "dhcp6", "address": "2001:db8::1/64"}},
        {"ipv6": {"mode": "disabled", "address": "2001:db8::1/64"}},
        {"ipv4": {"mode": "dhcp6"}},
        {"ipv6": {"mode": "dhcp"}},
        {"ipv4": {"mode": "dhcp", "unknown": True}},
        {"ipv6": None},
    ],
)
def test_invalid_addressing(families):
    with pytest.raises(ValidationError):
        parse(**families)


def test_missing_address_families_default_to_disabled():
    config = parse()
    assert config.ipv4.mode.value == config.ipv6.mode.value == "disabled"
    assert LogicalInterfacesConfig(version=2, interfaces=()).interfaces == ()


@pytest.mark.parametrize("field", ["vlan", "bridge", "uid", "routes", "unexpected"])
def test_logical_interface_rejects_non_l3_fields(field):
    with pytest.raises(ValidationError, match=field):
        parse(**{field: {}})


@pytest.mark.parametrize(
    "field,value", [("id", "Invalid"), ("device", "eth0.1"), ("name", "  ")]
)
def test_interface_identifiers_and_display_name(field, value):
    item = interface()
    item[field] = value
    with pytest.raises(ValidationError):
        LogicalInterfacesConfig.model_validate({"version": 2, "interfaces": [item]})


def test_dhcp_defaults_and_partial_override():
    defaults = parse(ipv4={"mode": "dhcp"}).ipv4.dhcp
    assert defaults.model_dump() == {
        "use_hostname": False,
        "send_hostname": True,
        "use_dns": True,
        "use_routes": True,
        "use_ntp": True,
        "route_metric": None,
    }
    partial = parse(ipv4={"mode": "dhcp", "dhcp": {"use_dns": False}}).ipv4.dhcp
    assert partial == defaults.model_copy(update={"use_dns": False})


@pytest.mark.parametrize("metric", [-1, 4294967296, True, "200", 2.5])
def test_invalid_dhcp_metric(metric):
    with pytest.raises(ValidationError, match="route_metric"):
        parse(ipv4={"mode": "dhcp", "dhcp": {"route_metric": metric}})


@pytest.mark.parametrize("mode", ["static", "disabled"])
def test_dhcp_policy_requires_dhcp_mode(mode):
    config = {"mode": mode, "dhcp": {"use_dns": False}}
    if mode == "static":
        config["address"] = "192.0.2.1/24"
    with pytest.raises(ValidationError, match="dhcp"):
        parse(ipv4=config)


@pytest.mark.parametrize(
    "field", ["use_hostname", "send_hostname", "use_dns", "use_routes", "use_ntp"]
)
def test_dhcp_booleans_are_strict(field):
    with pytest.raises(ValidationError):
        parse(ipv4={"mode": "dhcp", "dhcp": {field: "false"}})


@pytest.mark.parametrize("contents", [b"interfaces: [", b"\xff", b"null", b"[]"])
def test_parser_reports_source_on_invalid_document(contents):
    with pytest.raises(ConfigurationError, match="edited interfaces"):
        parse_document(contents, LogicalInterfacesConfig, "edited interfaces")


@pytest.mark.parametrize(
    "contents,duplicate_line",
    [
        (
            b"version: 2\nversion: 2\ninterfaces: []\n",
            2,
        ),
        (
            b"version: 2\ninterfaces:\n- id: lan\n  name: LAN\n  device: nic\n"
            b"  ipv4:\n    mode: disabled\n    mode: dhcp\n",
            8,
        ),
        (
            b"version: 2\ninterfaces:\n- id: lan\n  name: LAN\n  device: nic\n"
            b"- id: wan\n  name: WAN\n  device: nic2\n  ipv6:\n"
            b"    mode: slaac\n    mode: dhcp6\n",
            10,
        ),
    ],
)
def test_parser_rejects_duplicate_keys_with_source_and_line(contents, duplicate_line):
    source = "network/interfaces.yaml"
    with pytest.raises(ConfigurationError) as error:
        parse_document(contents, LogicalInterfacesConfig, source)
    message = str(error.value)
    assert source in message
    assert f"line {duplicate_line}" in message


def test_parser_rejects_duplicate_routing_keys_in_sequence():
    contents = (
        b"version: 2\ngateways:\n- id: router\n  interface: lan\n"
        b"  address: 192.0.2.1\nroutes:\n- id: route\n"
        b"  network: 10.0.0.0/8\n  gateway: router\n  gateway: other\n"
    )
    with pytest.raises(ConfigurationError, match=r"(?s)routing.yaml.*line 10"):
        parse_document(contents, RoutingConfig, "network/routing.yaml")


def test_yaml_merge_defaults_allow_explicit_override():
    contents = (
        b"version: 2\ninterfaces:\n- id: lan\n  name: LAN\n  device: nic\n"
        b"  ipv4: &ipv4_defaults\n    mode: dhcp\n    dhcp: {use_dns: true}\n"
        b"- id: wan\n  name: WAN\n  device: nic2\n  ipv4:\n"
        b"    <<: *ipv4_defaults\n    dhcp: {use_dns: false}\n"
    )
    parsed = parse_document(contents, LogicalInterfacesConfig, "interfaces.yaml")
    assert parsed.interfaces[0].ipv4.dhcp is not None
    assert parsed.interfaces[0].ipv4.dhcp.use_dns is True
    assert parsed.interfaces[1].ipv4.dhcp is not None
    assert parsed.interfaces[1].ipv4.dhcp.use_dns is False


def test_yaml_merge_does_not_hide_duplicate_explicit_keys():
    contents = (
        b"version: 2\ninterfaces:\n- id: lan\n  name: LAN\n  device: nic\n"
        b"  ipv4: &ipv4_defaults {mode: dhcp}\n- id: wan\n  name: WAN\n"
        b"  device: nic2\n  ipv4:\n    <<: *ipv4_defaults\n"
        b"    mode: slaac\n    mode: dhcp\n"
    )
    with pytest.raises(ConfigurationError, match=r"(?s)interfaces.yaml.*line 13"):
        parse_document(contents, LogicalInterfacesConfig, "interfaces.yaml")
