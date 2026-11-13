"""Network desired-state behavior when static routing documents are absent."""

from pathlib import Path

import pytest
from kavernis.config.errors import ConfigurationError
from kavernis.core import network as network_core
from kavernis.core.network import (
    NetworkStateService,
    load_network,
    plan_network,
)
from network_helpers import service


def write_dhcp_interfaces(state_service: NetworkStateService) -> None:
    state_service.paths.interfaces_path.parent.mkdir(parents=True, exist_ok=True)
    state_service.paths.devices_path.write_text(
        "version: 2\ndevices: [{id: wan-nic, type: physical, name: enp1s0}]\n"
    )
    state_service.paths.interfaces_path.write_text(
        "version: 2\ninterfaces:\n  - id: wan\n    name: WAN\n    device: wan-nic\n"
        "    ipv4:\n      mode: dhcp\n"
    )


def test_interfaces_is_mandatory_but_static_resources_are_optional(
    tmp_path: Path,
) -> None:
    state_service = service(tmp_path)
    with pytest.raises(ConfigurationError, match="mandatory network/devices"):
        load_network(state_service.paths)

    write_dhcp_interfaces(state_service)
    desired = load_network(state_service.paths)
    assert desired.routing.version == 2
    assert desired.routing.gateways == ()
    assert desired.routing.routes == ()
    assert set(desired.files) == {"network/devices.yaml", "network/interfaces.yaml"}


def test_dhcp_only_network_generates_policy_without_fake_static_route(
    tmp_path: Path,
) -> None:
    state_service = service(tmp_path)
    write_dhcp_interfaces(state_service)

    candidate = plan_network(load_network(state_service.paths))
    content = candidate.files["10-kavernis-wan-nic.network"]
    assert "DHCP=ipv4\n" in content
    assert "[DHCPv4]\n" in content
    assert "UseRoutes=yes\n" in content
    assert "[Route]" not in content
    assert "Gateway=" not in content


def test_absent_and_explicit_empty_static_resources_are_semantically_equal(
    tmp_path: Path,
) -> None:
    state_service = service(tmp_path)
    write_dhcp_interfaces(state_service)
    absent = load_network(state_service.paths)
    absent_candidate = plan_network(absent)

    state_service.paths.routing_path.write_text("version: 2\nroutes: []\n")
    explicit = load_network(state_service.paths)

    assert absent.routing == explicit.routing
    assert absent_candidate == plan_network(explicit)


@pytest.mark.parametrize(
    ("filename", "contents"),
    [
        ("routing.yaml", "version: 2\ngateways: [\n"),
        ("routing.yaml", "version: 1\nroutes: []\n"),
    ],
)
def test_invalid_existing_optional_resources_are_not_ignored(
    tmp_path: Path, filename: str, contents: str
) -> None:
    state_service = service(tmp_path)
    write_dhcp_interfaces(state_service)
    (state_service.paths.interfaces_path.parent / filename).write_text(contents)
    with pytest.raises(ConfigurationError):
        load_network(state_service.paths)


def test_optional_file_presence_is_part_of_concurrency_fingerprint(
    tmp_path: Path,
) -> None:
    state_service = service(tmp_path)
    write_dhcp_interfaces(state_service)
    absent = state_service.desired_fingerprint()
    assert absent["network/routing.yaml"] == "absent"
    state_service.paths.routing_path.write_text("version: 2\nroutes: []\n")
    present = state_service.desired_fingerprint()
    assert present["network/routing.yaml"].startswith("present:")
    assert present != absent


@pytest.mark.parametrize("resource", ("routing",))
def test_editing_absent_optional_resource_creates_only_a_saved_document(
    tmp_path: Path, resource: str
) -> None:
    state_service = service(tmp_path)
    write_dhcp_interfaces(state_service)
    path = state_service.paths.interfaces_path.parent / f"{resource}.yaml"

    assert not state_service.edit_resource(resource, lambda _: 0)
    assert not path.exists()

    def add_comment(candidate: Path) -> int:
        candidate.write_bytes(candidate.read_bytes() + b"# created\n")
        return 0

    assert state_service.edit_resource(resource, add_comment)
    assert path.exists()
    assert f"kavernis edit {resource}" in path.read_text()


def test_history_and_rollback_preserve_absent_optional_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_service = service(tmp_path)
    write_dhcp_interfaces(state_service)
    monkeypatch.setattr(network_core, "apply", lambda *_: None)
    state_service.apply()
    first = state_service.store.get_domain_state("network")
    assert first is not None

    state_service.paths.routing_path.write_text("version: 2\nroutes: []\n")
    state_service.apply()
    assert set(state_service.history.file_names()) == {
        "network/devices.yaml",
        "network/interfaces.yaml",
        "network/routing.yaml",
    }

    state_service.rollback(first.revision)
    assert not state_service.paths.routing_path.exists()
    assert state_service.history.file_names() == (
        "network/devices.yaml",
        "network/interfaces.yaml",
    )


def test_diff_represents_optional_file_creation_and_deletion(tmp_path: Path) -> None:
    state_service = service(tmp_path)
    write_dhcp_interfaces(state_service)
    first = state_service.history.snapshot(
        load_network(state_service.paths).files, "initial network desired state"
    )
    state_service.paths.routing_path.write_text("version: 2\nroutes: []\n")
    created = load_network(state_service.paths)
    assert "network/routing.yaml (current)" in state_service.diff(created.files, first)

    second = state_service.history.snapshot(created.files, "add routing")
    state_service.paths.routing_path.unlink()
    current = load_network(state_service.paths)
    assert "network/routing.yaml@" in state_service.diff(current.files, second)


@pytest.mark.parametrize("missing", ["devices_path", "interfaces_path"])
def test_both_documents_are_mandatory(tmp_path, missing):
    state_service = service(tmp_path)
    write_dhcp_interfaces(state_service)
    getattr(state_service.paths, missing).unlink()
    with pytest.raises(ConfigurationError, match="mandatory"):
        load_network(state_service.paths)


def test_optional_resource_read_error_is_not_treated_as_absence(tmp_path):
    state_service = service(tmp_path)
    write_dhcp_interfaces(state_service)
    state_service.paths.routing_path.mkdir()
    with pytest.raises(ConfigurationError, match="cannot load network/routing.yaml"):
        load_network(state_service.paths)
