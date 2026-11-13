"""Network resource schemas; cross-resource rules belong to core topology."""

from typing import Annotated, Literal, TypeVar

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from pydantic_core import PydanticCustomError

from kavernis.config.addressing import (
    IPv4Config,
    IPv4Mode,
    IPv6Config,
    IPv6Mode,
)
from kavernis.config.errors import ConfigurationError
from kavernis.config.gateways import GatewayConfig
from kavernis.config.routes import RouteConfig

Identifier = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]*$")]
DeviceName = Annotated[
    str,
    Field(
        pattern=r"^[A-Za-z0-9_.:-]+$", json_schema_extra={"not": {"enum": [".", ".."]}}
    ),
]
VirtualName = Annotated[
    str,
    Field(
        pattern=r"^[A-Za-z0-9_.-]+$",
        min_length=1,
        max_length=15,
        json_schema_extra={"not": {"enum": [".", ".."]}},
    ),
]


class StrictConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class VLANConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    parent: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    tag: int = Field(strict=True, ge=1, le=4094)


class BridgeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    members: tuple[Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]*$")], ...] = Field(
        min_length=1, json_schema_extra={"uniqueItems": True}
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


class PhysicalDeviceConfig(StrictConfig):
    id: Identifier
    type: Literal["physical"]
    name: DeviceName

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if value in {".", ".."}:
            raise ValueError("invalid device name")
        return value


class VirtualDeviceConfig(StrictConfig):
    id: Identifier
    name: VirtualName

    @field_validator("name")
    @classmethod
    def validate_virtual_name(cls, value: str) -> str:
        if value in {".", ".."}:
            raise ValueError("invalid virtual device name")
        return value


class VLANDeviceConfig(VirtualDeviceConfig):
    type: Literal["vlan"]
    vlan: VLANConfig


class BridgeDeviceConfig(VirtualDeviceConfig):
    type: Literal["bridge"]
    bridge: BridgeConfig


DeviceConfig = Annotated[
    PhysicalDeviceConfig | VLANDeviceConfig | BridgeDeviceConfig,
    Field(discriminator="type"),
]


class NetworkDocument(StrictConfig):
    version: int = Field(strict=True, json_schema_extra={"const": 2})

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: int) -> int:
        if value != 2:
            raise ValueError("version must be 2")
        return value


class DevicesConfig(NetworkDocument):
    devices: tuple[DeviceConfig, ...] = Field(min_length=1)

    @field_validator("devices")
    @classmethod
    def unique_devices(
        cls, values: tuple[DeviceConfig, ...]
    ) -> tuple[DeviceConfig, ...]:
        for label, items in (
            ("id", [d.id for d in values]),
            ("name", [d.name for d in values]),
        ):
            if len(items) != len(set(items)):
                raise ValueError(f"duplicate device {label}")
        return values


class LogicalInterfaceConfig(StrictConfig):
    id: Identifier
    name: str = Field(pattern=r"\S")
    device: Identifier
    ipv4: IPv4Config = Field(default_factory=lambda: IPv4Config(mode=IPv4Mode.DISABLED))
    ipv6: IPv6Config = Field(default_factory=lambda: IPv6Config(mode=IPv6Mode.DISABLED))


class LogicalInterfacesConfig(NetworkDocument):
    interfaces: tuple[LogicalInterfaceConfig, ...]

    @field_validator("interfaces")
    @classmethod
    def unique_interfaces(
        cls, values: tuple[LogicalInterfaceConfig, ...]
    ) -> tuple[LogicalInterfaceConfig, ...]:
        for label, items in (
            ("id", [i.id for i in values]),
            ("device", [i.device for i in values]),
        ):
            if len(items) != len(set(items)):
                raise ValueError(f"duplicate interface {label}")
        return values


class RoutingConfig(NetworkDocument):
    gateways: tuple[GatewayConfig, ...] = ()
    routes: tuple[RouteConfig, ...] = ()

    @field_validator("gateways", "routes")
    @classmethod
    def unique_ids(
        cls, values: tuple[GatewayConfig, ...] | tuple[RouteConfig, ...]
    ) -> tuple[GatewayConfig, ...] | tuple[RouteConfig, ...]:
        ids = [item.id for item in values]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate routing id")
        return values


Document = TypeVar("Document", bound=BaseModel)


class _UniqueSafeLoader(yaml.SafeLoader):
    """Safe YAML loader rejecting duplicate keys, including merged mappings."""


def _construct_unique_mapping(
    loader: _UniqueSafeLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[object, object]:
    explicit_keys: set[object] = set()
    for key_node, _ in node.value:
        if key_node.tag == "tag:yaml.org,2002:merge":
            continue
        key = loader.construct_object(key_node, deep=deep)
        if key in explicit_keys:
            mark = key_node.start_mark
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"duplicate explicitly declared key {key!r}",
                mark,
            )
        explicit_keys.add(key)

    # PyYAML expands aliases and merge keys here. Assignment order preserves
    # YAML's merge precedence while explicit keys override merged defaults.
    loader.flatten_mapping(node)
    result: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def parse_document(contents: bytes, model: type[Document], source: str) -> Document:
    try:
        parsed = yaml.load(contents.decode("utf-8"), Loader=_UniqueSafeLoader)
        return model.model_validate(parsed)
    except (UnicodeError, yaml.YAMLError, ValidationError) as error:
        raise ConfigurationError(f"invalid {source}: {error}") from error
