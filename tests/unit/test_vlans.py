from copy import deepcopy
from pathlib import Path

import pytest
from kavernis.backends.networkd import (
    NetworkdConfiguration,
    NetworkdValidationError,
    validate,
)
from kavernis.config.interfaces import InterfacesConfig
from kavernis.config.loader import load_interfaces
from kavernis.config.resolver import resolve_interfaces
from kavernis.core.interfaces import plan_interfaces
from kavernis.models.interface import VLANSettings
from pydantic import ValidationError


def configuration() -> dict:
    return {
        "version": 1,
        "interfaces": [
            {
                "uid": "8f3a7c22-1c7d-4d6b-a901-000000000001",
                "id": "trunk",
                "name": "Trunk",
                "device": "eth1",
                "ipv4": {"mode": "disabled"},
                "ipv6": {"mode": "disabled"},
            },
            {
                "uid": "8f3a7c22-1c7d-4d6b-a901-000000000002",
                "id": "guest",
                "name": "Guests",
                "device": "eth1.20",
                "vlan": {"parent": "trunk", "tag": 20},
                "ipv4": {"mode": "static", "address": "192.168.20.1/24"},
                "ipv6": {"mode": "static", "address": "2001:db8:20::1/64"},
            },
        ],
    }


def test_vlan_pipeline() -> None:
    config = InterfacesConfig.model_validate(configuration())
    models = resolve_interfaces(config)
    assert models[0].vlan is None
    assert models[1].vlan == VLANSettings(parent="trunk", tag=20)
    candidate = plan_interfaces(config)
    assert candidate.files == {
        "10-kavernis-trunk.network": (
            "[Match]\nName=eth1\n\n[Network]\nIPv6AcceptRA=no\nVLAN=eth1.20\n"
        ),
        "10-kavernis-guest.netdev": (
            "[NetDev]\nName=eth1.20\nKind=vlan\n\n[VLAN]\nId=20\n"
        ),
        "10-kavernis-guest.network": (
            "[Match]\nName=eth1.20\n\n[Network]\nAddress=192.168.20.1/24\n"
            "Address=2001:db8:20::1/64\nIPv6AcceptRA=no\n"
        ),
    }


@pytest.mark.parametrize("tag", [1, 4094])
def test_vlan_tag_boundaries(tag: int) -> None:
    data = configuration()
    data["interfaces"][1]["vlan"]["tag"] = tag
    candidate = plan_interfaces(InterfacesConfig.model_validate(data))
    assert f"Id={tag}\n" in candidate.files["10-kavernis-guest.netdev"]


@pytest.mark.parametrize("tag", [0, -1, 4095, True, "20", 20.5, None])
def test_reject_invalid_tag(tag: object) -> None:
    data = configuration()
    data["interfaces"][1]["vlan"]["tag"] = tag
    with pytest.raises(ValidationError, match="vlan.tag"):
        InterfacesConfig.model_validate(data)


@pytest.mark.parametrize(
    "vlan",
    [{}, {"parent": "trunk"}, {"tag": 20}, {"parent": "trunk", "tag": 20, "extra": 1}],
)
def test_reject_incomplete_or_unknown_vlan_fields(vlan: dict) -> None:
    data = configuration()
    data["interfaces"][1]["vlan"] = vlan
    with pytest.raises(ValidationError, match="vlan"):
        InterfacesConfig.model_validate(data)


@pytest.mark.parametrize("parent", ["missing", "guest", "eth1"])
def test_reject_invalid_parent(parent: str) -> None:
    data = configuration()
    data["interfaces"][1]["vlan"]["parent"] = parent
    with pytest.raises(ValidationError, match="guest: vlan.parent"):
        InterfacesConfig.model_validate(data)


@pytest.mark.parametrize(
    "device", ["x" * 16, ".", "..", "eth1:20", "eth1\nKind=bridge"]
)
def test_reject_invalid_vlan_device(device: str) -> None:
    data = configuration()
    data["interfaces"][1]["device"] = device
    with pytest.raises(ValidationError, match="device"):
        InterfacesConfig.model_validate(data)


def add_vlan(data: dict) -> dict:
    other = deepcopy(data["interfaces"][1])
    other.update(
        uid="8f3a7c22-1c7d-4d6b-a901-000000000003", id="staff", device="eth1.30"
    )
    data["interfaces"].append(other)
    return other


def test_reject_duplicate_vlan_on_parent() -> None:
    data = configuration()
    add_vlan(data)
    with pytest.raises(ValidationError, match="duplicate vlan.tag"):
        InterfacesConfig.model_validate(data)


def test_reject_nested_vlan() -> None:
    data = configuration()
    add_vlan(data)["vlan"] = {"parent": "guest", "tag": 30}
    with pytest.raises(ValidationError, match="physical interface"):
        InterfacesConfig.model_validate(data)


def test_multiple_vlans_are_deterministic_and_support_dhcp() -> None:
    data = configuration()
    other = add_vlan(data)
    other["vlan"]["tag"] = 30
    other["ipv4"] = {"mode": "dhcp"}
    other["ipv6"] = {"mode": "dhcp6"}
    first = plan_interfaces(InterfacesConfig.model_validate(data))
    data["interfaces"].reverse()
    second = plan_interfaces(InterfacesConfig.model_validate(data))
    assert list(first.files.items()) == list(second.files.items())
    assert "VLAN=eth1.20\nVLAN=eth1.30\n" in first.files["10-kavernis-trunk.network"]
    assert "DHCP=yes\n" in first.files["10-kavernis-staff.network"]


def test_same_tag_allowed_on_different_parents() -> None:
    data = configuration()
    other = add_vlan(data)
    parent = deepcopy(data["interfaces"][0])
    parent.update(
        uid="8f3a7c22-1c7d-4d6b-a901-000000000004", id="uplink", device="eth2"
    )
    data["interfaces"].append(parent)
    other["vlan"]["parent"] = "uplink"
    candidate = plan_interfaces(InterfacesConfig.model_validate(data))
    assert "VLAN=eth1.30" in candidate.files["10-kavernis-uplink.network"]
    assert "VLAN=eth1.30" not in candidate.files["10-kavernis-trunk.network"]


@pytest.mark.parametrize(
    "content",
    [
        "[NetDev]\nName=eth1.20\nKind=bridge\n\n[VLAN]\nId=20\n",
        "[NetDev]\nName=eth1.20\nKind=vlan\n\n[VLAN]\nId=4095\n",
        "[NetDev]\nName=eth1.20\nKind=vlan\n\n[VLAN]\n",
        "[NetDev]\nName=eth1.20\nKind=vlan\n\n[VLAN]\nId=20",
    ],
)
def test_reject_malformed_netdev(content: str) -> None:
    with pytest.raises(NetworkdValidationError, match="invalid (VLAN|bridge)"):
        validate(NetworkdConfiguration(files={"10-kavernis-guest.netdev": content}))


def test_load_vlan_example() -> None:
    config = load_interfaces(Path("configs/examples/interfaces-vlan.yml"))
    assert config.interfaces[1].vlan is not None
    assert len(plan_interfaces(config).files) == 3
