from enum import StrEnum
from ipaddress import IPv4Interface, IPv6Interface
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
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


class InterfaceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    uid: UUID
    id: str
    name: str
    device: str
    ipv4: IPv4Config
    ipv6: IPv6Config

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
        return interfaces
