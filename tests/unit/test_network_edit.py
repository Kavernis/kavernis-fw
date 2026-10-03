"""Tests for safe, transactional network desired-state edits."""

import multiprocessing
from pathlib import Path

import pytest
from kavernis.core.network import (
    DesiredStateChangedError,
    EditorFailedError,
    NetworkStatePaths,
    NetworkStateService,
    load_network,
    plan_network,
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
    assert not list((tmp_path / "etc").glob(".kavernis-routes-*.yaml"))
    with state_service.network_write_transaction():
        pass


@pytest.mark.parametrize(
    ("edited", "invalid_other"),
    [
        ("interfaces", "routes.yaml"),
        ("interfaces", "gateways.yaml"),
        ("routes", "interfaces.yaml"),
        ("gateways", "routes.yaml"),
    ],
)
def test_valid_resource_edit_ignores_invalid_other_resource(
    tmp_path: Path, edited: str, invalid_other: str
) -> None:
    state_service = service(tmp_path)
    desired = tmp_path / "etc"
    write_desired(desired)
    (desired / invalid_other).write_text("not: [valid")

    assert state_service.edit_resource(edited, append_comment)
    assert "# edited" in (desired / f"{edited}.yaml").read_text()


@pytest.mark.parametrize(
    ("resource", "contents"),
    [
        (
            "gateways",
            b"version: 1\ngateways:\n  - id: missing\n    name: Missing\n"
            b"    interface: absent\n",
        ),
        (
            "routes",
            b"version: 1\nroutes:\n  - id: missing\n    network: 10.0.0.0/24\n"
            b"    gateway: absent\n",
        ),
    ],
)
def test_cross_resource_references_can_be_saved_during_edit(
    tmp_path: Path, resource: str, contents: bytes
) -> None:
    state_service = service(tmp_path)
    write_desired(tmp_path / "etc")

    with state_service.network_write_transaction():
        state_service.replace_desired_resource(
            resource, contents, state_service.desired_fingerprint()
        )


@pytest.mark.parametrize(
    ("resource", "invalid"),
    [
        ("interfaces", "version: 1\ninterfaces: []\n"),
        ("gateways", "version: 1\ngateways: [\n"),
        ("routes", "version: 2\nroutes: []\n"),
    ],
)
def test_invalid_resource_edit_is_not_saved(
    tmp_path: Path, resource: str, invalid: str
) -> None:
    state_service = service(tmp_path)
    write_desired(tmp_path / "etc")
    path = tmp_path / "etc" / f"{resource}.yaml"
    original = path.read_bytes()

    def invalidate(path: Path) -> int:
        path.write_text(invalid)
        return 0

    assert not state_service.edit_resource(resource, invalidate)
    assert path.read_bytes() == original
    assert not list(path.parent.glob(f".kavernis-{resource}-*.yaml"))
    with state_service.network_write_transaction():
        pass


def test_invalid_edit_reuses_candidate_then_saves_corrected_content(
    tmp_path: Path,
) -> None:
    state_service = service(tmp_path)
    write_desired(tmp_path / "etc")
    candidates: list[Path] = []

    def edit(path: Path) -> int:
        candidates.append(path)
        if len(candidates) == 1:
            path.write_text("routes: [\n")
        else:
            assert path.read_text() == "routes: [\n"
            path.write_text("version: 1\nroutes: []\n# corrected\n")
        return 0

    errors: list[str] = []
    assert state_service.edit_resource(
        "routes", edit, lambda error: errors.append(str(error)) or True
    )
    assert len(candidates) == 2
    assert candidates[0] == candidates[1]
    assert errors
    assert "# corrected" in state_service.paths.routes_path.read_text()
    assert not candidates[0].exists()


def test_invalid_edit_abort_cleans_up_candidate_and_leaves_live_file(
    tmp_path: Path,
) -> None:
    state_service = service(tmp_path)
    write_desired(tmp_path / "etc")
    original = state_service.paths.routes_path.read_bytes()

    def invalidate(path: Path) -> int:
        path.write_text("routes: [\n")
        return 0

    assert not state_service.edit_resource("routes", invalidate, lambda _: False)
    assert state_service.paths.routes_path.read_bytes() == original
    assert not list((tmp_path / "etc").glob(".kavernis-routes-*.yaml"))


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
    assert not list(desired.glob(".kavernis-routes-*.yaml"))


@pytest.mark.parametrize(
    ("filename", "contents", "error"),
    [
        (
            "gateways.yaml",
            "version: 1\ngateways:\n  - id: bad\n    name: Bad\n"
            "    interface: absent\n",
            "does not exist",
        ),
        (
            "routes.yaml",
            "version: 1\nroutes:\n  - id: bad\n    network: 10.0.0.0/24\n"
            "    gateway: absent\n",
            "does not exist",
        ),
        (
            "routes.yaml",
            "version: 1\nroutes:\n  - id: bad\n    network: ::/0\n    gateway: local\n",
            "family",
        ),
        (
            "gateways.yaml",
            "version: 1\ngateways:\n  - id: local\n    name: Local\n"
            "    interface: lan\n    address: 10.0.0.1\n",
            "onlink: true",
        ),
    ],
)
def test_plan_network_keeps_cross_resource_validation(
    tmp_path: Path, filename: str, contents: str, error: str
) -> None:
    state_service = service(tmp_path)
    desired = tmp_path / "etc"
    write_desired(desired)
    if filename == "routes.yaml" and "gateway: local" in contents:
        (desired / "gateways.yaml").write_text(
            "version: 1\ngateways:\n  - id: local\n    name: Local\n"
            "    interface: lan\n    address: 192.168.10.254\n"
        )
    (desired / filename).write_text(contents)

    with pytest.raises(ValueError, match=error):
        plan_network(load_network(state_service.paths))


def test_apply_runs_complete_network_validation_before_native_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_service = service(tmp_path)
    desired = tmp_path / "etc"
    write_desired(desired)
    (desired / "gateways.yaml").write_text(
        "version: 1\ngateways:\n  - id: bad\n    name: Bad\n    interface: absent\n"
    )
    monkeypatch.setattr("kavernis.core.network.apply", lambda *_: pytest.fail())

    with pytest.raises(ValueError, match="does not exist"):
        state_service.apply()


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


def test_retry_keeps_network_lock_for_entire_edit_transaction(tmp_path: Path) -> None:
    state_service = service(tmp_path)
    write_desired(tmp_path / "etc")
    context = multiprocessing.get_context("fork")
    result = context.Queue()

    def invalidate(path: Path) -> int:
        path.write_text("routes: [\n")
        return 0

    def abort_while_locked(_: object) -> bool:
        process = context.Process(
            target=_try_network_lock,
            args=(str(state_service.paths.lock_path), result),
        )
        process.start()
        process.join(timeout=5)
        assert process.exitcode == 0
        assert result.get(timeout=1)
        return False

    assert not state_service.edit_resource("routes", invalidate, abort_while_locked)
    with state_service.network_write_transaction():
        pass


def test_keyboard_interrupt_cleans_up_candidate_and_releases_lock(
    tmp_path: Path,
) -> None:
    state_service = service(tmp_path)
    write_desired(tmp_path / "etc")

    def interrupt(_: Path) -> int:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        state_service.edit_resource("routes", interrupt)
    assert not list((tmp_path / "etc").glob(".kavernis-routes-*.yaml"))
    with state_service.network_write_transaction():
        pass
