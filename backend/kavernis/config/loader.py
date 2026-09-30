from pathlib import Path

import yaml
from pydantic import ValidationError

from kavernis.config.errors import ConfigurationError
from kavernis.config.interfaces import InterfacesConfig


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
