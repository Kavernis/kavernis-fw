"""Target-layout fixtures shared by network behavior tests."""

from pathlib import Path

from kavernis.config.layout import NetworkDesiredState
from kavernis.config.network import (
    DevicesConfig,
    LogicalInterfacesConfig,
    RoutingConfig,
)
from kavernis.config.resolver import resolve_network
from kavernis.core.network import NetworkStatePaths, NetworkStateService
from kavernis.core.topology import resolve_topology


def desired(devices, interfaces=(), gateways=(), routes=()) -> NetworkDesiredState:
    return NetworkDesiredState(
        DevicesConfig.model_validate({"version": 2, "devices": devices}),
        LogicalInterfacesConfig.model_validate(
            {"version": 2, "interfaces": interfaces}
        ),
        RoutingConfig.model_validate(
            {"version": 2, "gateways": gateways, "routes": routes}
        ),
        {},
    )


def physical(identifier="nic", name="eth0"):
    return {"id": identifier, "type": "physical", "name": name}


def interface(identifier="lan", device="nic", **families):
    return {"id": identifier, "name": identifier.upper(), "device": device, **families}


def write_desired(directory: Path, address="192.168.10.1/24") -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "devices.yaml").write_text(
        "version: 2\ndevices:\n  - {id: lan-nic, type: physical, name: eth1}\n"
    )
    (directory / "interfaces.yaml").write_text(
        "version: 2\ninterfaces:\n  - id: lan\n    name: LAN\n    device: lan-nic\n"
        f"    ipv4: {{mode: static, address: {address}}}\n"
    )
    (directory / "routing.yaml").write_text("version: 2\ngateways: []\nroutes: []\n")


def service(tmp_path: Path) -> NetworkStateService:
    directory = tmp_path / "etc/network"
    return NetworkStateService(
        NetworkStatePaths(
            devices_path=directory / "devices.yaml",
            interfaces_path=directory / "interfaces.yaml",
            routing_path=directory / "routing.yaml",
            history_path=tmp_path / "history",
            database_path=tmp_path / "state.db",
            networkd_path=tmp_path / "networkd",
            lock_path=tmp_path / "run/network.lock",
        )
    )


def resolved_model(config):
    return resolve_topology(
        resolve_network(
            config.devices,
            config.interfaces,
            config.routing,
        )
    )
