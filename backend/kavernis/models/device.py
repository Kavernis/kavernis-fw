"""Immutable L2 identity and topology, independent of native backends."""

from dataclasses import dataclass
from enum import Enum


class DeviceKind(Enum):
    PHYSICAL = "physical"
    VLAN = "vlan"
    BRIDGE = "bridge"


@dataclass(frozen=True)
class PhysicalSettings:
    """Adopt an existing kernel device without creating it."""


@dataclass(frozen=True)
class VLANSettings:
    parent: str
    tag: int


@dataclass(frozen=True)
class BridgeSettings:
    members: tuple[str, ...]
    stp: bool = True


DeviceSettings = PhysicalSettings | VLANSettings | BridgeSettings


@dataclass(frozen=True)
class NetworkDevice:
    id: str
    name: str
    settings: DeviceSettings

    @property
    def kind(self) -> DeviceKind:
        if isinstance(self.settings, VLANSettings):
            return DeviceKind.VLAN
        if isinstance(self.settings, BridgeSettings):
            return DeviceKind.BRIDGE
        return DeviceKind.PHYSICAL
