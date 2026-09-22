from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from kavernis.backends.networkd import (
    NetworkdConfiguration,
    NetworkdValidationError,
    generate,
    validate,
)
from kavernis.config.errors import ConfigurationError
from kavernis.config.interfaces import InterfacesConfig
from kavernis.config.loader import load_interfaces
from kavernis.config.resolver import resolve_interfaces
from kavernis.core.interfaces import plan_interfaces
from kavernis.models.interface import BridgeSettings
from pydantic import ValidationError


def configuration() -> dict[str, Any]:
    interfaces = [
        {
            "uid": f"8f3a7c22-1c7d-4d6b-a901-{index:012d}",
            "id": identifier,
            "name": identifier,
            "device": device,
            "ipv4": {"mode": "disabled"},
            "ipv6": {"mode": "disabled"},
        }
        for index, (identifier, device) in enumerate(
            [("lan-one", "eth1"), ("lan-two", "eth2"), ("lan", "br0")], start=1
        )
    ]
    interfaces[2]["bridge"] = {"members": ["lan-one", "lan-two"]}
    interfaces[2]["ipv4"] = {"mode": "static", "address": "192.168.10.1/24"}
    interfaces[2]["ipv6"] = {"mode": "static", "address": "2001:db8::1/64"}
    return {"version": 1, "interfaces": interfaces}


def test_bridge_pipeline() -> None:
    config = InterfacesConfig.model_validate(configuration())
    models = resolve_interfaces(config)
    assert models[0].bridge is None
    assert models[2].bridge == BridgeSettings(members=("lan-one", "lan-two"))
    candidate = plan_interfaces(config)
    assert candidate.files == {
        "10-kavernis-lan.netdev": (
            "[NetDev]\nName=br0\nKind=bridge\n\n[Bridge]\nSTP=yes\n"
        ),
        "10-kavernis-lan.network": (
            "[Match]\nName=br0\n\n[Network]\nAddress=192.168.10.1/24\n"
            "Address=2001:db8::1/64\nIPv6AcceptRA=no\n"
        ),
        **{
            f"10-kavernis-{identifier}.network": (
                f"[Match]\nName={device}\n\n[Network]\nIPv6AcceptRA=no\n"
                "DHCP=no\nLinkLocalAddressing=no\nBridge=br0\n"
            )
            for identifier, device in [("lan-one", "eth1"), ("lan-two", "eth2")]
        },
    }


def test_bridge_generation_is_order_independent() -> None:
    data = configuration()
    first = plan_interfaces(InterfacesConfig.model_validate(data))
    data["interfaces"][2]["bridge"]["members"].reverse()
    data["interfaces"].reverse()
    second = plan_interfaces(InterfacesConfig.model_validate(data))
    assert list(first.files.items()) == list(second.files.items())


@pytest.mark.parametrize(
    "bridge",
    [
        {},
        {"members": []},
        {"members": ["lan-one", "lan-one"]},
        {"members": "lan-one"},
        {"members": [42]},
        {"members": ["bad id"]},
        {"members": ["lan-one"], "unknown": True},
        {"members": ["lan-one"], "stp": "false"},
    ],
)
def test_reject_invalid_bridge_block(bridge: dict[str, Any]) -> None:
    data = configuration()
    data["interfaces"][2]["bridge"] = bridge
    with pytest.raises(ValidationError, match="bridge"):
        InterfacesConfig.model_validate(data)


@pytest.mark.parametrize("member", ["missing", "eth1", "lan"])
def test_reject_invalid_bridge_member(member: str) -> None:
    data = configuration()
    data["interfaces"][2]["bridge"]["members"] = [member]
    with pytest.raises(ValidationError, match="bridge.members"):
        InterfacesConfig.model_validate(data)


def test_reject_shared_member_and_nested_bridge() -> None:
    data = configuration()
    second = deepcopy(data["interfaces"][2])
    second.update(uid="8f3a7c22-1c7d-4d6b-a901-000000000004", id="other", device="br1")
    data["interfaces"].append(second)
    with pytest.raises(ValidationError, match="already belongs"):
        InterfacesConfig.model_validate(data)
    second["bridge"]["members"] = ["lan"]
    with pytest.raises(ValidationError, match="must not be a bridge"):
        InterfacesConfig.model_validate(data)


@pytest.mark.parametrize(
    ("family", "settings"),
    [
        ("ipv4", {"mode": "dhcp"}),
        ("ipv4", {"mode": "static", "address": "192.168.10.2/24"}),
        ("ipv6", {"mode": "slaac"}),
        ("ipv6", {"mode": "dhcp6"}),
        ("ipv6", {"mode": "static", "address": "2001:db8::2/64"}),
    ],
)
def test_reject_ip_addressing_on_members(family: str, settings: dict[str, str]) -> None:
    data = configuration()
    data["interfaces"][0][family] = settings
    with pytest.raises(ValidationError, match="configure IP addressing on the bridge"):
        InterfacesConfig.model_validate(data)


@pytest.mark.parametrize("device", ["x" * 16, ".", "..", "br:0", "br0\nKind=vlan"])
def test_reject_invalid_bridge_device(device: str) -> None:
    data = configuration()
    data["interfaces"][2]["device"] = device
    with pytest.raises(ValidationError, match="device"):
        InterfacesConfig.model_validate(data)


def test_reject_bridge_and_vlan_on_same_interface() -> None:
    data = configuration()
    data["interfaces"][2]["vlan"] = {"parent": "lan-one", "tag": 20}
    with pytest.raises(ValidationError, match="mutually exclusive"):
        InterfacesConfig.model_validate(data)


def test_bridge_supports_vlan_members() -> None:
    data = configuration()
    trunk = deepcopy(data["interfaces"][0])
    trunk.update(uid="8f3a7c22-1c7d-4d6b-a901-000000000004", id="trunk", device="eth3")
    data["interfaces"].append(trunk)
    data["interfaces"][0]["vlan"] = {"parent": "trunk", "tag": 20}
    data["interfaces"][0]["device"] = "eth3.20"
    candidate = plan_interfaces(InterfacesConfig.model_validate(data))
    assert "VLAN=eth3.20\n" in candidate.files["10-kavernis-trunk.network"]
    assert "Bridge=br0\n" in candidate.files["10-kavernis-lan-one.network"]
    assert "Kind=vlan\n" in candidate.files["10-kavernis-lan-one.netdev"]
    data["interfaces"][2]["bridge"]["members"].append("trunk")
    with pytest.raises(ValidationError, match="is a VLAN parent"):
        InterfacesConfig.model_validate(data)
    data["interfaces"][2]["bridge"]["members"].remove("trunk")
    data["interfaces"][0]["vlan"]["parent"] = "lan"
    with pytest.raises(ValidationError, match="physical interface"):
        InterfacesConfig.model_validate(data)


def test_bridge_supports_dhcp_and_explicit_stp_disable() -> None:
    data = configuration()
    bridge = data["interfaces"][2]
    bridge["bridge"]["stp"] = False
    bridge["ipv4"] = {"mode": "dhcp"}
    bridge["ipv6"] = {"mode": "dhcp6"}
    candidate = plan_interfaces(InterfacesConfig.model_validate(data))
    assert "STP=no\n" in candidate.files["10-kavernis-lan.netdev"]
    assert "DHCP=yes\n" in candidate.files["10-kavernis-lan.network"]


@pytest.mark.parametrize(
    "members", [(), ("missing",), ("lan",), ("lan-one", "lan-one")]
)
def test_backend_rejects_invalid_domain_bridge(members: tuple[str, ...]) -> None:
    models = resolve_interfaces(InterfacesConfig.model_validate(configuration()))
    models[2] = replace(models[2], bridge=BridgeSettings(members=members))
    with pytest.raises(NetworkdValidationError, match="invalid bridge"):
        generate(models)


@pytest.mark.parametrize(
    "content",
    [
        "[NetDev]\nName=br0\nKind=bridge\n",
        "[NetDev]\nName=br0\nKind=bridge\n\n[Bridge]\nSTP=invalid\n",
        "[NetDev]\nName=..\nKind=bridge\n\n[Bridge]\nSTP=yes\n",
        "[NetDev]\nName=br0\nKind=bridge\n\n[Bridge]\nSTP=yes",
    ],
)
def test_reject_malformed_bridge_netdev(content: str) -> None:
    with pytest.raises(NetworkdValidationError, match="invalid bridge"):
        validate(NetworkdConfiguration(files={"10-kavernis-lan.netdev": content}))


def test_load_bridge_example() -> None:
    config = load_interfaces(Path("configs/examples/interfaces-bridge.yml"))
    assert config.interfaces[2].bridge is not None
    assert len(plan_interfaces(config).files) == 4


def test_loader_reports_invalid_bridge(tmp_path: Path) -> None:
    import yaml

    data = configuration()
    data["interfaces"][2]["bridge"]["members"] = ["missing"]
    path = tmp_path / "interfaces.yml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ConfigurationError, match="lan: bridge.members 'missing'"):
        load_interfaces(path)
