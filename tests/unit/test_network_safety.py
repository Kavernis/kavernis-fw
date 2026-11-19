"""Failure injection around native mutations using temporary files only."""

import sqlite3
import subprocess
from dataclasses import replace

import pytest
import yaml
from kavernis.backends import networkd
from kavernis.backends.network_runtime import (
    RuntimeLink,
    RuntimeVerificationError,
)
from kavernis.core import network as core
from kavernis.state.models import ApplyOutcomeKind
from kavernis.state.sqlite import StateStoreError
from network_helpers import service, write_desired


@pytest.fixture
def native(monkeypatch):
    original = subprocess.run
    calls = []
    links = {"eth1", "enp1s0"}

    def run(args, **kwargs):
        if args[0] == "networkctl":
            calls.append(args)
            return subprocess.CompletedProcess(args, 0)
        if args[0] == "ip":
            calls.append(args)
            if args[1:6] == ["-j", "-d", "link", "show", "dev"]:
                name = args[-1]
                return subprocess.CompletedProcess(
                    args, 0 if name in links else 1, "", "Cannot find device"
                )
            if args[1:3] == ["link", "add"]:
                links.add(args[args.index("name") + 1])
            elif args[1:3] == ["link", "delete"]:
                links.discard(args[-1])
            return subprocess.CompletedProcess(args, 0)
        return original(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    return calls


def artifacts(state):
    path = state.paths.networkd_path
    return {p.name: p.read_bytes() for p in path.iterdir()} if path.exists() else {}


def test_verification_failure_recovers_without_advancing_applied(
    tmp_path, monkeypatch, native
):
    state = service(tmp_path)
    write_desired(state.paths.interfaces_path.parent)
    state.apply()
    previous = state.store.get_domain_state("network")
    old_artifacts = artifacts(state)
    write_desired(state.paths.interfaces_path.parent, "192.168.20.1/24")
    original = state.inspector.inspect

    def inspect(timeout):
        snap = original(timeout)
        return replace(
            snap,
            links=tuple(
                replace(link, addresses=frozenset({"192.168.10.1/24"}))
                if link.name == "eth1"
                else link
                for link in snap.links
            ),
        )

    monkeypatch.setattr(state.inspector, "inspect", inspect)
    with pytest.raises(
        RuntimeVerificationError, match="missing static address"
    ) as caught:
        state.apply()
    assert artifacts(state) == old_artifacts
    assert state.store.get_domain_state("network") == previous
    assert "runtime restoration verified" in " ".join(caught.value.__notes__)
    outcome = state.store.get_last_apply_outcome("network")
    assert outcome.kind is ApplyOutcomeKind.FAILURE
    assert "missing static address" in outcome.message
    assert len(native) == 3


@pytest.mark.parametrize(
    "stage", ["preflight", "replace", "reload", "verify", "record"]
)
@pytest.mark.parametrize("cause", [KeyboardInterrupt, SystemExit])
def test_cancellation_restores_artifacts_and_preserves_cause(
    tmp_path, monkeypatch, native, stage, cause
):
    state = service(tmp_path)
    write_desired(state.paths.interfaces_path.parent)
    state.apply()
    previous = state.store.get_domain_state("network")
    old_artifacts = artifacts(state)
    write_desired(state.paths.interfaces_path.parent, "192.168.20.1/24")
    interruption = cause("cancel requested")
    fired = False

    def once():
        nonlocal fired
        if not fired:
            fired = True
            raise interruption

    if stage == "preflight":
        monkeypatch.setattr(state.store, "preflight", lambda *_, **kwargs: once())
    elif stage == "replace":
        original = networkd._write_atomically

        def write(*args):
            original(*args)
            once()

        monkeypatch.setattr(networkd, "_write_atomically", write)
    elif stage == "reload":
        original_run = subprocess.run

        def run(args, **kwargs):
            if args[0] == "networkctl":
                once()
            return original_run(args, **kwargs)

        monkeypatch.setattr(subprocess, "run", run)
    elif stage == "verify":
        original_verify = core.verify_runtime

        def verify(*args, **kwargs):
            once()
            return original_verify(*args, **kwargs)

        monkeypatch.setattr(core, "verify_runtime", verify)
    else:
        monkeypatch.setattr(
            state.store, "record_successful_apply", lambda *_, **kwargs: once()
        )

    with pytest.raises(cause) as caught:
        state.apply()
    assert caught.value is interruption
    assert artifacts(state) == old_artifacts
    assert state.store.get_domain_state("network") == previous
    notes = " ".join(caught.value.__notes__)
    if stage == "preflight":
        assert len(native) == 1
        assert "before native mutation" in notes
    else:
        assert "runtime restoration verified" in notes
        assert (
            state.store.get_last_apply_outcome("network").kind
            is ApplyOutcomeKind.FAILURE
        )


def test_interruption_preserved_when_recovery_and_outcome_recording_fail(
    tmp_path, monkeypatch, native
):
    state = service(tmp_path)
    write_desired(state.paths.interfaces_path.parent)
    state.apply()
    previous = state.store.get_domain_state("network")
    interruption = KeyboardInterrupt()

    def cancel(*args, **kwargs):
        raise interruption

    def fail(*args, **kwargs):
        raise OSError("recovery disk failure")

    monkeypatch.setattr(core, "verify_runtime", cancel)
    monkeypatch.setattr(networkd, "_restore", fail)
    monkeypatch.setattr(state.store, "record_failed_apply", fail)
    with pytest.raises(KeyboardInterrupt) as caught:
        state.apply()
    assert caught.value is interruption
    notes = " ".join(caught.value.__notes__)
    assert "artifact recovery failed" in notes
    assert "outcome could not be recorded" in notes
    assert state.store.get_domain_state("network") == previous


@pytest.mark.parametrize("corruption", ["version", "corrupt", "table", "column"])
@pytest.mark.parametrize("operation", ["apply", "rollback"])
def test_sqlite_preflight_prevents_all_native_mutation(
    tmp_path, monkeypatch, native, corruption, operation
):
    state = service(tmp_path)
    write_desired(state.paths.interfaces_path.parent)
    state.apply()
    revision = state.history.current_revision()
    previous = artifacts(state)
    desired = core.load_network(state.paths).files
    path = state.paths.database_path
    if corruption == "corrupt":
        path.write_bytes(b"corrupt database")
    else:
        with sqlite3.connect(path) as db:
            if corruption == "version":
                db.execute("UPDATE schema_version SET version=999")
            elif corruption == "table":
                db.execute("DROP TABLE artifact_hash")
            else:
                db.execute(
                    "ALTER TABLE apply_operation RENAME COLUMN message TO broken"
                )
    monkeypatch.setattr(
        core, "apply", lambda *a, **kw: pytest.fail("native apply called")
    )
    with pytest.raises(StateStoreError):
        state.apply() if operation == "apply" else state.rollback(revision)
    assert artifacts(state) == previous
    assert core.load_network(state.paths).files == desired
    assert len(native) == 1


def test_late_sqlite_failure_recovers_and_records_failure(
    tmp_path, monkeypatch, native
):
    state = service(tmp_path)
    write_desired(state.paths.interfaces_path.parent)
    state.apply()
    previous = state.store.get_domain_state("network")
    old_artifacts = artifacts(state)
    write_desired(state.paths.interfaces_path.parent, "192.168.20.1/24")

    def fail(*args, **kwargs):
        raise StateStoreError("disk full after preflight")

    monkeypatch.setattr(state.store, "record_successful_apply", fail)
    with pytest.raises(StateStoreError, match="disk full"):
        state.apply()
    assert artifacts(state) == old_artifacts
    assert state.store.get_domain_state("network") == previous
    assert (
        state.store.get_last_apply_outcome("network").kind is ApplyOutcomeKind.FAILURE
    )


@pytest.mark.parametrize("virtual", ["vlan", "bridge"])
def test_structural_rollback_executes_lifecycle_before_desired_replacement(
    tmp_path, monkeypatch, native, virtual
):
    state = service(tmp_path)
    write_desired(state.paths.interfaces_path.parent)
    state.apply()
    old_revision = state.history.current_revision()
    device = {"id": "virtual", "name": "virt0", "type": virtual}
    if virtual == "vlan":
        device["vlan"] = {"parent": "lan-nic", "tag": 30}
    else:
        device["bridge"] = {"members": ["lan-nic"]}
        state.paths.interfaces_path.write_text("version: 2\ninterfaces: []\n")
    data = yaml.safe_load(state.paths.devices_path.read_text())
    data["devices"].append(device)
    state.paths.devices_path.write_text(yaml.safe_dump(data))
    original = state.inspector.inspect

    def observed(timeout):
        snap = original(timeout)
        if not (state.paths.networkd_path / "10-kavernis-virtual.netdev").exists():
            return snap
        if virtual == "vlan":
            link = RuntimeLink("virt0", "vlan", "eth1", 30)
        else:
            snap = replace(
                snap,
                links=tuple(
                    replace(link, master="virt0") if link.name == "eth1" else link
                    for link in snap.links
                ),
            )
            link = RuntimeLink("virt0", "bridge", stp=True)
        link = replace(
            link,
            setup_state="configured",
            network_file=str(state.paths.networkd_path / "10-kavernis-virtual.network"),
        )
        return replace(snap, links=(*snap.links, link))

    monkeypatch.setattr(state.inspector, "inspect", observed)
    state.apply()  # Initial creation must still work, including bridge attachments.
    previous = core.load_network(state.paths).files
    old_artifacts = artifacts(state)
    calls = []

    def lifecycle(_self, plan, _model, _progress):
        calls.extend(plan.operations)

    monkeypatch.setattr(
        networkd.NativeNetworkdOperations, "execute_lifecycle", lifecycle
    )
    state.rollback(old_revision)
    assert core.load_network(state.paths).files != previous
    assert artifacts(state) != old_artifacts
    assert any(operation.kind.value == "delete" for operation in calls)


def test_rollback_interruption_reports_changed_desired(tmp_path, monkeypatch, native):
    state = service(tmp_path)
    write_desired(state.paths.interfaces_path.parent)
    state.apply()
    revision = state.history.current_revision()
    write_desired(state.paths.interfaces_path.parent, "192.168.20.1/24")
    state.apply()
    previous = state.store.get_domain_state("network")
    old_artifacts = artifacts(state)
    original = core.verify_runtime
    first = True

    def cancel(*args, **kwargs):
        nonlocal first
        if first:
            first = False
            raise KeyboardInterrupt()
        return original(*args, **kwargs)

    monkeypatch.setattr(core, "verify_runtime", cancel)
    with pytest.raises(KeyboardInterrupt) as caught:
        state.rollback(revision)
    assert "rollback desired state already changed" in " ".join(caught.value.__notes__)
    assert "192.168.10.1/24" in state.paths.interfaces_path.read_text()
    assert artifacts(state) == old_artifacts
    assert state.store.get_domain_state("network") == previous
    assert state.store.get_last_apply_outcome("network").operation.value == "rollback"


def test_rollback_interruption_during_yaml_replacement_restores_desired(
    tmp_path, monkeypatch, native
):
    state = service(tmp_path)
    write_desired(state.paths.interfaces_path.parent)
    state.apply()
    revision = state.history.current_revision()
    write_desired(state.paths.interfaces_path.parent, "192.168.20.1/24")
    before = core.load_network(state.paths).files
    original = core._write_desired_atomically
    fired = False

    def write(*args):
        nonlocal fired
        original(*args)
        if not fired:
            fired = True
            raise KeyboardInterrupt()

    monkeypatch.setattr(core, "_write_desired_atomically", write)
    with pytest.raises(KeyboardInterrupt) as caught:
        state.rollback(revision)
    assert core.load_network(state.paths).files == before
    assert "previous desired resources restored" in " ".join(caught.value.__notes__)
    assert len(native) == 1


def test_missing_applied_history_prevents_native_changes(tmp_path, monkeypatch, native):
    state = service(tmp_path)
    write_desired(state.paths.interfaces_path.parent)
    state.apply()
    previous = artifacts(state)
    from kavernis.state.history import HistoryError

    def missing(*args):
        raise HistoryError("applied revision unavailable")

    monkeypatch.setattr(state.history, "read_revision", missing)
    with pytest.raises(HistoryError, match="unavailable"):
        state.apply()
    assert artifacts(state) == previous
    assert len(native) == 1
    assert (
        state.store.get_last_apply_outcome("network").kind is ApplyOutcomeKind.FAILURE
    )


def test_leftover_new_virtual_device_is_not_reported_as_recovered(
    tmp_path, monkeypatch, native
):
    state = service(tmp_path)
    write_desired(state.paths.interfaces_path.parent)
    state.apply()
    data = yaml.safe_load(state.paths.devices_path.read_text())
    data["devices"].append(
        {
            "id": "vlan",
            "name": "vlan30",
            "type": "vlan",
            "vlan": {"parent": "lan-nic", "tag": 30},
        }
    )
    state.paths.devices_path.write_text(yaml.safe_dump(data))
    original = state.inspector.inspect
    created = False

    def inspect(timeout):
        nonlocal created
        snap = original(timeout)
        created |= (state.paths.networkd_path / "10-kavernis-vlan.netdev").exists()
        if created:
            snap = replace(
                snap,
                links=(
                    *snap.links,
                    RuntimeLink(
                        "vlan30",
                        "vlan",
                        "eth1",
                        30,
                        network_file=str(
                            state.paths.networkd_path / "10-kavernis-vlan.network"
                        ),
                        setup_state="configured",
                    ),
                ),
            )
        return snap

    def fail_record(*args, **kwargs):
        raise StateStoreError("write failed")

    monkeypatch.setattr(state.inspector, "inspect", inspect)
    monkeypatch.setattr(state.store, "record_successful_apply", fail_record)
    # Keep the simulated kernel link present after recovery cleanup so this
    # test exercises the fail-closed verification path rather than attempting
    # a real host netlink delete.
    monkeypatch.setattr(
        networkd.NativeNetworkdOperations,
        "remove_recovery_identities",
        lambda *_: None,
    )
    with pytest.raises(StateStoreError) as caught:
        state.apply()
    assert "retired managed virtual device remains: vlan30" in " ".join(
        caught.value.__notes__
    )
    assert not (state.paths.networkd_path / "10-kavernis-vlan.netdev").exists()


def test_network_lock_remains_held_during_verification(tmp_path, monkeypatch, native):
    from kavernis.state.lock import LockUnavailableError, network_lock

    state = service(tmp_path)
    write_desired(state.paths.interfaces_path.parent)
    original = core.verify_runtime
    verified = []

    def verify(*args, **kwargs):
        with pytest.raises(LockUnavailableError), network_lock(state.paths.lock_path):
            pytest.fail("lock released before verification")
        verified.append(True)
        original(*args, **kwargs)

    monkeypatch.setattr(core, "verify_runtime", verify)
    state.apply()
    assert verified == [True]


def test_cli_reports_interruption_notes_without_claiming_unchanged_desired(
    tmp_path, monkeypatch, capsys
):
    from kavernis import cli

    def cancel(*args):
        error = KeyboardInterrupt()
        error.add_note(
            "rollback desired state already changed; runtime recovery failed"
        )
        raise error

    monkeypatch.setattr(core.NetworkStateService, "rollback", cancel)
    assert cli.main(["rollback", "network", "a" * 40]) == 1
    output = capsys.readouterr().err
    assert "Interrupted" in output
    assert "desired state already changed" in output
    assert "runtime recovery failed" in output
    assert "unchanged" not in output


def test_post_commit_interruption_keeps_verified_native_state(
    tmp_path, monkeypatch, native
):
    state = service(tmp_path)
    write_desired(state.paths.interfaces_path.parent)
    state.apply()
    previous = state.store.get_domain_state("network")
    write_desired(state.paths.interfaces_path.parent, "192.168.20.1/24")
    record = state.store.record_successful_apply
    interruption = KeyboardInterrupt()

    def commit_then_interrupt(*args, **kwargs):
        record(*args, **kwargs)
        raise interruption

    monkeypatch.setattr(state.store, "record_successful_apply", commit_then_interrupt)
    with pytest.raises(KeyboardInterrupt) as caught:
        state.apply()
    assert caught.value is interruption
    assert "after verified success was durably recorded" in " ".join(
        interruption.__notes__
    )
    assert "192.168.20.1/24" in next(iter(artifacts(state).values())).decode()
    assert state.store.get_domain_state("network").revision != previous.revision
    assert len(native) == 2  # No compensating reload after acknowledged commit.
    assert (
        state.store.get_last_apply_outcome("network").kind is ApplyOutcomeKind.SUCCESS
    )
    with sqlite3.connect(state.paths.database_path) as db:
        assert db.execute("SELECT count(*) FROM apply_failure").fetchone() == (0,)


def test_uncertain_commit_reports_manual_inspection(tmp_path, monkeypatch, native):
    state = service(tmp_path)
    write_desired(state.paths.interfaces_path.parent)
    state.apply()
    previous = state.store.get_domain_state("network")

    class AmbiguousCommit(sqlite3.Connection):
        def commit(self):
            raise sqlite3.OperationalError("commit acknowledgement lost")

    monkeypatch.setattr(
        state.store,
        "_connect",
        lambda: sqlite3.connect(state.paths.database_path, factory=AmbiguousCommit),
    )
    with pytest.raises(StateStoreError, match="acknowledgement") as caught:
        state.apply()
    assert "durable recording outcome is uncertain" in " ".join(caught.value.__notes__)
    assert state.store.get_domain_state("network") == previous
    assert len(native) == 2
