"""Reload-only safety classification uses IDs and dependencies, not artifacts."""

from dataclasses import replace

import pytest
from kavernis.backends.network_runtime import NetworkRuntimeSnapshot, RuntimeLink
from kavernis.core.network_transition import (
    TransitionKind,
    TransitionOperationKind,
    UnsupportedNetworkTransition,
    classify_transitions,
    draft_transition,
    plan_transition,
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
        replace(BRIDGE, settings=BridgeSettings(("nic-two",), False)),
    ],
)
def test_structural_replacement_has_ordered_lifecycle_plan(device):
    proposed = replace(
        PREVIOUS,
        devices=tuple(device if d.id == device.id else d for d in PREVIOUS.devices),
    )
    plan = plan_transition(PREVIOUS, proposed, OBSERVED)
    assert TransitionKind.REPLACE in {
        t.kind for t in classify_transitions(PREVIOUS, proposed)
    }
    kinds = [operation.kind for operation in plan.operations]
    assert TransitionOperationKind.DELETE in kinds
    assert TransitionOperationKind.RELOAD in kinds
    assert TransitionOperationKind.CREATE in kinds
    assert kinds.index(TransitionOperationKind.DELETE) < kinds.index(
        TransitionOperationKind.RELOAD
    )


@pytest.mark.parametrize("device", [VLAN, BRIDGE])
def test_managed_virtual_removal_has_lifecycle_plan(device):
    proposed = replace(
        PREVIOUS, devices=tuple(d for d in PREVIOUS.devices if d != device)
    )
    plan = plan_transition(PREVIOUS, proposed, OBSERVED)
    assert any(
        operation.kind is TransitionOperationKind.DELETE
        and operation.device_id == device.id
        for operation in plan.operations
    )


def test_physical_removal_remains_rejected():
    proposed = replace(PREVIOUS, devices=tuple(d for d in PREVIOUS.devices if d != NIC))
    with pytest.raises(UnsupportedNetworkTransition, match="physical device"):
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


@pytest.mark.parametrize(
    "foreign",
    [
        RuntimeLink("foreign0", "vlan", "eth0", 99),
        RuntimeLink("foreign0", "bridge", stp=True),
        RuntimeLink("foreign0", "veth"),
        RuntimeLink("foreign0"),
    ],
)
def test_virtual_rename_cannot_claim_existing_foreign_link(foreign):
    desired = NetworkModel(
        (NIC, replace(VLAN, name="foreign0")),
        (),
    )
    observed = NetworkRuntimeSnapshot((RuntimeLink("eth0"), OBSERVED.links[2], foreign))
    with pytest.raises(UnsupportedNetworkTransition, match="ownership"):
        plan_transition(NetworkModel((NIC, VLAN), ()), desired, observed)


def test_swapped_virtual_names_are_rejected_before_native_mutation():
    other = NetworkDevice("other", "vlan40", VLANSettings("nic", 40))
    previous = NetworkModel((NIC, VLAN, other), ())
    desired = NetworkModel(
        (NIC, replace(VLAN, name="vlan40"), replace(other, name="vlan30")), ()
    )
    observed = NetworkRuntimeSnapshot(
        (
            RuntimeLink("eth0"),
            RuntimeLink("vlan30", "vlan", "eth0", 30),
            RuntimeLink("vlan40", "vlan", "eth0", 40),
        )
    )
    with pytest.raises(UnsupportedNetworkTransition, match="ownership"):
        plan_transition(previous, desired, observed)


def test_virtual_rename_to_confirmed_absent_name_has_scoped_recreate_plan():
    previous = NetworkModel((NIC, VLAN), ())
    desired = NetworkModel((NIC, replace(VLAN, name="vlan40")), ())
    observed = NetworkRuntimeSnapshot(
        (RuntimeLink("eth0"), RuntimeLink("vlan30", "vlan", "eth0", 30))
    )
    plan = plan_transition(previous, desired, observed)
    assert plan.recovery_delete_names == ("vlan30", "vlan40")
    assert [operation.kind for operation in plan.operations][:2] == [
        TransitionOperationKind.DELETE,
        TransitionOperationKind.RELOAD,
    ]


def test_vlan_tag_reuse_deletes_only_the_retired_identity_before_creation():
    previous = NetworkModel((NIC, VLAN), ())
    replacement = NetworkDevice("replacement", "vlan100", VLANSettings("nic", 30))
    desired = NetworkModel((NIC, replacement), ())
    observed = NetworkRuntimeSnapshot(
        (RuntimeLink("eth0"), RuntimeLink("vlan30", "vlan", "eth0", 30))
    )
    plan = plan_transition(previous, desired, observed)
    deletes = [
        operation.device_name
        for operation in plan.operations
        if operation.kind is TransitionOperationKind.DELETE
    ]
    creates = [
        operation.device_name
        for operation in plan.operations
        if operation.kind is TransitionOperationKind.CREATE
    ]
    assert deletes == ["vlan30"]
    assert creates == ["vlan100"]


@pytest.mark.parametrize(
    "previous, desired",
    [
        (
            NetworkModel((NIC, VLAN), ()),
            NetworkModel(
                (NIC, NetworkDevice("vlan", "vlan30", PhysicalSettings())), ()
            ),
        ),
        (
            NetworkModel(
                (NIC, NetworkDevice("virtual", "vlan40", VLANSettings("nic", 40))), ()
            ),
            NetworkModel(
                (NIC, NetworkDevice("virtual", "vlan40", PhysicalSettings())), ()
            ),
        ),
        (
            NetworkModel(
                (NIC, NetworkDevice("physical", "eth1", PhysicalSettings())), ()
            ),
            NetworkModel(
                (NIC, NetworkDevice("physical", "eth1", VLANSettings("nic", 40))), ()
            ),
        ),
    ],
)
def test_device_kind_changes_are_rejected(previous, desired):
    with pytest.raises(UnsupportedNetworkTransition, match="cannot change kind"):
        draft_transition(previous, desired)


def test_recovery_cleanup_excludes_unaffected_managed_virtual_devices():
    unrelated = NetworkDevice("other-vlan", "vlan200", VLANSettings("nic", 200))
    previous = NetworkModel((NIC, VLAN, unrelated), ())
    desired = NetworkModel(
        (NIC, replace(VLAN, settings=VLANSettings("nic", 40)), unrelated), ()
    )
    observed = NetworkRuntimeSnapshot(
        (
            RuntimeLink("eth0"),
            RuntimeLink("vlan30", "vlan", "eth0", 30),
            RuntimeLink("vlan200", "vlan", "eth0", 200),
        )
    )
    plan = plan_transition(previous, desired, observed)
    assert plan.recovery_delete_names == ("vlan30",)


def test_bridge_membership_change_uses_targeted_attachment_not_recreation():
    physical = NetworkDevice("port", "eth2", PhysicalSettings())
    previous = NetworkModel(
        (NIC, physical, NetworkDevice("bridge", "br0", BridgeSettings(("nic",)))), ()
    )
    desired = NetworkModel(
        (
            NIC,
            physical,
            NetworkDevice("bridge", "br0", BridgeSettings(("nic", "port"))),
        ),
        (),
    )
    observed = NetworkRuntimeSnapshot(
        (
            RuntimeLink("eth0", master="br0"),
            RuntimeLink("eth2"),
            RuntimeLink("br0", "bridge", stp=True),
        )
    )
    plan = plan_transition(previous, desired, observed)
    assert not any(
        operation.kind is TransitionOperationKind.DELETE
        and operation.device_id == "bridge"
        for operation in plan.operations
    )
    assert any(
        operation.kind is TransitionOperationKind.ATTACH
        and operation.device_id == "port"
        for operation in plan.operations
    )


def test_new_bridge_attachment_to_applied_unmastered_physical_is_supported():
    old = NetworkModel((NIC2,), ())
    new = NetworkModel((NIC2, BRIDGE), ())
    result = validate_transition(
        old, new, NetworkRuntimeSnapshot((RuntimeLink("eth1"),))
    )
    assert {t.kind for t in result} == {TransitionKind.NEW, TransitionKind.RELOAD}
