"""Typed intent, graph resolution and device identity invariants."""

from dataclasses import FrozenInstanceError, replace

import pytest
from kavernis.core.network import plan_network
from kavernis.core.topology import resolve_topology
from kavernis.models.device import DeviceKind
from network_helpers import desired, interface, physical
from network_helpers import resolved_model as model
from pydantic import ValidationError


def test_domain_objects_are_separate_and_deeply_immutable():
    resolved = model(desired([physical()], [interface()]))
    assert isinstance(resolved.devices, tuple)
    assert isinstance(resolved.interfaces, tuple)
    assert isinstance(resolved.routing.gateways, tuple)
    assert isinstance(resolved.routing.routes, tuple)
    assert resolved.devices[0].kind is DeviceKind.PHYSICAL
    assert resolved.devices[0].id == resolved.interfaces[0].device == "nic"
    assert resolved.interfaces[0].id == "lan"
    for obj, field, value in [
        (resolved, "devices", ()),
        (resolved.devices[0], "name", "eth2"),
        (resolved.interfaces[0], "device", "other"),
        (resolved.interfaces[0].addressing.ipv4, "address", "192.0.2.1/24"),
        (resolved.routing, "routes", ()),
    ]:
        with pytest.raises(FrozenInstanceError):
            setattr(obj, field, value)


def test_unassigned_devices_have_no_dynamic_addressing_or_netdev():
    config = desired([physical(), physical("other", "eth1")])
    candidate = plan_network(config)
    assert set(candidate.files) == {
        "10-kavernis-nic.network",
        "10-kavernis-other.network",
    }
    assert candidate.files["10-kavernis-nic.network"] == (
        "[Match]\nName=eth0\n\n[Network]\nIPv6AcceptRA=no\n"
        "DHCP=no\nLinkLocalAddressing=no\n"
    )


def test_l3_role_and_display_name_do_not_determine_artifact_names():
    config = desired(
        [physical("wan-nic", "enp1s0")],
        [interface("wan", "wan-nic", ipv4={"mode": "dhcp"})],
    )
    candidate = plan_network(config)
    item = config.interfaces.interfaces[0].model_copy(update={"name": "Internet"})
    renamed = replace(
        config, interfaces=config.interfaces.model_copy(update={"interfaces": (item,)})
    )
    assert candidate == plan_network(renamed)
    assert set(candidate.files) == {"10-kavernis-wan-nic.network"}
    assert "Name=enp1s0\n" in candidate.files["10-kavernis-wan-nic.network"]


@pytest.mark.parametrize(
    "devices,interfaces",
    [
        ([physical(), physical()], []),
        ([physical(), physical("other")], []),
        ([physical()], [interface(), interface(device="other")]),
        ([physical()], [interface(), interface("other")]),
    ],
)
def test_duplicate_ids_names_and_l3_assignments(devices, interfaces):
    with pytest.raises(ValidationError, match="duplicate"):
        desired(devices, interfaces)


def test_unknown_interface_device_and_kernel_name_reference_rejected():
    for ref in ["missing", "eth0"]:
        with pytest.raises(ValueError, match="does not exist"):
            plan_network(desired([physical()], [interface(device=ref)]))


def test_physical_device_name_length_boundaries():
    assert desired([physical(name="e" * 15)]).devices.devices[0].name == "e" * 15
    with pytest.raises(ValidationError, match="name"):
        desired([physical(name="e" * 16)])


def test_leading_dash_device_names_are_rejected_before_native_commands():
    with pytest.raises(ValidationError, match="name"):
        desired([physical(name="-eth0")])


def test_domain_callers_also_receive_duplicate_validation():
    resolved = model(desired([physical()], [interface()]))
    with pytest.raises(ValueError, match="duplicate device id"):
        resolve_topology(replace(resolved, devices=resolved.devices * 2))


@pytest.mark.parametrize(
    "field,value",
    [
        ("type", "bond"),
        ("id", "../unsafe"),
        ("name", "eth0\nDHCP=yes"),
        ("name", "eth*"),
        ("ipv4", {"mode": "dhcp"}),
        ("mtu", 9000),
    ],
)
def test_invalid_device_fields(field, value):
    device = physical()
    device[field] = value
    with pytest.raises(ValidationError):
        desired([device])
