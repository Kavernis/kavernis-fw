from pathlib import Path

import pytest
from kavernis.backends.networkd import NetworkdApplyError
from kavernis.core import network as network_core
from kavernis.core.network import (
    NETWORK_DOMAIN,
    NetworkStatusKind,
    load_network,
)
from network_helpers import service, write_desired


def test_network_apply_records_network_state_and_detects_all_file_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_service = service(tmp_path)
    write_desired(tmp_path / "etc/network")

    def install(candidate, directory):
        directory.mkdir(parents=True, exist_ok=True)
        for filename, content in candidate.files.items():
            (directory / filename).write_text(content)

    monkeypatch.setattr(network_core, "apply", install)
    desired = load_network(state_service.paths)
    state_service.apply()
    state = state_service.store.get_domain_state(NETWORK_DOMAIN)
    assert state is not None
    assert state_service.status(desired.files).kind is NetworkStatusKind.IN_SYNC
    for filename in (
        "network/devices.yaml",
        "network/interfaces.yaml",
        "network/routing.yaml",
    ):
        changed = dict(desired.files)
        changed[filename] += b"# changed\n"
        assert state_service.status(changed).kind is NetworkStatusKind.PENDING_CHANGES
    (tmp_path / "networkd/10-kavernis-lan-nic.network").write_text("changed\n")
    assert state_service.status(desired.files).kind is NetworkStatusKind.DRIFT_DETECTED


def test_failed_network_apply_keeps_previous_network_revision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_service = service(tmp_path)
    write_desired(tmp_path / "etc/network")
    monkeypatch.setattr(network_core, "apply", lambda candidate, directory: None)
    state_service.apply()
    previous = state_service.store.get_domain_state(NETWORK_DOMAIN)
    assert previous is not None
    write_desired(tmp_path / "etc/network", "192.168.20.1/24")
    monkeypatch.setattr(
        network_core,
        "apply",
        lambda candidate, directory: (_ for _ in ()).throw(
            NetworkdApplyError("reload failed")
        ),
    )
    with pytest.raises(NetworkdApplyError):
        state_service.apply()
    assert state_service.store.get_domain_state(NETWORK_DOMAIN) == previous


def test_network_rollback_restores_all_desired_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_service = service(tmp_path)
    monkeypatch.setattr(network_core, "apply", lambda candidate, directory: None)
    write_desired(tmp_path / "etc/network")
    state_service.apply()
    first = state_service.store.get_domain_state(NETWORK_DOMAIN)
    assert first is not None
    write_desired(tmp_path / "etc/network", "192.168.20.1/24")
    state_service.apply()
    state_service.rollback(first.revision)
    assert "192.168.10.1/24" in state_service.paths.interfaces_path.read_text()
    assert state_service.paths.devices_path.exists()
    assert state_service.paths.routing_path.exists()
