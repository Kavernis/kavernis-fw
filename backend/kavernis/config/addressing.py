from enum import StrEnum
from ipaddress import IPv4Interface, IPv6Interface

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
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


class DHCPv4Config(BaseModel):
    """Kavernis DHCPv4 policy, resolved independently of networkd defaults."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    use_hostname: bool = Field(default=False, strict=True)
    send_hostname: bool = Field(default=True, strict=True)
    use_dns: bool = Field(default=True, strict=True)
    use_routes: bool = Field(default=True, strict=True)
    use_ntp: bool = Field(default=True, strict=True)
    route_metric: int | None = Field(default=None, strict=True, ge=0, le=4294967295)


class IPv4Config(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={
            "allOf": [
                {
                    "if": {
                        "properties": {"mode": {"const": "static"}},
                        "required": ["mode"],
                    },
                    "then": {
                        "required": ["address"],
                        "properties": {"address": {"type": "string"}},
                    },
                    "else": {"properties": {"address": {"type": "null"}}},
                },
                {
                    "if": {
                        "properties": {"mode": {"const": "dhcp"}},
                        "required": ["mode"],
                    },
                    "else": {"not": {"required": ["dhcp"]}},
                },
            ]
        },
    )

    mode: IPv4Mode
    address: IPv4Interface | None = None
    dhcp: DHCPv4Config = Field(default_factory=DHCPv4Config)

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
        if self.mode is not IPv4Mode.DHCP and "dhcp" in self.model_fields_set:
            raise PydanticCustomError(
                "dhcp_not_allowed",
                "ipv4.dhcp is only allowed when ipv4.mode is 'dhcp'",
            )
        return self


class IPv6Config(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={
            "allOf": [
                {
                    "if": {
                        "properties": {"mode": {"const": "static"}},
                        "required": ["mode"],
                    },
                    "then": {
                        "required": ["address"],
                        "properties": {"address": {"type": "string"}},
                    },
                    "else": {"properties": {"address": {"type": "null"}}},
                }
            ]
        },
    )

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
