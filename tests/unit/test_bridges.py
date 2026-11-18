from copy import deepcopy
from dataclasses import replace

import pytest
from kavernis.backends.networkd import (
    NetworkdConfiguration,
    NetworkdValidationError,
    validate,
)
from kavernis.core.network import plan_network
from kavernis.core.topology import resolve_topology
from kavernis.models.device import BridgeSettings
from network_helpers import desired, interface, physical
from network_helpers import resolved_model as model
from pydantic import ValidationError


def devices():
    return [
        physical("port-one", "eth1"),
        physical("port-two", "eth2"),
        {
            "id": "lan-bridge",
            "type": "bridge",
            "name": "br0",
            "bridge": {"members": ["port-one", "port-two"]},
        },
    ]


def test_bridge_pipeline():
    config = desired(
        devices(),
        [
            interface(
                "lan",
                "lan-bridge",
                ipv4={"mode": "static", "address": "192.168.10.1/24"},
                ipv6={"mode": "static", "address": "2001:db8::1/64"},
            )
        ],
    )
    resolved = model(config)
    assert resolved.devices[-1].settings == BridgeSettings(("port-one", "port-two"))
    candidate = plan_network(config)
    assert candidate.files["10-kavernis-lan-bridge.netdev"] == (
        "[NetDev]\nName=br0\nKind=bridge\n\n[Bridge]\nSTP=yes\n"
    )
    assert (
        "Address=192.168.10.1/24\nAddress=2001:db8::1/64\n"
        in candidate.files["10-kavernis-lan-bridge.network"]
    )
    for identifier, name in [("port-one", "eth1"), ("port-two", "eth2")]:
        assert candidate.files[f"10-kavernis-{identifier}.network"] == (
            f"[Match]\nName={name}\n\n[Network]\nIPv6AcceptRA=no\n"
            "DHCP=no\nLinkLocalAddressing=no\nBridge=br0\n"
        )


def test_unassigned_bridge_and_order_independence():
    data = devices()
    first = plan_network(desired(data))
    data[2]["bridge"]["members"].reverse()
    data.reverse()
    assert list(first.files.items()) == list(plan_network(desired(data)).files.items())
    assert (
        "DHCP=no\nLinkLocalAddressing=no\n"
        in first.files["10-kavernis-lan-bridge.network"]
    )


@pytest.mark.parametrize(
    "bridge",
    [
        {},
        {"members": []},
        {"members": ["port-one", "port-one"]},
        {"members": "port-one"},
        {"members": [42]},
        {"members": ["bad id"]},
        {"members": ["port-one"], "unknown": True},
        {"members": ["port-one"], "stp": "false"},
    ],
)
def test_reject_invalid_bridge_block(bridge):
    data = devices()
    data[2]["bridge"] = bridge
    with pytest.raises(ValidationError, match="bridge"):
        desired(data)


@pytest.mark.parametrize("member", ["missing", "eth1", "lan-bridge"])
def test_reject_invalid_bridge_member(member):
    data = devices()
    data[2]["bridge"]["members"] = [member]
    with pytest.raises(ValueError, match="does not exist|cycle"):
        plan_network(desired(data))


def test_reject_shared_member_and_nested_bridge():
    data = devices()
    second = deepcopy(data[2])
    second.update(id="other", name="br1")
    data.append(second)
    with pytest.raises(ValueError, match="conflicting bridge ownership"):
        plan_network(desired(data))
    second["bridge"]["members"] = ["lan-bridge"]
    with pytest.raises(ValueError, match="cannot be a bridge"):
        plan_network(desired(data))


@pytest.mark.parametrize(
    "families",
    [
        {},
        {"ipv4": {"mode": "dhcp"}},
        {"ipv4": {"mode": "static", "address": "192.168.10.2/24"}},
        {"ipv6": {"mode": "slaac"}},
        {"ipv6": {"mode": "dhcp6"}},
        {"ipv6": {"mode": "static", "address": "2001:db8::2/64"}},
    ],
)
def test_bridge_member_cannot_have_l3_assignment_even_if_disabled(families):
    with pytest.raises(ValueError, match="cannot have an L3 assignment"):
        plan_network(desired(devices(), [interface(device="port-one", **families)]))


@pytest.mark.parametrize("name", ["x" * 16, ".", "..", "br:0", "br0\nKind=vlan"])
def test_reject_invalid_bridge_device(name):
    data = devices()
    data[2]["name"] = name
    with pytest.raises(ValidationError, match="name"):
        desired(data)


def test_reject_bridge_and_vlan_on_same_device():
    data = devices()
    data[2]["vlan"] = {"parent": "port-one", "tag": 20}
    with pytest.raises(ValidationError):
        desired(data)


def test_bridge_supports_vlan_members_and_rejects_bridged_vlan_parent():
    data = devices()
    data.append(physical("trunk", "eth3"))
    data[0] = {
        "id": "port-one",
        "type": "vlan",
        "name": "eth3.20",
        "vlan": {"parent": "trunk", "tag": 20},
    }
    candidate = plan_network(desired(data))
    assert "VLAN=eth3.20\n" in candidate.files["10-kavernis-trunk.network"]
    assert "Bridge=br0\n" in candidate.files["10-kavernis-port-one.network"]
    assert "Kind=vlan\n" in candidate.files["10-kavernis-port-one.netdev"]
    data[2]["bridge"]["members"].append("trunk")
    with pytest.raises(ValueError, match="VLAN parent cannot be a bridge member"):
        plan_network(desired(data))
    data[2]["bridge"]["members"].remove("trunk")
    data[2]["bridge"]["members"].remove("port-one")
    data[0]["vlan"]["parent"] = "lan-bridge"
    with pytest.raises(ValueError, match="parent must be physical"):
        plan_network(desired(data))


def test_bridge_supports_dhcp_and_stp_disable():
    data = devices()
    data[2]["bridge"]["stp"] = False
    candidate = plan_network(
        desired(
            data,
            [
                interface(
                    device="lan-bridge", ipv4={"mode": "dhcp"}, ipv6={"mode": "dhcp6"}
                )
            ],
        )
    )
    assert "STP=no\n" in candidate.files["10-kavernis-lan-bridge.netdev"]
    assert "DHCP=yes\n" in candidate.files["10-kavernis-lan-bridge.network"]


@pytest.mark.parametrize(
    "members", [(), ("missing",), ("lan-bridge",), ("port-one", "port-one")]
)
def test_core_rejects_invalid_domain_bridge(members):
    resolved = model(desired(devices()))
    bad = replace(resolved.devices[-1], settings=BridgeSettings(members))
    with pytest.raises(ValueError):
        resolve_topology(replace(resolved, devices=(*resolved.devices[:-1], bad)))


@pytest.mark.parametrize(
    "content",
    [
        "[NetDev]\nName=br0\nKind=bridge\n",
        "[NetDev]\nName=br0\nKind=bridge\n\n[Bridge]\nSTP=invalid\n",
        "[NetDev]\nName=..\nKind=bridge\n\n[Bridge]\nSTP=yes\n",
        "[NetDev]\nName=br0\nKind=bridge\n\n[Bridge]\nSTP=yes",
    ],
)
def test_reject_malformed_bridge_netdev(content):
    with pytest.raises(NetworkdValidationError, match="invalid bridge"):
        validate(NetworkdConfiguration({"10-kavernis-lan-bridge.netdev": content}))
