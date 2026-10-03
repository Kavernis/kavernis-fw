"""Interface-only planning used by focused backend tests."""

from kavernis.backends.networkd import NetworkdConfiguration, generate
from kavernis.config.interfaces import InterfacesConfig
from kavernis.config.resolver import resolve_interfaces


def plan_interfaces(config: InterfacesConfig) -> NetworkdConfiguration:
    """Resolve interface desired state and generate a networkd candidate."""
    return generate(resolve_interfaces(config))
