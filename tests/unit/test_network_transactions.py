"""Whole network transactions with real temporary history/SQLite/artifacts.

Only networkctl is mocked. No test invokes native networking on the host.
"""

import subprocess

import pytest
from kavernis.backends import networkd
from kavernis.core import network as core
from kavernis.state.history import HistoryError
from kavernis.state.sqlite import StateStoreError
from network_helpers import service, write_desired


@pytest.fixture
def reload_control(monkeypatch):
    original = subprocess.run
    control = {"fail": False, "calls": 0}

    def run(arguments, **kwargs):
        if arguments[0] == "networkctl":
            assert arguments == ["networkctl", "reload"]
            control["calls"] += 1
            if control["fail"] and kwargs.get("check"):
                raise subprocess.CalledProcessError(
                    1, arguments, stderr="reload failed"
                )
            return subprocess.CompletedProcess(arguments, 0)
        return original(arguments, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    return control


def test_apply_cleans_obsolete_owned_artifacts_and_preserves_unrelated(
    tmp_path,
    reload_control,
):
    state = service(tmp_path)
    write_desired(state.paths.interfaces_path.parent)
    native = state.paths.networkd_path
    native.mkdir()
    (native / "10-kavernis-obsolete.network").write_bytes(b"old\r\n")
    (native / "10-kavernis-obsolete.netdev").write_bytes(b"old virtual\n")
    unrelated = {
        "20-other.network": b"unrelated\n",
        "10-kavernis-not-owned.link": b"unrelated link\n",
        "10-kavernis-example.network.bak": b"backup\n",
    }
    for filename, contents in unrelated.items():
        (native / filename).write_bytes(contents)
    candidate = state.apply()
    assert set(path.name for path in native.iterdir()) == set(candidate.files) | set(
        unrelated
    )
    for filename, contents in unrelated.items():
        assert (native / filename).read_bytes() == contents
    assert (
        state.status(core.load_network(state.paths).files).kind
        is core.NetworkStatusKind.IN_SYNC
    )
    assert reload_control["calls"] == 1


def test_failed_apply_restores_exact_artifacts_and_successful_metadata(
    tmp_path,
    reload_control,
):
    state = service(tmp_path)
    write_desired(state.paths.interfaces_path.parent)
    state.apply()
    first = state.store.get_domain_state("network")
    extra = state.paths.networkd_path / "10-kavernis-obsolete.network"
    extra.write_bytes(b"previous bytes\r\n\xff\n")
    previous = {
        path.name: path.read_bytes() for path in state.paths.networkd_path.iterdir()
    }
    write_desired(state.paths.interfaces_path.parent, "192.168.20.1/24")
    reload_control["fail"] = True
    with pytest.raises(networkd.NetworkdApplyError, match="reload failed"):
        state.apply()
    assert state.store.get_domain_state("network") == first
    assert {
        path.name: path.read_bytes() for path in state.paths.networkd_path.iterdir()
    } == previous
    assert len(state.history_revisions()) == 2
    assert reload_control["calls"] == 3
    assert (
        state.status(core.load_network(state.paths).files).kind
        is core.NetworkStatusKind.PENDING_CHANGES
    )


def test_rollback_rebuilds_desired_state_and_preserves_history(
    tmp_path, reload_control
):
    state = service(tmp_path)
    directory = state.paths.interfaces_path.parent
    write_desired(directory)
    first_candidate = state.apply()
    first = state.history.current_revision()
    write_desired(directory, "192.168.20.1/24")
    state.apply()
    second = state.history.current_revision()
    restored = state.rollback(first)
    assert restored == first_candidate
    revisions = state.history_revisions()
    assert len(revisions) == 3
    assert revisions[0].identifier not in (first, second)
    assert [revision.identifier for revision in revisions[1:]] == [second, first]
    assert "id: lan\n" in state.paths.interfaces_path.read_text()
    assert "id: lan-nic" in state.paths.devices_path.read_text()
    assert (
        state.status(core.load_network(state.paths).files).kind
        is core.NetworkStatusKind.IN_SYNC
    )


def test_failed_native_rollback_keeps_attempted_desired_and_previous_applied(
    tmp_path,
    reload_control,
):
    state = service(tmp_path)
    directory = state.paths.interfaces_path.parent
    write_desired(directory)
    state.apply()
    first = state.history.current_revision()
    write_desired(directory, "192.168.20.1/24")
    state.apply()
    previous = state.store.get_domain_state("network")
    artifacts = {p.name: p.read_bytes() for p in state.paths.networkd_path.iterdir()}
    reload_control["fail"] = True
    with pytest.raises(networkd.NetworkdApplyError):
        state.rollback(first)
    assert "192.168.10.1/24" in state.paths.interfaces_path.read_text()
    assert state.store.get_domain_state("network") == previous
    assert len(state.history_revisions()) == 3
    assert {
        p.name: p.read_bytes() for p in state.paths.networkd_path.iterdir()
    } == artifacts


def test_invalid_historical_topology_never_replaces_current_files(
    tmp_path,
    reload_control,
):
    state = service(tmp_path)
    write_desired(state.paths.interfaces_path.parent)
    current = core.load_network(state.paths)
    bad = dict(current.files)
    bad["network/interfaces.yaml"] = bad["network/interfaces.yaml"].replace(
        b"device: lan-nic", b"device: absent"
    )
    revision = state.history.snapshot(bad, "invalid topology")
    with pytest.raises(ValueError, match="does not exist"):
        state.rollback(revision)
    assert core.load_network(state.paths).files == current.files
    assert reload_control["calls"] == 0


@pytest.mark.parametrize("failure", ["write", "history"])
def test_pre_activation_rollback_failure_restores_desired_snapshot(
    tmp_path,
    monkeypatch,
    reload_control,
    failure,
):
    state = service(tmp_path)
    write_desired(state.paths.interfaces_path.parent)
    state.apply()
    first = state.history.current_revision()
    write_desired(state.paths.interfaces_path.parent, "192.168.20.1/24")
    state.apply()
    previous = core.load_network(state.paths).files
    applied = state.store.get_domain_state("network")
    calls = reload_control["calls"]
    if failure == "write":
        original = core._write_desired_atomically
        failed = False

        def write(path, contents):
            nonlocal failed
            if path == state.paths.interfaces_path and not failed:
                failed = True
                raise OSError("replacement failed")
            original(path, contents)

        monkeypatch.setattr(core, "_write_desired_atomically", write)
        error_type = OSError
    else:

        def snapshot(*_):
            raise HistoryError("history failed")

        monkeypatch.setattr(state.history, "snapshot", snapshot)
        error_type = HistoryError
    with pytest.raises(error_type):
        state.rollback(first)
    assert core.load_network(state.paths).files == previous
    assert state.store.get_domain_state("network") == applied
    assert reload_control["calls"] == calls


def test_applied_metadata_failure_is_reported_after_native_success(
    tmp_path,
    monkeypatch,
    reload_control,
):
    state = service(tmp_path)
    write_desired(state.paths.interfaces_path.parent)
    state.apply()
    previous = state.store.get_domain_state("network")
    write_desired(state.paths.interfaces_path.parent, "192.168.20.1/24")

    def fail(*_):
        raise StateStoreError("metadata write failed")

    monkeypatch.setattr(state.store, "record_successful_apply", fail)
    with pytest.raises(StateStoreError, match="metadata write failed"):
        state.apply()
    assert state.store.get_domain_state("network") == previous
    assert (
        "192.168.20.1/24"
        in (state.paths.networkd_path / "10-kavernis-lan-nic.network").read_text()
    )


def test_external_change_during_planning_aborts_before_snapshot_or_apply(
    tmp_path,
    monkeypatch,
    reload_control,
):
    state = service(tmp_path)
    write_desired(state.paths.interfaces_path.parent)
    original = core.plan_network

    def plan(desired):
        candidate = original(desired)
        state.paths.routing_path.unlink()
        return candidate

    monkeypatch.setattr(core, "plan_network", plan)
    with pytest.raises(core.DesiredStateChangedError):
        state.apply()
    assert not state.paths.history_path.exists()
    assert not state.paths.database_path.exists()
    assert reload_control["calls"] == 0
