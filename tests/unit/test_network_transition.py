"""Reload-only safety classification uses IDs and dependencies, not artifacts."""

from dataclasses import replace

import pytest
from kavernis.backends.network_runtime import NetworkRuntimeSnapshot, RuntimeLink
from kavernis.core.network_transition import (
    TransitionKind,
    UnsupportedNetworkTransition,
    classify_transitions,
    validate_transition,
)
from kavernis.models.device import (
    BridgeSettings,
    NetworkDevice,
    PhysicalSettings,
    VLANSettings,
)
from kavernis.models.network import NetworkModel

NIC = NetworkDevice("nic", "eth0", PhysicalSettings())
NIC2 = NetworkDevice("nic-two", "eth1", PhysicalSettings())
VLAN = NetworkDevice("vlan", "vlan30", VLANSettings("nic", 30))
BRIDGE = NetworkDevice("bridge", "br0", BridgeSettings(("nic-two",)))
PREVIOUS = NetworkModel((NIC, NIC2, VLAN, BRIDGE), ())
OBSERVED = NetworkRuntimeSnapshot(
    (
        RuntimeLink("eth0"),
        RuntimeLink("eth1", master="br0"),
        RuntimeLink("vlan30", "vlan", "eth0", 30),
        RuntimeLink("br0", "bridge", stp=True),
    )
)


@pytest.mark.parametrize(
    "device",
    [
        replace(VLAN, settings=VLANSettings("nic", 40)),
        replace(VLAN, settings=VLANSettings("nic-two", 30)),
        replace(VLAN, name="vlan40"),
        replace(VLAN, settings=BridgeSettings(("nic",))),
        replace(BRIDGE, settings=BridgeSettings(("vlan",))),
        replace(BRIDGE, settings=BridgeSettings(("nic-two",), False)),
    ],
)
def test_structural_replacement_is_rejected(device):
    proposed = replace(
        PREVIOUS,
        devices=tuple(device if d.id == device.id else d for d in PREVIOUS.devices),
    )
    with pytest.raises(UnsupportedNetworkTransition, match="recreation"):
        validate_transition(PREVIOUS, proposed, OBSERVED)
    assert TransitionKind.REPLACE in {
        t.kind for t in classify_transitions(PREVIOUS, proposed)
    }


@pytest.mark.parametrize("device", [VLAN, BRIDGE, NIC])
def test_removal_is_rejected(device):
    proposed = replace(
        PREVIOUS, devices=tuple(d for d in PREVIOUS.devices if d != device)
    )
    with pytest.raises(UnsupportedNetworkTransition, match="removal"):
        validate_transition(PREVIOUS, proposed, OBSERVED)


@pytest.mark.parametrize("virtual", [VLAN, BRIDGE])
def test_initial_virtual_creation_is_supported(virtual):
    initial = NetworkModel((NIC, NIC2, virtual), ())
    observed = NetworkRuntimeSnapshot((RuntimeLink("eth0"), RuntimeLink("eth1")))
    transitions = validate_transition(None, initial, observed)
    assert all(t.kind is TransitionKind.NEW for t in transitions)


def test_existing_topology_is_unchanged():
    assert all(
        t.kind is TransitionKind.UNCHANGED
        for t in validate_transition(PREVIOUS, PREVIOUS, OBSERVED)
    )


def test_observed_wrong_vlan_prevents_reload_even_if_desired_matches_applied():
    observed = replace(
        OBSERVED,
        links=tuple(
            replace(link, vlan_id=99) if link.name == "vlan30" else link
            for link in OBSERVED.links
        ),
    )
    with pytest.raises(UnsupportedNetworkTransition, match="observed 99"):
        validate_transition(PREVIOUS, PREVIOUS, observed)


def test_unowned_virtual_name_collision_is_rejected():
    with pytest.raises(UnsupportedNetworkTransition, match="ownership"):
        validate_transition(None, NetworkModel((NIC, VLAN), ()), OBSERVED)


def test_new_bridge_attachment_to_applied_unmastered_physical_is_supported():
    old = NetworkModel((NIC2,), ())
    new = NetworkModel((NIC2, BRIDGE), ())
    result = validate_transition(
        old, new, NetworkRuntimeSnapshot((RuntimeLink("eth1"),))
    )
    assert {t.kind for t in result} == {TransitionKind.NEW, TransitionKind.RELOAD}
