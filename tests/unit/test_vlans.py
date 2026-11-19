from copy import deepcopy

import pytest
from kavernis.backends.networkd import (
    NetworkdConfiguration,
    NetworkdValidationError,
    validate,
)
from kavernis.core.network import plan_network
from network_helpers import desired, interface, physical
from pydantic import ValidationError


def devices():
    return [
        physical("trunk", "eth1"),
        {
            "id": "guest",
            "name": "eth1.20",
            "type": "vlan",
            "vlan": {"parent": "trunk", "tag": 20},
        },
    ]


def test_vlan_pipeline_with_unassigned_parent():
    config = desired(
        devices(),
        [
            interface(
                "guests",
                "guest",
                ipv4={"mode": "static", "address": "192.168.20.1/24"},
                ipv6={"mode": "static", "address": "2001:db8:20::1/64"},
            )
        ],
    )
    candidate = plan_network(config)
    assert candidate.files == {
        "10-kavernis-trunk.network": (
            "[Match]\nName=eth1\n\n[Network]\nIPv6AcceptRA=no\n"
            "DHCP=no\nLinkLocalAddressing=no\nVLAN=eth1.20\n"
        ),
        "10-kavernis-guest.netdev": (
            "[NetDev]\nName=eth1.20\nKind=vlan\n\n[VLAN]\nId=20\n"
        ),
        "10-kavernis-guest.network": (
            "[Match]\nName=eth1.20\n\n[Network]\nAddress=192.168.20.1/24\n"
            "Address=2001:db8:20::1/64\nIPv6AcceptRA=no\n"
            "DHCP=no\nLinkLocalAddressing=ipv6\n"
        ),
    }


@pytest.mark.parametrize("tag", [1, 4094])
def test_vlan_tag_boundaries(tag):
    data = devices()
    data[1]["vlan"]["tag"] = tag
    assert (
        f"Id={tag}\n" in plan_network(desired(data)).files["10-kavernis-guest.netdev"]
    )


@pytest.mark.parametrize("tag", [0, -1, 4095, True, "20", 20.5, None])
def test_reject_invalid_tag(tag):
    data = devices()
    data[1]["vlan"]["tag"] = tag
    with pytest.raises(ValidationError, match="vlan.tag"):
        desired(data)


@pytest.mark.parametrize(
    "vlan",
    [
        {},
        {"parent": "trunk"},
        {"tag": 20},
        {"parent": "trunk", "tag": 20, "extra": 1},
    ],
)
def test_reject_incomplete_or_unknown_vlan_fields(vlan):
    data = devices()
    data[1]["vlan"] = vlan
    with pytest.raises(ValidationError, match="vlan"):
        desired(data)


@pytest.mark.parametrize(
    "parent,error",
    [
        ("missing", "does not exist"),
        ("eth1", "does not exist"),
        ("guest", "cycle"),
    ],
)
def test_reject_invalid_parent(parent, error):
    data = devices()
    data[1]["vlan"]["parent"] = parent
    with pytest.raises(ValueError, match=error):
        plan_network(desired(data))


@pytest.mark.parametrize(
    "name", ["x" * 16, ".", "..", "-vlan20", "eth1:20", "eth1\nKind=bridge"]
)
def test_reject_invalid_vlan_device_name(name):
    data = devices()
    data[1]["name"] = name
    with pytest.raises(ValidationError, match="name"):
        desired(data)


def second_vlan(data, tag=30, parent="trunk"):
    item = deepcopy(data[1])
    item.update(id="staff", name="eth1.30", vlan={"parent": parent, "tag": tag})
    data.append(item)


def test_reject_duplicate_vlan_on_parent():
    data = devices()
    second_vlan(data, tag=20)
    with pytest.raises(ValueError, match="duplicate VLAN"):
        plan_network(desired(data))


def test_reject_nested_vlan_and_dependency_cycle():
    data = devices()
    second_vlan(data, parent="guest")
    with pytest.raises(ValueError, match="parent must be physical"):
        plan_network(desired(data))
    data[1]["vlan"]["parent"] = "staff"
    with pytest.raises(ValueError, match="cycle"):
        plan_network(desired(data))


def test_multiple_vlans_deterministic_with_addressed_parent_and_dhcp():
    data = devices()
    second_vlan(data)
    interfaces = [
        interface("uplink", "trunk", ipv4={"mode": "static", "address": "10.1.0.1/24"}),
        interface("staff-l3", "staff", ipv4={"mode": "dhcp"}, ipv6={"mode": "dhcp6"}),
    ]
    first = plan_network(desired(data, interfaces))
    second = plan_network(desired(list(reversed(data)), list(reversed(interfaces))))
    assert list(first.files.items()) == list(second.files.items())
    assert "VLAN=eth1.20\nVLAN=eth1.30\n" in first.files["10-kavernis-trunk.network"]
    assert "Address=10.1.0.1/24\n" in first.files["10-kavernis-trunk.network"]
    assert "DHCP=yes\n" in first.files["10-kavernis-staff.network"]


def test_same_tag_allowed_on_different_parents():
    data = devices()
    second_vlan(data, tag=20, parent="uplink")
    data.append(physical("uplink", "eth2"))
    candidate = plan_network(desired(data))
    assert "VLAN=eth1.30\n" in candidate.files["10-kavernis-uplink.network"]
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
def test_reject_malformed_netdev(content):
    with pytest.raises(NetworkdValidationError, match="invalid (VLAN|bridge)"):
        validate(NetworkdConfiguration({"10-kavernis-guest.netdev": content}))
