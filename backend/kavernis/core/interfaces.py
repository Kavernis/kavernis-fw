"""Plan and apply interface desired state."""

from kavernis.backends.networkd import NetworkdConfiguration, apply, generate
from kavernis.config.interfaces import InterfacesConfig
from kavernis.config.resolver import resolve_interfaces


def plan_interfaces(config: InterfacesConfig) -> NetworkdConfiguration:
    """Resolve validated desired state and generate a validated candidate."""
    return generate(resolve_interfaces(config))


def apply_interfaces(config: InterfacesConfig) -> NetworkdConfiguration:
    """Plan, validate, and apply the desired interface state."""
    candidate = plan_interfaces(config)
    apply(candidate)
    return candidate
