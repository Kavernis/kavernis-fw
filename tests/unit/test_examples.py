"""Repository example configuration conventions."""

import re
from pathlib import Path

from kavernis.config.loader import (
    load_gateways,
    load_interfaces,
    load_routes,
)


def test_examples_use_yaml_extension_and_parse() -> None:
    examples = Path("configs/examples")

    assert not list(examples.glob("*.yml"))
    assert load_interfaces(examples / "interfaces.yaml")
    assert load_interfaces(examples / "interfaces-dhcp-options.yaml")
    assert load_interfaces(examples / "interfaces-vlan.yaml")
    assert load_interfaces(examples / "interfaces-bridge.yaml")
    assert load_interfaces(examples / "interfaces-routing.yaml")
    assert load_gateways(examples / "gateways.yaml")
    assert load_routes(examples / "routes.yaml")


def test_readme_has_no_stale_example_yml_references() -> None:
    readme = Path("README.md").read_text()
    assert not re.search(r"configs/examples/[^\s)`]+\.yml\b", readme)
