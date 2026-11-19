"""Deterministic, Debian-networkd structural transition planning.

The plan is deliberately backend-neutral: it describes only Kavernis device
identities and dependencies. The systemd-networkd backend owns the native
`ip` and `networkctl` operations used to implement it.
"""

from dataclasses import dataclass, replace
from enum import StrEnum

from kavernis.backends.network_runtime import NetworkRuntimeSnapshot, topology_issues
from kavernis.models.device import (
    BridgeSettings,
    NetworkDevice,
    PhysicalSettings,
    VLANSettings,
)
from kavernis.models.network import NetworkModel


class UnsupportedNetworkTransition(ValueError):
    """A transition would recreate, remove, or overwrite a physical device."""


class TransitionKind(StrEnum):
    NEW = "new"
    UNCHANGED = "unchanged"
    RELOAD = "reload"
    REPLACE = "replacement required"
    REMOVE = "removal required"


class TransitionOperationKind(StrEnum):
    DETACH = "detach"
    DELETE = "delete"
    RELOAD = "reload"
    CREATE = "create"
    ATTACH = "attach"
    VERIFY = "verify"


@dataclass(frozen=True)
class DeviceTransition:
    device_id: str
    kind: TransitionKind
    detail: str


@dataclass(frozen=True)
class TransitionOperation:
    """One ordered, backend-neutral action shown in a live transition plan."""

    kind: TransitionOperationKind
    device_id: str
    device_name: str
    detail: str


@dataclass(frozen=True)
class NetworkTransitionPlan:
    """Validated operations and identities scoped to one apply transaction."""

    transitions: tuple[DeviceTransition, ...]
    operations: tuple[TransitionOperation, ...]
    previous_virtual_names: tuple[str, ...]
    candidate_virtual_names: tuple[str, ...]
    recovery_virtual_names: tuple[str, ...]

    @property
    def requires_lifecycle(self) -> bool:
        return any(
            operation.kind
            in (TransitionOperationKind.DETACH, TransitionOperationKind.DELETE)
            for operation in self.operations
        )

    @property
    def has_structural_operations(self) -> bool:
        """Whether this change needs device-level, rather than L3-only, work."""
        return bool(self.operations)

    @property
    def recovery_delete_names(self) -> tuple[str, ...]:
        """Only owned identities that recovery may safely delete."""
        return self.recovery_virtual_names


def classify_transitions(
    previous: NetworkModel | None,
    desired: NetworkModel,
) -> tuple[DeviceTransition, ...]:
    old = {device.id: device for device in previous.devices} if previous else {}
    new = {device.id: device for device in desired.devices}
    result = []
    for identifier in sorted(old.keys() | new.keys()):
        before, after = old.get(identifier), new.get(identifier)
        if before is None:
            kind, detail = TransitionKind.NEW, f"create/adopt {after}"
        elif after is None:
            kind, detail = TransitionKind.REMOVE, f"remove {before}"
        elif before != after:
            kind, detail = TransitionKind.REPLACE, f"change {before} to {after}"
        elif previous == desired:
            kind, detail = TransitionKind.UNCHANGED, "unchanged intent"
        else:
            kind, detail = TransitionKind.RELOAD, "same topology; reload L3 intent"
        result.append(DeviceTransition(identifier, kind, detail))
    return tuple(result)


def plan_transition(
    previous: NetworkModel | None,
    desired: NetworkModel,
    observed: NetworkRuntimeSnapshot,
) -> NetworkTransitionPlan:
    """Validate ownership and produce safe, dependency-ordered live operations."""
    draft = draft_transition(previous, desired)
    old = {device.id: device for device in previous.devices} if previous else {}
    new = {device.id: device for device in desired.devices}

    if previous:
        issues = topology_issues(previous, observed)
        if issues:
            raise UnsupportedNetworkTransition(
                "Applied topology does not match Linux; cannot safely plan a "
                "structural transition: " + "; ".join(issues)
            )

    links = {link.name: link for link in observed.links}
    previous_virtual_by_name = {
        device.name: device
        for device in old.values()
        if not isinstance(device.settings, PhysicalSettings)
    }
    for identifier in sorted(old.keys() | new.keys()):
        before, after = old.get(identifier), new.get(identifier)
        if before is None and after is not None:
            link = links.get(after.name)
            if isinstance(after.settings, PhysicalSettings):
                if link is None:
                    raise UnsupportedNetworkTransition(
                        f"physical device {after.name} is missing before apply"
                    )
                if link.kind in ("bridge", "vlan") or link.master is not None:
                    raise UnsupportedNetworkTransition(
                        f"physical device {after.name} has conflicting type/bridge "
                        "membership before adoption"
                    )
            elif link is not None:
                raise UnsupportedNetworkTransition(
                    f"new virtual device {after.name} already exists without an "
                    "applied identity; ownership cannot be established"
                )
        elif (
            before is not None
            and after is not None
            and not isinstance(after.settings, PhysicalSettings)
        ):
            link = links.get(after.name)
            established = previous_virtual_by_name.get(after.name)
            if link is not None and established is not before:
                raise UnsupportedNetworkTransition(
                    f"candidate virtual device {after.name} conflicts with an "
                    "existing link whose ownership cannot be established"
                )

    # Apply reaches this point only after every candidate identity was checked
    # against the pre-mutation snapshot. Recovery may therefore clean up only
    # names that were either applied identities or confirmed absent candidates.
    return replace(draft, recovery_virtual_names=_recovery_names(old, new))


def draft_transition(
    previous: NetworkModel | None,
    desired: NetworkModel,
) -> NetworkTransitionPlan:
    """Build a side-effect-free plan without claiming runtime applicability.

    This powers ``kavernis plan network`` before an appliance has an applied
    baseline.  Live apply always calls :func:`plan_transition`, which adds the
    observed-topology ownership checks before native mutation.
    """
    transitions = classify_transitions(previous, desired)
    old = {device.id: device for device in previous.devices} if previous else {}
    new = {device.id: device for device in desired.devices}

    for identifier in sorted(old.keys() | new.keys()):
        before, after = old.get(identifier), new.get(identifier)
        if before is not None and after is not None and before.kind is not after.kind:
            raise UnsupportedNetworkTransition(
                f"device '{identifier}' cannot change kind from "
                f"{before.kind.value} to {after.kind.value}"
            )
        if before is not None and isinstance(before.settings, PhysicalSettings):
            if after is None or after != before:
                raise UnsupportedNetworkTransition(
                    f"physical device '{identifier}' cannot be removed or recreated"
                )

    operations = _structural_operations(old, new)
    previous_virtual_names = tuple(
        sorted(
            device.name
            for device in old.values()
            if not isinstance(device.settings, PhysicalSettings)
        )
    )
    candidate_virtual_names = tuple(
        sorted(
            device.name
            for device in new.values()
            if not isinstance(device.settings, PhysicalSettings)
        )
    )
    return NetworkTransitionPlan(
        transitions,
        tuple(operations),
        previous_virtual_names,
        candidate_virtual_names,
        (),
    )


def validate_transition(
    previous: NetworkModel | None,
    desired: NetworkModel,
    observed: NetworkRuntimeSnapshot,
) -> tuple[DeviceTransition, ...]:
    """Backward-compatible validation entry point for callers needing labels."""
    return plan_transition(previous, desired, observed).transitions


def _structural_operations(
    old: dict[str, NetworkDevice], new: dict[str, NetworkDevice]
) -> list[TransitionOperation]:
    """Destroy dependants before dependencies; target networkd recreates them."""
    operations: list[TransitionOperation] = []
    affected_bridges = {
        identifier
        for identifier, device in old.items()
        if isinstance(device.settings, BridgeSettings)
        and (
            identifier not in new
            or not isinstance(new[identifier].settings, BridgeSettings)
            or _requires_bridge_recreation(device, new[identifier])
        )
    }
    detached: set[str] = set()

    # A bridge owns its member attachment, so detach every old member first.
    for identifier in sorted(affected_bridges):
        bridge = old[identifier]
        assert isinstance(bridge.settings, BridgeSettings)
        for member_id in bridge.settings.members:
            member = old[member_id]
            operations.append(
                TransitionOperation(
                    TransitionOperationKind.DETACH,
                    member_id,
                    member.name,
                    f"detach from bridge {bridge.name}",
                )
            )
            detached.add(member_id)
        operations.append(
            TransitionOperation(
                TransitionOperationKind.DELETE,
                identifier,
                bridge.name,
                "delete managed bridge before recreation/removal",
            )
        )

    # Bridge membership is a .network association, not a bridge-netdev
    # identity change. Apply only the changed attachments when STP/name/kind
    # stayed stable; recreating the bridge would interrupt every member.
    for identifier, bridge in sorted(old.items()):
        candidate = new.get(identifier)
        if (
            candidate is None
            or not isinstance(bridge.settings, BridgeSettings)
            or not isinstance(candidate.settings, BridgeSettings)
            or identifier in affected_bridges
        ):
            continue
        assert isinstance(candidate.settings, BridgeSettings)
        old_members = set(bridge.settings.members)
        new_members = set(candidate.settings.members)
        for member_id in sorted(old_members - new_members):
            member = old[member_id]
            operations.append(
                TransitionOperation(
                    TransitionOperationKind.DETACH,
                    member_id,
                    member.name,
                    f"detach from bridge {bridge.name}",
                )
            )
            detached.add(member_id)
        for member_id in sorted(new_members - old_members):
            member = new[member_id]
            operations.append(
                TransitionOperation(
                    TransitionOperationKind.ATTACH,
                    member_id,
                    member.name,
                    f"attach to existing bridge {bridge.name}",
                )
            )

    affected_vlans = {
        identifier
        for identifier, device in old.items()
        if isinstance(device.settings, VLANSettings)
        and (identifier not in new or new[identifier] != device)
    }
    for identifier in sorted(affected_vlans):
        vlan = old[identifier]
        owner = _bridge_owner(old, identifier)
        if owner is not None and identifier not in detached:
            operations.append(
                TransitionOperation(
                    TransitionOperationKind.DETACH,
                    identifier,
                    vlan.name,
                    f"detach from bridge {owner.name}",
                )
            )
        operations.append(
            TransitionOperation(
                TransitionOperationKind.DELETE,
                identifier,
                vlan.name,
                "delete managed VLAN before recreation/removal",
            )
        )

    if operations:
        operations.append(
            TransitionOperation(
                TransitionOperationKind.RELOAD,
                "networkd",
                "networkd",
                "reload target networkd artifacts before recreation",
            )
        )
    for identifier, device in sorted(new.items()):
        before = old.get(identifier)
        if not isinstance(device.settings, PhysicalSettings) and (
            before is None or before != device
        ):
            operations.append(
                TransitionOperation(
                    TransitionOperationKind.CREATE,
                    identifier,
                    device.name,
                    "create/recreate managed virtual device from target netdev",
                )
            )
    for identifier, bridge in sorted(new.items()):
        if not isinstance(bridge.settings, BridgeSettings):
            continue
        before = old.get(identifier)
        if before is None or identifier in affected_bridges:
            for member_id in bridge.settings.members:
                member = new[member_id]
                operations.append(
                    TransitionOperation(
                        TransitionOperationKind.ATTACH,
                        member_id,
                        member.name,
                        f"attach to bridge {bridge.name} through target networkd "
                        "configuration",
                    )
                )
    if operations:
        for identifier, device in sorted(new.items()):
            if not isinstance(device.settings, PhysicalSettings):
                operations.append(
                    TransitionOperation(
                        TransitionOperationKind.VERIFY,
                        identifier,
                        device.name,
                        "verify target virtual-device identity and topology",
                    )
                )
    return operations


def _bridge_owner(
    devices: dict[str, NetworkDevice], member_id: str
) -> NetworkDevice | None:
    return next(
        (
            device
            for device in devices.values()
            if isinstance(device.settings, BridgeSettings)
            and member_id in device.settings.members
        ),
        None,
    )


def _requires_bridge_recreation(
    previous: NetworkDevice, candidate: NetworkDevice
) -> bool:
    """Only immutable netdev properties justify rebuilding a bridge."""
    assert isinstance(previous.settings, BridgeSettings)
    assert isinstance(candidate.settings, BridgeSettings)
    return (
        previous.name != candidate.name
        or previous.settings.stp != candidate.settings.stp
    )


def _recovery_names(
    old: dict[str, NetworkDevice], new: dict[str, NetworkDevice]
) -> tuple[str, ...]:
    """Return confirmed affected virtual identities, bridge-first for cleanup."""
    affected = [
        device
        for identifier, device in old.items()
        if not isinstance(device.settings, PhysicalSettings)
        and (identifier not in new or new[identifier] != device)
    ] + [
        device
        for identifier, device in new.items()
        if not isinstance(device.settings, PhysicalSettings)
        and (identifier not in old or old[identifier] != device)
    ]
    return tuple(
        dict.fromkeys(
            device.name
            for device in sorted(
                affected,
                key=lambda item: (
                    not isinstance(item.settings, BridgeSettings),
                    item.name,
                ),
            )
        )
    )
