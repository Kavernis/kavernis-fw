"""Network desired-state behavior when static routing documents are absent."""

from pathlib import Path

import pytest
from kavernis.config.errors import ConfigurationError
from kavernis.core import network as network_core
from kavernis.core.network import (
    NetworkStatePaths,
    NetworkStateService,
    load_network,
    plan_network,
)


def service(tmp_path: Path) -> NetworkStateService:
    desired = tmp_path / "etc"
    return NetworkStateService(
        NetworkStatePaths(
            interfaces_path=desired / "interfaces.yaml",
            gateways_path=desired / "gateways.yaml",
            routes_path=desired / "routes.yaml",
            history_path=tmp_path / "history",
            database_path=tmp_path / "state.db",
            networkd_path=tmp_path / "networkd",
            lock_path=tmp_path / "run/network.lock",
        )
    )


def write_dhcp_interfaces(state_service: NetworkStateService) -> None:
    state_service.paths.interfaces_path.parent.mkdir(parents=True, exist_ok=True)
    state_service.paths.interfaces_path.write_text(
        "version: 1\ninterfaces:\n  - id: wan\n    name: WAN\n    device: enp1s0\n"
        "    ipv4:\n      mode: dhcp\n"
    )


def test_interfaces_is_mandatory_but_static_resources_are_optional(
    tmp_path: Path,
) -> None:
    state_service = service(tmp_path)
    with pytest.raises(ConfigurationError, match="mandatory interfaces"):
        load_network(state_service.paths)

    write_dhcp_interfaces(state_service)
    desired = load_network(state_service.paths)
    assert desired.gateways.version == 1
    assert desired.gateways.gateways == []
    assert desired.routes.version == 1
    assert desired.routes.routes == []
    assert set(desired.files) == {"interfaces.yaml"}


def test_dhcp_only_network_generates_policy_without_fake_static_route(
    tmp_path: Path,
) -> None:
    state_service = service(tmp_path)
    write_dhcp_interfaces(state_service)

    candidate = plan_network(load_network(state_service.paths))
    content = candidate.files["10-kavernis-wan.network"]
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

    state_service.paths.gateways_path.write_text("version: 1\ngateways: []\n")
    state_service.paths.routes_path.write_text("version: 1\nroutes: []\n")
    explicit = load_network(state_service.paths)

    assert absent.gateways == explicit.gateways
    assert absent.routes == explicit.routes
    assert absent_candidate == plan_network(explicit)


@pytest.mark.parametrize(
    ("filename", "contents"),
    [
        ("gateways.yaml", "version: 1\ngateways: [\n"),
        ("routes.yaml", "version: 2\nroutes: []\n"),
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
    assert absent["gateways.yaml"] == "absent"
    state_service.paths.gateways_path.write_text("version: 1\ngateways: []\n")
    present = state_service.desired_fingerprint()
    assert present["gateways.yaml"].startswith("present:")
    assert present != absent


@pytest.mark.parametrize("resource", ("gateways", "routes"))
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

    state_service.paths.gateways_path.write_text("version: 1\ngateways: []\n")
    state_service.paths.routes_path.write_text("version: 1\nroutes: []\n")
    state_service.apply()
    assert set(state_service.history.file_names()) == {
        "interfaces.yaml",
        "gateways.yaml",
        "routes.yaml",
    }

    state_service.rollback(first.revision)
    assert not state_service.paths.gateways_path.exists()
    assert not state_service.paths.routes_path.exists()
    assert state_service.history.file_names() == ("interfaces.yaml",)


def test_diff_represents_optional_file_creation_and_deletion(tmp_path: Path) -> None:
    state_service = service(tmp_path)
    write_dhcp_interfaces(state_service)
    first = state_service.history.snapshot(
        load_network(state_service.paths).files, "initial network desired state"
    )
    state_service.paths.gateways_path.write_text("version: 1\ngateways: []\n")
    created = load_network(state_service.paths)
    assert "gateways.yaml (current)" in state_service.diff(created.files, first)

    second = state_service.history.snapshot(created.files, "add gateways")
    state_service.paths.gateways_path.unlink()
    current = load_network(state_service.paths)
    assert "gateways.yaml@" in state_service.diff(current.files, second)
