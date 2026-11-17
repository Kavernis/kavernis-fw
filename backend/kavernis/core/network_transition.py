"""Conservative reload-only transition planning using immutable device identity."""

from dataclasses import dataclass
from enum import StrEnum

from kavernis.backends.network_runtime import NetworkRuntimeSnapshot, topology_issues
from kavernis.models.device import PhysicalSettings
from kavernis.models.network import NetworkModel


class UnsupportedNetworkTransition(ValueError):
    """The reload backend cannot safely perform this topology transition."""


class TransitionKind(StrEnum):
    NEW = "new"
    UNCHANGED = "unchanged"
    RELOAD = "reload"
    REPLACE = "replacement required"
    REMOVE = "removal required"


@dataclass(frozen=True)
class DeviceTransition:
    device_id: str
    kind: TransitionKind
    detail: str


def classify_transitions(
    previous: NetworkModel | None,
    desired: NetworkModel,
) -> tuple[DeviceTransition, ...]:
    old = {d.id: d for d in previous.devices} if previous else {}
    new = {d.id: d for d in desired.devices}
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


def validate_transition(
    previous: NetworkModel | None,
    desired: NetworkModel,
    observed: NetworkRuntimeSnapshot,
) -> tuple[DeviceTransition, ...]:
    transitions = classify_transitions(previous, desired)
    for transition in transitions:
        if transition.kind in (TransitionKind.REPLACE, TransitionKind.REMOVE):
            raise UnsupportedNetworkTransition(
                f"Unsupported live network transition for '{transition.device_id}': "
                f"{transition.detail}. This requires device recreation, removal or "
                "detachment that the current networkd reload backend cannot safely "
                "perform. Reconcile in a controlled maintenance environment."
            )
    if previous:
        issues = topology_issues(previous, observed)
        if issues:
            raise UnsupportedNetworkTransition(
                "Applied topology does not match Linux; cannot safely assume a "
                "reload transition: " + "; ".join(issues)
            )
    old_ids = {d.id for d in previous.devices} if previous else set()
    links = {link.name: link for link in observed.links}
    for device in desired.devices:
        if device.id in old_ids:
            continue
        link = links.get(device.name)
        if isinstance(device.settings, PhysicalSettings):
            if link is None:
                raise UnsupportedNetworkTransition(
                    f"physical device {device.name} is missing before apply"
                )
            if link.kind in ("bridge", "vlan") or link.master is not None:
                raise UnsupportedNetworkTransition(
                    f"physical device {device.name} has conflicting type/bridge "
                    "membership before adoption"
                )
        elif link is not None:
            raise UnsupportedNetworkTransition(
                f"new virtual device {device.name} already exists without an "
                "applied identity; ownership cannot be established"
            )
    return transitions
