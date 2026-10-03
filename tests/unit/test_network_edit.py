"""Tests for safe, transactional network desired-state edits."""

import multiprocessing
from pathlib import Path

import pytest
from kavernis.core.network import (
    DesiredStateChangedError,
    EditorFailedError,
    EditValidationError,
    NetworkStatePaths,
    NetworkStateService,
)
from kavernis.state.lock import LockUnavailableError, network_lock


def write_desired(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "interfaces.yaml").write_text(
        "version: 1\ninterfaces:\n  - id: lan\n    name: LAN\n    device: eth1\n"
        "    ipv4: {mode: static, address: 192.168.10.1/24}\n"
        "    ipv6: {mode: disabled}\n"
    )
    (directory / "gateways.yaml").write_text("version: 1\ngateways: []\n")
    (directory / "routes.yaml").write_text("version: 1\nroutes: []\n")


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


def append_comment(path: Path) -> int:
    path.write_bytes(path.read_bytes() + b"# edited\n")
    return 0


def _try_network_lock(path: str, result: object) -> None:
    queue = result
    try:
        with network_lock(Path(path)):
            queue.put(False)  # type: ignore[attr-defined]
    except LockUnavailableError:
        queue.put(True)  # type: ignore[attr-defined]


@pytest.mark.parametrize("resource", ("interfaces", "gateways", "routes"))
def test_edit_resources_are_validated_and_written_with_header(
    tmp_path: Path, resource: str
) -> None:
    state_service = service(tmp_path)
    write_desired(tmp_path / "etc")

    assert state_service.edit_resource(resource, append_comment)

    contents = (tmp_path / "etc" / f"{resource}.yaml").read_text()
    assert contents.startswith("# Kavernis desired-state configuration.\n")
    assert f'kavernis edit {resource}' in contents
    assert "# edited" in contents


def test_noop_edit_does_not_rewrite_live_file(tmp_path: Path) -> None:
    state_service = service(tmp_path)
    write_desired(tmp_path / "etc")
    original = state_service.paths.routes_path.read_bytes()

    assert not state_service.edit_resource("routes", lambda path: 0)
    assert state_service.paths.routes_path.read_bytes() == original


def test_editor_failure_does_not_modify_live_file(tmp_path: Path) -> None:
    state_service = service(tmp_path)
    write_desired(tmp_path / "etc")
    original = state_service.paths.routes_path.read_bytes()

    with pytest.raises(EditorFailedError, match="editor exited with status 7"):
        state_service.edit_resource("routes", lambda path: 7)
    assert state_service.paths.routes_path.read_bytes() == original
    with state_service.network_write_transaction():
        pass


def test_invalid_edit_keeps_live_file_and_retains_candidate(tmp_path: Path) -> None:
    state_service = service(tmp_path)
    write_desired(tmp_path / "etc")
    original = state_service.paths.routes_path.read_bytes()

    def invalidate(path: Path) -> int:
        path.write_text("routes: [\n")
        return 0

    with pytest.raises(EditValidationError) as raised:
        state_service.edit_resource("routes", invalidate)
    assert state_service.paths.routes_path.read_bytes() == original
    assert raised.value.candidate_path.exists()
    raised.value.candidate_path.unlink()
    with state_service.network_write_transaction():
        pass


@pytest.mark.parametrize("changed", ("interfaces.yaml", "gateways.yaml", "routes.yaml"))
def test_external_change_to_any_network_file_aborts_edit(
    tmp_path: Path, changed: str
) -> None:
    state_service = service(tmp_path)
    desired = tmp_path / "etc"
    write_desired(desired)
    original_routes = state_service.paths.routes_path.read_bytes()

    def edit_and_change_elsewhere(path: Path) -> int:
        path.write_bytes(path.read_bytes() + b"# candidate\n")
        changed_path = desired / changed
        changed_path.write_bytes(changed_path.read_bytes() + b"# external\n")
        return 0

    with pytest.raises(DesiredStateChangedError):
        state_service.edit_resource("routes", edit_and_change_elsewhere)
    routes_after = state_service.paths.routes_path.read_bytes()
    if changed == "routes.yaml":
        assert routes_after == original_routes + b"# external\n"
    else:
        assert routes_after == original_routes


def test_fingerprint_covers_every_network_desired_file(tmp_path: Path) -> None:
    state_service = service(tmp_path)
    write_desired(tmp_path / "etc")

    assert set(state_service.desired_fingerprint()) == {
        "interfaces.yaml",
        "gateways.yaml",
        "routes.yaml",
    }


def test_core_transaction_is_reusable_without_cli(tmp_path: Path) -> None:
    state_service = service(tmp_path)
    write_desired(tmp_path / "etc")
    replacement = b"version: 1\nroutes: []\n# written by an API caller\n"

    with state_service.network_write_transaction():
        baseline = state_service.desired_fingerprint()
        state_service.replace_desired_resource("routes", replacement, baseline)

    assert "kavernis edit routes" in state_service.paths.routes_path.read_text()


def test_network_lock_is_nonblocking_and_released(tmp_path: Path) -> None:
    path = tmp_path / "run/network.lock"
    with network_lock(path):
        with pytest.raises(LockUnavailableError):
            with network_lock(path):
                pass
    with network_lock(path):
        pass


def test_network_lock_rejects_another_process(tmp_path: Path) -> None:
    path = tmp_path / "run/network.lock"
    context = multiprocessing.get_context("fork")
    result = context.Queue()
    with network_lock(path):
        process = context.Process(target=_try_network_lock, args=(str(path), result))
        process.start()
        process.join(timeout=5)
    assert process.exitcode == 0
    assert result.get(timeout=1)
