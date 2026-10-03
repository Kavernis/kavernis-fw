from copy import deepcopy
from pathlib import Path

import pytest
from kavernis.config.errors import ConfigurationError
from kavernis.config.interfaces import InterfacesConfig, IPv4Mode, IPv6Mode
from kavernis.config.loader import load_interfaces
from pydantic import ValidationError


def valid_configuration() -> dict[str, object]:
    return {
        "version": 1,
        "interfaces": [
            {
                "id": "lan",
                "name": "LAN",
                "device": "eth0",
                "ipv4": {"mode": "static", "address": "192.168.10.1/24"},
                "ipv6": {"mode": "slaac"},
            }
        ],
    }


def test_load_example_configuration() -> None:
    config = load_interfaces(Path("configs/examples/interfaces.yaml"))

    assert config.version == 1
    assert [interface.id for interface in config.interfaces] == ["lan", "wan"]
    assert str(config.interfaces[0].ipv4.address) == "192.168.0.1/24"


@pytest.mark.parametrize(
    ("mutation", "field"),
    [
        (lambda data: data["interfaces"][0]["ipv4"].pop("address"), "ipv4"),
        (
            lambda data: data["interfaces"][0]["ipv4"].update(
                {"mode": "dhcp", "address": "192.168.10.1/24"}
            ),
            "ipv4",
        ),
        (
            lambda data: data["interfaces"][0]["ipv4"].update(
                {"address": "2001:db8::1/64"}
            ),
            "ipv4",
        ),
        (lambda data: data["interfaces"][0].update({"unexpected": True}), "unexpected"),
    ],
)
def test_reject_invalid_ip_configuration(mutation: object, field: str) -> None:
    data = valid_configuration()
    mutation(data)  # type: ignore[operator]

    with pytest.raises(ValidationError, match=field):
        InterfacesConfig.model_validate(data)


def test_reject_duplicate_interface_id() -> None:
    data = valid_configuration()
    duplicate = deepcopy(data["interfaces"][0])
    data["interfaces"].append(duplicate)

    with pytest.raises(ValidationError, match="duplicate id"):
        InterfacesConfig.model_validate(data)


def test_reject_obsolete_uid_in_current_configuration() -> None:
    data = valid_configuration()
    data["interfaces"][0]["uid"] = "8f3a7c22-1c7d-4d6b-a901-000000000001"

    with pytest.raises(ValidationError, match="uid"):
        InterfacesConfig.model_validate(data)


def test_reject_empty_interfaces_configuration() -> None:
    with pytest.raises(ValidationError, match="at least one interface"):
        InterfacesConfig.model_validate({"version": 1, "interfaces": []})


def test_loader_exposes_context_for_invalid_yaml(tmp_path: Path) -> None:
    path = tmp_path / "interfaces.yml"
    path.write_text("interfaces: [", encoding="utf-8")

    with pytest.raises(
        ConfigurationError, match="cannot load interfaces configuration"
    ):
        load_interfaces(path)


def test_missing_address_families_default_to_disabled() -> None:
    config = InterfacesConfig.model_validate(
        {
            "version": 1,
            "interfaces": [{"id": "trunk", "name": "Trunk", "device": "eth1"}],
        }
    )

    assert config.interfaces[0].ipv4.mode is IPv4Mode.DISABLED
    assert config.interfaces[0].ipv6.mode is IPv6Mode.DISABLED


def test_dhcpv4_options_default_without_dhcp_subsection() -> None:
    data = valid_configuration()
    data["interfaces"][0]["ipv4"] = {"mode": "dhcp"}  # type: ignore[index]

    dhcp = InterfacesConfig.model_validate(data).interfaces[0].ipv4.dhcp

    assert dhcp.use_hostname is False
    assert dhcp.send_hostname is True
    assert dhcp.use_dns is True
    assert dhcp.use_routes is True
    assert dhcp.use_ntp is True
    assert dhcp.route_metric is None


def test_dhcpv4_options_allow_partial_and_complete_overrides() -> None:
    data = valid_configuration()
    data["interfaces"][0]["ipv4"] = {  # type: ignore[index]
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

    dhcp = InterfacesConfig.model_validate(data).interfaces[0].ipv4.dhcp

    assert dhcp.use_hostname is True
    assert dhcp.send_hostname is False
    assert dhcp.use_dns is False
    assert dhcp.use_routes is False
    assert dhcp.use_ntp is False
    assert dhcp.route_metric == 200

    data["interfaces"][0]["ipv4"] = {  # type: ignore[index]
        "mode": "dhcp",
        "dhcp": {"use_dns": False},
    }
    partial = InterfacesConfig.model_validate(data).interfaces[0].ipv4.dhcp
    assert partial.use_dns is False
    assert partial.use_hostname is False
    assert partial.send_hostname is True
    assert partial.use_routes is True
    assert partial.use_ntp is True
    assert partial.route_metric is None


@pytest.mark.parametrize(
    ("mode", "dhcp", "match"),
    [
        ("dhcp", {"route_metric": -1}, "route_metric"),
        ("dhcp", {"route_metric": 4294967296}, "route_metric"),
        ("static", {"use_dns": False}, "dhcp"),
        ("disabled", {"use_dns": False}, "dhcp"),
    ],
)
def test_dhcpv4_options_reject_invalid_mode_or_metric(
    mode: str, dhcp: dict[str, object], match: str
) -> None:
    data = valid_configuration()
    ipv4: dict[str, object] = {"mode": mode, "dhcp": dhcp}
    if mode == "static":
        ipv4["address"] = "192.168.10.1/24"
    data["interfaces"][0]["ipv4"] = ipv4  # type: ignore[index]

    with pytest.raises(ValidationError, match=match):
        InterfacesConfig.model_validate(data)
