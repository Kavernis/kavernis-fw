"""Child process only: create disposable kernel objects inside a new namespace."""

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2] / "backend"))

from kavernis.backends.network_runtime import (  # noqa: E402
    RuntimeRoute,
    parse_snapshot,
    topology_issues,
)
from kavernis.models.device import (  # noqa: E402
    BridgeSettings,
    NetworkDevice,
    PhysicalSettings,
    VLANSettings,
)
from kavernis.models.network import NetworkModel  # noqa: E402


def ip(*args):
    return subprocess.run(
        ["ip", *args], check=True, capture_output=True, text=True, timeout=3
    ).stdout


def main():
    # Refuse direct execution in the host namespace, even if invoked by mistake.
    if os.readlink("/proc/self/ns/net") == os.readlink("/proc/1/ns/net"):
        raise RuntimeError("must run in an isolated network namespace")
    ip("link", "add", "kv-a", "type", "veth", "peer", "name", "kv-b")
    ip("link", "add", "link", "kv-a", "name", "kv-vlan", "type", "vlan", "id", "30")
    ip("link", "add", "kv-br", "type", "bridge", "stp_state", "0")
    ip("link", "set", "kv-vlan", "master", "kv-br")
    for name in ("kv-a", "kv-b", "kv-vlan", "kv-br"):
        ip("link", "set", name, "up")
    ip("address", "add", "192.0.2.1/24", "dev", "kv-br")
    ip("address", "add", "2001:db8::1/64", "dev", "kv-br", "nodad")
    ip(
        "route",
        "add",
        "198.51.100.0/24",
        "via",
        "192.0.2.2",
        "dev",
        "kv-br",
        "proto",
        "static",
    )
    observed = parse_snapshot(
        json.loads(ip("-j", "-d", "link", "show")),
        json.loads(ip("-j", "address", "show")),
        json.loads(ip("-j", "-4", "route", "show", "table", "main")),
        json.loads(ip("-j", "-6", "route", "show", "table", "main")),
        {"Interfaces": []},  # networkd is deliberately not simulated here.
    )
    model = NetworkModel(
        (
            NetworkDevice("parent", "kv-a", PhysicalSettings()),
            NetworkDevice("vlan", "kv-vlan", VLANSettings("parent", 30)),
            NetworkDevice("bridge", "kv-br", BridgeSettings(("vlan",), False)),
        ),
        (),
    )
    assert not topology_issues(model, observed)
    bridge = next(link for link in observed.links if link.name == "kv-br")
    assert {"192.0.2.1/24", "2001:db8::1/64"} <= bridge.addresses
    assert RuntimeRoute("198.51.100.0/24", "192.0.2.2", "kv-br") in observed.routes
    # Namespace teardown destroys all these objects; no host cleanup commands.


if __name__ == "__main__":
    main()
