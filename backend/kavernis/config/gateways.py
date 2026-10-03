"""Strict desired-state configuration for logical static gateways."""

import re
from ipaddress import IPv4Address, IPv6Address

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_core import PydanticCustomError


class GatewayConfig(BaseModel):
    """A next-hop or direct interface exit, before cross-resource resolution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    name: str
    interface: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    address: IPv4Address | IPv6Address | None = None
    onlink: bool = Field(default=False, strict=True)
    description: str | None = None

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if re.fullmatch(r"[a-z][a-z0-9-]*", value) is None:
            raise PydanticCustomError(
                "invalid_gateway_id",
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

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise PydanticCustomError(
                "empty_description", "description must not be empty"
            )
        return value

    def model_post_init(self, __context: object) -> None:
        if self.address is None and self.onlink:
            raise PydanticCustomError(
                "onlink_without_address", "onlink: true requires a gateway address"
            )


class GatewaysConfig(BaseModel):
    """Gateway desired-state document."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = 1
    gateways: list[GatewayConfig]

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: int) -> int:
        if value != 1:
            raise PydanticCustomError("unsupported_version", "version must be 1")
        return value

    @field_validator("gateways")
    @classmethod
    def validate_unique_ids(cls, gateways: list[GatewayConfig]) -> list[GatewayConfig]:
        ids = [gateway.id for gateway in gateways]
        if len(ids) != len(set(ids)):
            raise PydanticCustomError(
                "duplicate_gateway", "gateways contains duplicate id values"
            )
        return gateways
