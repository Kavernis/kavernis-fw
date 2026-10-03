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
    description: str | None = None

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


class RoutesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = 1
    routes: list[RouteConfig]

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: int) -> int:
        if value != 1:
            raise PydanticCustomError("unsupported_version", "version must be 1")
        return value

    @field_validator("routes")
    @classmethod
    def validate_unique_ids(cls, routes: list[RouteConfig]) -> list[RouteConfig]:
        ids = [route.id for route in routes]
        if len(ids) != len(set(ids)):
            raise PydanticCustomError(
                "duplicate_route", "routes contains duplicate id values"
            )
        return routes
