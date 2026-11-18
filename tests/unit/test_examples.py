"""Every documented example is a complete, independently plannable network."""

from pathlib import Path

import pytest
from kavernis.core.network import NetworkStatePaths, load_network, plan_network

EXAMPLES = sorted(Path("configs/examples").glob("*/network"))


@pytest.mark.parametrize("directory", EXAMPLES, ids=lambda path: path.parent.name)
def test_complete_example_loads_and_plans_without_writes(directory):
    before = {path.name: path.read_bytes() for path in directory.iterdir()}
    paths = NetworkStatePaths(
        devices_path=directory / "devices.yaml",
        interfaces_path=directory / "interfaces.yaml",
        routing_path=directory / "routing.yaml",
    )
    candidate = plan_network(load_network(paths))
    assert candidate.files
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == before


def test_examples_cover_all_documented_variants():
    assert {path.parent.name for path in EXAMPLES} == {
        "vlans",
        "bridge",
        "routing",
        "dhcp",
        "dhcp-options",
        "dual-stack",
    }
    assert not list(Path("configs/examples").rglob("*.yml"))
    assert not (Path("configs/examples/vlans/network/routing.yaml")).exists()
