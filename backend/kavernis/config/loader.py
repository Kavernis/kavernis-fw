from pathlib import Path

import yaml
from pydantic import ValidationError

from kavernis.config.errors import ConfigurationError
from kavernis.config.interfaces import InterfacesConfig


def load_interfaces(path: str | Path) -> InterfacesConfig:
    """Load and validate an interfaces YAML document."""
    path = Path(path)

    try:
        with path.open("r", encoding="utf-8") as file:
            data = yaml.safe_load(file)
    except (OSError, yaml.YAMLError) as error:
        message = f"cannot load interfaces configuration {path}: {error}"
        raise ConfigurationError(message) from error

    try:
        return InterfacesConfig.model_validate(data)
    except ValidationError as error:
        raise ConfigurationError(
            f"invalid interfaces configuration {path}: {error}"
        ) from error
