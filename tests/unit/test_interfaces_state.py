from pathlib import Path

import pytest
from kavernis.backends.networkd import NetworkdApplyError
from kavernis.config.loader import load_interfaces_contents
from kavernis.core import interfaces as interfaces_core
from kavernis.core.interfaces import (
    InterfacesStatePaths,
    InterfacesStateService,
    InterfacesStatusKind,
)


def desired(address: str) -> bytes:
    return f"""version: 1
interfaces:
  - id: lan
    name: Users LAN
    device: eth1
    ipv4: {{mode: static, address: {address}}}
    ipv6: {{mode: disabled}}
""".encode()


def service(tmp_path: Path) -> InterfacesStateService:
    return InterfacesStateService(
        InterfacesStatePaths(
            desired_path=tmp_path / "etc" / "interfaces.yaml",
            history_path=tmp_path / "var" / "history",
            database_path=tmp_path / "var" / "state.db",
            networkd_path=tmp_path / "networkd",
            lock_path=tmp_path / "var" / "state.lock",
        )
    )


def test_apply_records_hashes_only_after_native_apply_and_detects_drift(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_service = service(tmp_path)
    contents = desired("192.168.10.1/24")
    config = load_interfaces_contents(contents.decode())

    def install(candidate, directory):
        directory.mkdir(parents=True, exist_ok=True)
        for filename, content in candidate.files.items():
            (directory / filename).write_text(content, encoding="utf-8")

    monkeypatch.setattr(interfaces_core, "apply", install)
    state_service.apply(config, contents)

    state = state_service.store.get_domain_state("interfaces")
    assert state is not None
    assert state.artifact_hashes
    assert state_service.status(contents).kind is InterfacesStatusKind.IN_SYNC

    (tmp_path / "networkd" / "10-kavernis-lan.network").write_text("changed\n")
    status = state_service.status(contents)
    assert status.kind is InterfacesStatusKind.DRIFT_DETECTED
    assert status.drift[0].kind.value == "modified"

    (tmp_path / "networkd" / "10-kavernis-extra.network").write_text("extra\n")
    assert {item.kind.value for item in state_service.status(contents).drift} == {
        "modified",
        "unexpected",
    }


def test_failed_apply_keeps_previous_successful_revision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_service = service(tmp_path)
    first = desired("192.168.10.1/24")
    monkeypatch.setattr(interfaces_core, "apply", lambda candidate, directory: None)
    state_service.apply(load_interfaces_contents(first.decode()), first)
    previous = state_service.store.get_domain_state("interfaces")
    assert previous is not None

    def fail(candidate, directory):
        raise NetworkdApplyError("reload failed")

    monkeypatch.setattr(interfaces_core, "apply", fail)
    second = desired("192.168.20.1/24")
    with pytest.raises(NetworkdApplyError):
        state_service.apply(load_interfaces_contents(second.decode()), second)

    assert state_service.store.get_domain_state("interfaces") == previous
    assert state_service.status(second).kind is InterfacesStatusKind.PENDING_CHANGES


def test_rollback_creates_new_linear_history_revision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_service = service(tmp_path)
    monkeypatch.setattr(interfaces_core, "apply", lambda candidate, directory: None)
    first = desired("192.168.10.1/24")
    second = desired("192.168.20.1/24")
    state_service.paths.desired_path.parent.mkdir(parents=True)
    state_service.paths.desired_path.write_bytes(first)
    state_service.apply(load_interfaces_contents(first.decode()), first)
    first_revision = state_service.store.get_domain_state("interfaces")
    assert first_revision is not None
    state_service.paths.desired_path.write_bytes(second)
    state_service.apply(load_interfaces_contents(second.decode()), second)

    state_service.rollback(first_revision.revision)

    revisions = state_service.history_revisions()
    assert len(revisions) == 3
    assert revisions[0].identifier != first_revision.revision
    assert state_service.paths.desired_path.read_bytes() == first
    applied = state_service.store.get_domain_state("interfaces")
    assert applied is not None
    assert applied.revision == revisions[0].identifier
