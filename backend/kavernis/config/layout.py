"""One network resource layout for loading, editing and desired history."""

from dataclasses import dataclass

from kavernis.config.errors import ConfigurationError
from kavernis.config.network import (
    DevicesConfig,
    LogicalInterfacesConfig,
    RoutingConfig,
    parse_document,
)

DESIRED_FILENAMES = (
    "network/devices.yaml",
    "network/interfaces.yaml",
    "network/routing.yaml",
)
MANDATORY_DESIRED_FILENAMES = DESIRED_FILENAMES[:2]


@dataclass(frozen=True)
class NetworkDesiredState:
    devices: DevicesConfig
    interfaces: LogicalInterfacesConfig
    routing: RoutingConfig
    files: dict[str, bytes]


def desired_from_files(files: dict[str, bytes], source: str) -> NetworkDesiredState:
    for filename in MANDATORY_DESIRED_FILENAMES:
        if filename not in files:
            raise ConfigurationError(f"missing mandatory {filename} in {source}")
    if set(files) - set(DESIRED_FILENAMES):
        raise ConfigurationError(f"unexpected network resources in {source}")
    return NetworkDesiredState(
        parse_document(files[DESIRED_FILENAMES[0]], DevicesConfig, source),
        parse_document(files[DESIRED_FILENAMES[1]], LogicalInterfacesConfig, source),
        parse_document(files[DESIRED_FILENAMES[2]], RoutingConfig, source)
        if DESIRED_FILENAMES[2] in files
        else RoutingConfig(version=2),
        dict(files),
    )
