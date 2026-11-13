"""Strict desired-state configuration for static routes."""

import re
from ipaddress import IPv4Network, IPv6Network

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_core import PydanticCustomError


class RouteConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    network: IPv4Network | IPv6Network
    gateway: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    description: str | None = Field(default=None, pattern=r"\S")

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if re.fullmatch(r"[a-z][a-z0-9-]*", value) is None:
            raise PydanticCustomError(
                "invalid_route_id",
                "id must contain lowercase letters, digits, and hyphens; "
                "it must start with a letter",
            )
        return value

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise PydanticCustomError(
                "empty_description", "description must not be empty"
            )
        return value
