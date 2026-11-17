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
        ),
        inspector=PhysicalRuntime(tmp_path / "networkd"),
        verification_timeout=0.02,
    )


def resolved_model(config):
    return resolve_topology(
        resolve_network(
            config.devices,
            config.interfaces,
            config.routing,
        )
    )


class PhysicalRuntime:
    """Unit-test simulator only; artifact contents are NOT Linux observations.

    Existing transaction fixtures have one eth1 physical device. Dedicated
    runtime tests exercise real JSON parsing and scripted convergence failures.
    """

    def __init__(self, directory):
        self.directory = directory

    def inspect(self, timeout):
        from kavernis.backends.network_runtime import (
            NetworkRuntimeSnapshot,
            RuntimeLink,
        )

        links = []
        for identifier, name in (("lan-nic", "eth1"), ("wan-nic", "enp1s0")):
            path = self.directory / f"10-kavernis-{identifier}.network"
            addresses = (
                frozenset(
                    line.removeprefix("Address=")
                    for line in path.read_text().splitlines()
                    if line.startswith("Address=")
                )
                if path.exists()
                else frozenset()
            )
            links.append(
                RuntimeLink(
                    name,
                    addresses=addresses,
                    configured_static_addresses=addresses,
                    network_file=str(path),
                    setup_state="configured",
                )
            )
        return NetworkRuntimeSnapshot(tuple(links))


def fake_activation(candidate, directory, **callbacks):
    """Explicit orchestration stub, not a native application test."""
    directory.mkdir(parents=True, exist_ok=True)
    for filename, content in candidate.files.items():
        (directory / filename).write_text(content)
    callbacks["verify"]()
    callbacks["record"]()
