from pathlib import Path
from typing import TypeVar

import yaml
from pydantic import ValidationError

from kavernis.config.errors import ConfigurationError
from kavernis.config.gateways import GatewaysConfig
from kavernis.config.interfaces import InterfacesConfig
from kavernis.config.routes import RoutesConfig

ConfigDocument = TypeVar("ConfigDocument", GatewaysConfig, RoutesConfig)


def load_interfaces(path: str | Path) -> InterfacesConfig:
    """Load and validate an interfaces YAML document."""
    path = Path(path)

    try:
        contents = path.read_text(encoding="utf-8")
    except (OSError, yaml.YAMLError) as error:
        message = f"cannot load interfaces configuration {path}: {error}"
        raise ConfigurationError(message) from error

    return load_interfaces_contents(contents, f"interfaces configuration {path}")


def load_interfaces_contents(
    contents: str, source: str = "interfaces configuration"
) -> InterfacesConfig:
    """Validate interfaces YAML already obtained from a controlled source."""
    try:
        data = yaml.safe_load(contents)
    except yaml.YAMLError as error:
        raise ConfigurationError(f"cannot load {source}: {error}") from error

    try:
        return InterfacesConfig.model_validate(data)
    except ValidationError as error:
        raise ConfigurationError(f"invalid {source}: {error}") from error


def load_gateways(path: str | Path) -> GatewaysConfig:
    return _load(path, "gateways", GatewaysConfig)


def load_routes(path: str | Path) -> RoutesConfig:
    return _load(path, "routes", RoutesConfig)


def load_gateways_contents(
    contents: str, source: str = "gateways configuration"
) -> GatewaysConfig:
    return _load_contents(contents, source, GatewaysConfig)


def load_routes_contents(
    contents: str, source: str = "routes configuration"
) -> RoutesConfig:
    return _load_contents(contents, source, RoutesConfig)


def _load(path: str | Path, domain: str, model: type[ConfigDocument]) -> ConfigDocument:
    source_path = Path(path)
    try:
        contents = source_path.read_text(encoding="utf-8")
    except OSError as error:
        raise ConfigurationError(
            f"cannot load {domain} configuration {source_path}: {error}"
        ) from error
    return _load_contents(contents, f"{domain} configuration {source_path}", model)


def _load_contents(
    contents: str, source: str, model: type[ConfigDocument]
) -> ConfigDocument:
    try:
        data = yaml.safe_load(contents)
    except yaml.YAMLError as error:
        raise ConfigurationError(f"cannot load {source}: {error}") from error
    try:
        return model.model_validate(data)
    except ValidationError as error:
        raise ConfigurationError(f"invalid {source}: {error}") from error
