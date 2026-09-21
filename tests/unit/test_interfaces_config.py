from copy import deepcopy
from pathlib import Path

import pytest
from kavernis.config.errors import ConfigurationError
from kavernis.config.interfaces import InterfacesConfig
from kavernis.config.loader import load_interfaces
from pydantic import ValidationError


def valid_configuration() -> dict[str, object]:
    return {
        "version": 1,
        "interfaces": [
            {
                "uid": "8f3a7c22-1c7d-4d6b-a901-000000000001",
                "id": "lan",
                "name": "LAN",
                "device": "eth0",
                "ipv4": {"mode": "static", "address": "192.168.10.1/24"},
                "ipv6": {"mode": "slaac"},
            }
        ],
    }


def test_load_example_configuration() -> None:
    config = load_interfaces(Path("configs/examples/interfaces.yml"))

    assert config.version == 1
    assert [interface.id for interface in config.interfaces] == ["lan", "wan"]
    assert str(config.interfaces[0].ipv4.address) == "192.168.0.1/24"


@pytest.mark.parametrize(
    ("mutation", "field"),
    [
        (lambda data: data["interfaces"][0]["ipv4"].pop("address"), "ipv4"),
        (
            lambda data: data["interfaces"][0]["ipv4"].update(
                {"mode": "dhcp", "address": "192.168.10.1/24"}
            ),
            "ipv4",
        ),
        (
            lambda data: data["interfaces"][0]["ipv4"].update(
                {"address": "2001:db8::1/64"}
            ),
            "ipv4",
        ),
        (lambda data: data["interfaces"][0].update({"unexpected": True}), "unexpected"),
    ],
)
def test_reject_invalid_ip_configuration(mutation: object, field: str) -> None:
    data = valid_configuration()
    mutation(data)  # type: ignore[operator]

    with pytest.raises(ValidationError, match=field):
        InterfacesConfig.model_validate(data)


def test_reject_duplicate_interface_identity() -> None:
    data = valid_configuration()
    duplicate = deepcopy(data["interfaces"][0])
    duplicate["id"] = "guest"
    data["interfaces"].append(duplicate)

    with pytest.raises(ValidationError, match="duplicate uid"):
        InterfacesConfig.model_validate(data)


def test_reject_empty_interfaces_configuration() -> None:
    with pytest.raises(ValidationError, match="at least one interface"):
        InterfacesConfig.model_validate({"version": 1, "interfaces": []})


def test_loader_exposes_context_for_invalid_yaml(tmp_path: Path) -> None:
    path = tmp_path / "interfaces.yml"
    path.write_text("interfaces: [", encoding="utf-8")

    with pytest.raises(
        ConfigurationError, match="cannot load interfaces configuration"
    ):
        load_interfaces(path)
