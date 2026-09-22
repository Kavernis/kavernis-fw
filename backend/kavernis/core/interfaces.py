"""Planning interfaces desired state without changing the host system."""

from kavernis.backends.networkd import NetworkdConfiguration, generate
from kavernis.config.interfaces import InterfacesConfig
from kavernis.config.resolver import resolve_interfaces


def plan_interfaces(config: InterfacesConfig) -> NetworkdConfiguration:
    """Resolve validated desired state and generate a validated candidate."""
    return generate(resolve_interfaces(config))
