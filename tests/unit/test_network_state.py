from pathlib import Path

import pytest
from kavernis.backends.networkd import NetworkdApplyError
from kavernis.core import network as network_core
from kavernis.core.network import (
    NETWORK_DOMAIN,
    NetworkStatePaths,
    NetworkStateService,
    NetworkStatusKind,
    load_network,
)


def write_desired(path: Path, address: str = "192.168.10.1/24") -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "interfaces.yaml").write_text(
        f"version: 1\ninterfaces:\n  - id: lan\n    name: LAN\n    device: eth1\n"
        f"    ipv4: {{mode: static, address: {address}}}\n"
        "    ipv6: {mode: disabled}\n"
    )
    (path / "gateways.yaml").write_text(
        "version: 1\ngateways:\n  - id: internet\n    name: Internet\n"
        "    interface: lan\n    address: 192.168.10.254\n    onlink: true\n"
    )
    (path / "routes.yaml").write_text(
        "version: 1\nroutes:\n  - id: default-v4\n    network: 0.0.0.0/0\n"
        "    gateway: internet\n"
    )


def service(tmp_path: Path) -> NetworkStateService:
    return NetworkStateService(
        NetworkStatePaths(
            interfaces_path=tmp_path / "etc/interfaces.yaml",
            gateways_path=tmp_path / "etc/gateways.yaml",
            routes_path=tmp_path / "etc/routes.yaml",
            history_path=tmp_path / "history",
            database_path=tmp_path / "state.db",
            networkd_path=tmp_path / "networkd",
            lock_path=tmp_path / "state.lock",
        )
    )


def test_network_apply_records_network_state_and_detects_all_file_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_service = service(tmp_path)
    write_desired(tmp_path / "etc")

    def install(candidate, directory):
        directory.mkdir(parents=True, exist_ok=True)
        for filename, content in candidate.files.items():
            (directory / filename).write_text(content)

    monkeypatch.setattr(network_core, "apply", install)
    desired = load_network(state_service.paths)
    state_service.apply(desired)
    state = state_service.store.get_domain_state(NETWORK_DOMAIN)
    assert state is not None
    assert state_service.status(desired.files).kind is NetworkStatusKind.IN_SYNC
    for filename in ("interfaces.yaml", "gateways.yaml", "routes.yaml"):
        changed = dict(desired.files)
        changed[filename] += b"# changed\n"
        assert state_service.status(changed).kind is NetworkStatusKind.PENDING_CHANGES
    (tmp_path / "networkd/10-kavernis-lan.network").write_text("changed\n")
    assert state_service.status(desired.files).kind is NetworkStatusKind.DRIFT_DETECTED


def test_failed_network_apply_keeps_previous_network_revision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_service = service(tmp_path)
    write_desired(tmp_path / "etc")
    monkeypatch.setattr(network_core, "apply", lambda candidate, directory: None)
    state_service.apply(load_network(state_service.paths))
    previous = state_service.store.get_domain_state(NETWORK_DOMAIN)
    assert previous is not None
    write_desired(tmp_path / "etc", "192.168.20.1/24")
    monkeypatch.setattr(
        network_core,
        "apply",
        lambda candidate, directory: (_ for _ in ()).throw(
            NetworkdApplyError("reload failed")
        ),
    )
    with pytest.raises(NetworkdApplyError):
        state_service.apply(load_network(state_service.paths))
    assert state_service.store.get_domain_state(NETWORK_DOMAIN) == previous


def test_network_rollback_restores_all_desired_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_service = service(tmp_path)
    monkeypatch.setattr(network_core, "apply", lambda candidate, directory: None)
    write_desired(tmp_path / "etc")
    state_service.apply(load_network(state_service.paths))
    first = state_service.store.get_domain_state(NETWORK_DOMAIN)
    assert first is not None
    write_desired(tmp_path / "etc", "192.168.20.1/24")
    state_service.apply(load_network(state_service.paths))
    state_service.rollback(first.revision)
    assert "192.168.10.1/24" in state_service.paths.interfaces_path.read_text()
    assert state_service.paths.gateways_path.exists()
    assert state_service.paths.routes_path.exists()
