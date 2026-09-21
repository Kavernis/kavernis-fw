from uuid import UUID

import pytest
from kavernis.backends.networkd import (
    NetworkdConfiguration,
    NetworkdValidationError,
    generate,
    validate,
)
from kavernis.config.interfaces import InterfacesConfig
from kavernis.config.resolver import resolve_interfaces
from kavernis.core.interfaces import plan_interfaces
from kavernis.models.interface import (
    IPv4AddressMode,
    IPv4Settings,
    IPv6AddressMode,
    IPv6Settings,
    NetworkInterface,
)


def configuration() -> InterfacesConfig:
    return InterfacesConfig.model_validate(
        {
            "version": 1,
            "interfaces": [
                {
                    "uid": "8f3a7c22-1c7d-4d6b-a901-000000000001",
                    "id": "lan",
                    "name": "Users LAN",
                    "device": "eth1",
                    "ipv4": {"mode": "static", "address": "192.168.10.254/24"},
                    "ipv6": {"mode": "slaac"},
                },
                {
                    "uid": "8f3a7c22-1c7d-4d6b-a901-000000000002",
                    "id": "wan",
                    "name": "WAN",
                    "device": "eth0",
                    "ipv4": {"mode": "dhcp"},
                    "ipv6": {"mode": "dhcp6"},
                },
            ],
        }
    )


def test_resolve_interfaces_preserves_typed_network_intent() -> None:
    interfaces = resolve_interfaces(configuration())

    assert interfaces[0].uid == UUID("8f3a7c22-1c7d-4d6b-a901-000000000001")
    assert interfaces[0].ipv4.mode is IPv4AddressMode.STATIC
    assert interfaces[0].ipv6.mode is IPv6AddressMode.SLAAC
    assert interfaces[1].ipv4.mode is IPv4AddressMode.DHCP
    assert interfaces[1].ipv6.mode is IPv6AddressMode.DHCP6


def test_plan_interfaces_generates_deterministic_networkd_files() -> None:
    candidate = plan_interfaces(configuration())

    assert list(candidate.files) == [
        "10-kavernis-lan.network",
        "10-kavernis-wan.network",
    ]
    assert candidate.files["10-kavernis-lan.network"] == (
        "[Match]\nName=eth1\n\n[Network]\nAddress=192.168.10.254/24\nIPv6AcceptRA=yes\n"
    )
    assert candidate.files["10-kavernis-wan.network"] == (
        "[Match]\nName=eth0\n\n[Network]\nDHCP=yes\n"
    )


def test_plan_interfaces_disables_ipv6_router_advertisements() -> None:
    config = InterfacesConfig.model_validate(
        {
            "version": 1,
            "interfaces": [
                {
                    "uid": "8f3a7c22-1c7d-4d6b-a901-000000000003",
                    "id": "isolated",
                    "name": "Isolated",
                    "device": "eth2",
                    "ipv4": {"mode": "disabled"},
                    "ipv6": {"mode": "disabled"},
                }
            ],
        }
    )

    candidate = plan_interfaces(config)

    assert candidate.files["10-kavernis-isolated.network"] == (
        "[Match]\nName=eth2\n\n[Network]\nIPv6AcceptRA=no\n"
    )


def test_reject_unsafe_generated_candidate() -> None:
    candidate = NetworkdConfiguration(
        files={"../outside.network": "[Match]\n[Network]\n"}
    )

    with pytest.raises(NetworkdValidationError, match="unsafe"):
        validate(candidate)


def test_reject_incomplete_domain_model_before_generation() -> None:
    interface = NetworkInterface(
        uid=UUID("8f3a7c22-1c7d-4d6b-a901-000000000004"),
        id="invalid",
        name="Invalid",
        device="eth3",
        ipv4=IPv4Settings(mode=IPv4AddressMode.STATIC),
        ipv6=IPv6Settings(mode=IPv6AddressMode.DISABLED),
    )

    with pytest.raises(NetworkdValidationError, match="no address"):
        generate([interface])
