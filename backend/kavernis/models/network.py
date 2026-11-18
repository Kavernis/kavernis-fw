"""Complete backend-independent resolved network intent."""

from dataclasses import dataclass

from kavernis.models.device import NetworkDevice
from kavernis.models.interface import NetworkInterface
from kavernis.models.routing import RoutingModel


@dataclass(frozen=True)
class NetworkModel:
    devices: tuple[NetworkDevice, ...]
    interfaces: tuple[NetworkInterface, ...]
    routing: RoutingModel = RoutingModel()
