from enum import StrEnum
from ipaddress import IPv4Interface, IPv6Interface
from typing import Annotated
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError


class IPv4Mode(StrEnum):
    STATIC = "static"
    DHCP = "dhcp"
    DISABLED = "disabled"


class IPv6Mode(StrEnum):
    STATIC = "static"
    DHCP6 = "dhcp6"
    SLAAC = "slaac"
    DISABLED = "disabled"


class IPv4Config(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: IPv4Mode
    address: IPv4Interface | None = None

    @model_validator(mode="after")
    def validate_address_for_mode(self) -> "IPv4Config":
        if self.mode is IPv4Mode.STATIC and self.address is None:
            raise PydanticCustomError(
                "static_address_required",
                "ipv4.address is required when ipv4.mode is 'static'",
            )
        if self.mode is not IPv4Mode.STATIC and self.address is not None:
            raise PydanticCustomError(
                "address_not_allowed",
                "ipv4.address is only allowed when ipv4.mode is 'static'",
            )
        return self


class IPv6Config(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: IPv6Mode
    address: IPv6Interface | None = None

    @model_validator(mode="after")
    def validate_address_for_mode(self) -> "IPv6Config":
        if self.mode is IPv6Mode.STATIC and self.address is None:
            raise PydanticCustomError(
                "static_address_required",
                "ipv6.address is required when ipv6.mode is 'static'",
            )
        if self.mode is not IPv6Mode.STATIC and self.address is not None:
            raise PydanticCustomError(
                "address_not_allowed",
                "ipv6.address is only allowed when ipv6.mode is 'static'",
            )
        return self


class VLANConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    parent: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    tag: int = Field(strict=True, ge=1, le=4094)


class BridgeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    members: tuple[Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]*$")], ...] = Field(
        min_length=1
    )
    stp: bool = Field(default=True, strict=True)

    @field_validator("members")
    @classmethod
    def validate_unique_members(cls, members: tuple[str, ...]) -> tuple[str, ...]:
        if len(members) != len(set(members)):
            raise PydanticCustomError(
                "duplicate_bridge_member", "bridge.members contains duplicate ids"
            )
        return members


class InterfaceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    uid: UUID
    id: str
    name: str
    device: str
    ipv4: IPv4Config
    ipv6: IPv6Config
    vlan: VLANConfig | None = None
    bridge: BridgeConfig | None = None

    @model_validator(mode="after")
    def validate_virtual_device(self) -> "InterfaceConfig":
        if self.vlan is not None and self.bridge is not None:
            raise PydanticCustomError(
                "conflicting_interface_types", "vlan and bridge are mutually exclusive"
            )
        if (self.vlan is not None or self.bridge is not None) and (
            len(self.device) > 15 or self.device in {".", ".."} or ":" in self.device
        ):
            raise PydanticCustomError(
                "invalid_virtual_device",
                "virtual device must be at most 15 characters, contain no colon, "
                "and cannot be '.' or '..'",
            )
        return self

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        import re

        if not re.fullmatch(r"[a-z][a-z0-9-]*", value):
            raise PydanticCustomError(
                "invalid_interface_id",
                "id must contain lowercase letters, digits, and hyphens; "
                "it must start with a letter",
            )
        return value

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not value.strip():
            raise PydanticCustomError("empty_name", "name must not be empty")
        return value

    @field_validator("device")
    @classmethod
    def validate_device(cls, value: str) -> str:
        import re

        if not re.fullmatch(r"[A-Za-z0-9_.:-]+", value):
            raise PydanticCustomError(
                "invalid_device_name",
                "device contains unsupported characters",
            )
        return value


class InterfacesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int
    interfaces: list[InterfaceConfig]

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: int) -> int:
        if value != 1:
            raise PydanticCustomError("unsupported_version", "version must be 1")
        return value

    @field_validator("interfaces")
    @classmethod
    def validate_unique_interfaces(
        cls, interfaces: list[InterfaceConfig]
    ) -> list[InterfaceConfig]:
        if not interfaces:
            raise PydanticCustomError(
                "interfaces_required", "interfaces must contain at least one interface"
            )
        for attribute in ("uid", "id", "device"):
            values = [getattr(interface, attribute) for interface in interfaces]
            if len(values) != len(set(values)):
                raise PydanticCustomError(
                    "duplicate_interface",
                    f"interfaces contains duplicate {attribute} values",
                )
        by_id = {interface.id: interface for interface in interfaces}
        vlan_pairs: set[tuple[str, int]] = set()
        for interface in interfaces:
            if interface.vlan is None:
                continue
            vlan = interface.vlan
            parent = by_id.get(vlan.parent)
            if parent is None:
                raise PydanticCustomError(
                    "unknown_vlan_parent",
                    f"interface {interface.id}: "
                    f"vlan.parent '{vlan.parent}' does not exist",
                )
            if (
                parent.id == interface.id
                or parent.vlan is not None
                or parent.bridge is not None
            ):
                raise PydanticCustomError(
                    "invalid_vlan_parent",
                    f"interface {interface.id}: vlan.parent must reference "
                    "a physical interface",
                )
            pair = (vlan.parent, vlan.tag)
            if pair in vlan_pairs:
                raise PydanticCustomError(
                    "duplicate_vlan",
                    f"interface {interface.id}: duplicate vlan.tag {vlan.tag} "
                    f"on vlan.parent '{vlan.parent}'",
                )
            vlan_pairs.add(pair)
        owners: dict[str, str] = {}
        vlan_parents = {
            interface.vlan.parent
            for interface in interfaces
            if interface.vlan is not None
        }
        for interface in interfaces:
            if interface.bridge is None:
                continue
            for member_id in interface.bridge.members:
                context = f"interface {interface.id}: bridge.members '{member_id}'"
                member = by_id.get(member_id)
                if member is None:
                    raise PydanticCustomError(
                        "unknown_bridge_member", f"{context} does not exist"
                    )
                if member.bridge is not None:
                    raise PydanticCustomError(
                        "invalid_bridge_member", f"{context} must not be a bridge"
                    )
                if member_id in owners:
                    raise PydanticCustomError(
                        "shared_bridge_member",
                        f"{context} already belongs to bridge '{owners[member_id]}'",
                    )
                if (
                    member.ipv4.mode is not IPv4Mode.DISABLED
                    or member.ipv6.mode is not IPv6Mode.DISABLED
                ):
                    raise PydanticCustomError(
                        "addressed_bridge_member",
                        f"{context} must have ipv4.mode and ipv6.mode disabled; "
                        "configure IP addressing on the bridge",
                    )
                if member_id in vlan_parents:
                    raise PydanticCustomError(
                        "bridged_vlan_parent",
                        f"{context} is a VLAN parent; "
                        "bridge its VLAN interfaces instead",
                    )
                owners[member_id] = interface.id
        return interfaces
