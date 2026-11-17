"""Explicit success-write phases survive exceptions and cancellation."""

import sqlite3
import subprocess

import pytest
from kavernis.state.models import ApplyCommit, ApplyOutcomeKind, CommitPhase
from kavernis.state.sqlite import SQLiteStateStore, StateStoreError
from network_helpers import service, write_desired
from test_network_safety import artifacts  # noqa: F401


@pytest.fixture
def native(monkeypatch):
    original = subprocess.run
    calls = []

    def run(args, **kwargs):
        if args[0] == "networkctl":
            calls.append(args)
            return subprocess.CompletedProcess(args, 0)
        return original(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    return calls


@pytest.mark.parametrize("phase", list(CommitPhase))
@pytest.mark.parametrize(
    "cause", [KeyboardInterrupt, SystemExit, sqlite3.OperationalError]
)
def test_write_receipt_tracks_actual_transaction_lifecycle(
    tmp_path, monkeypatch, phase, cause
):
    store = SQLiteStateStore(tmp_path / "state.db")
    store.record_successful_apply("network", "a" * 40, {})
    old = store.get_domain_state("network")
    receipt = ApplyCommit()
    error = cause("injected")

    def fail():
        raise error

    if phase is CommitPhase.BEFORE_TRANSACTION:
        monkeypatch.setattr(store, "_initialize", fail)
    elif phase is CommitPhase.TRANSACTION_STARTED:
        monkeypatch.setattr(store, "_before_success_commit", fail)
    elif phase is CommitPhase.COMMIT_ATTEMPTED:

        class FailingConnection(sqlite3.Connection):
            def commit(self):
                raise error

        monkeypatch.setattr(
            store,
            "_connect",
            lambda: sqlite3.connect(store.database_path, factory=FailingConnection),
        )
    else:
        monkeypatch.setattr(store, "_after_success_commit", fail)
    expected = StateStoreError if cause is sqlite3.OperationalError else cause
    with pytest.raises(expected) as caught:
        store.record_successful_apply("network", "b" * 40, {}, commit=receipt)
    assert receipt.phase is phase
    if cause is not sqlite3.OperationalError:
        assert caught.value is error
    reader = SQLiteStateStore(store.database_path)
    state = reader.get_domain_state("network")
    if phase is CommitPhase.COMMITTED:
        assert state.revision == "b" * 40
        assert reader.get_last_apply_outcome("network").kind is ApplyOutcomeKind.SUCCESS
    else:
        assert state == old


@pytest.mark.parametrize("before_transaction", [True, False])
def test_definite_failure_recovers_even_when_state_reads_fail(
    tmp_path, monkeypatch, native, before_transaction
):
    state = service(tmp_path)
    write_desired(state.paths.interfaces_path.parent)
    state.apply()
    old = state.store.get_domain_state("network")
    old_artifacts = artifacts(state)
    write_desired(state.paths.interfaces_path.parent, "192.168.20.1/24")
    original_record = state.store.record_successful_apply

    def fail(*args, **kwargs):
        raise StateStoreError("database unavailable")

    def record(*args, **kwargs):
        monkeypatch.setattr(state.store, "get_domain_state", fail)
        monkeypatch.setattr(state.store, "get_last_apply_outcome", fail)
        hook = "_initialize" if before_transaction else "_before_success_commit"
        monkeypatch.setattr(state.store, hook, fail)
        original_record(*args, **kwargs)

    monkeypatch.setattr(state.store, "record_successful_apply", record)
    with pytest.raises(StateStoreError) as caught:
        state.apply()
    assert artifacts(state) == old_artifacts
    assert (
        SQLiteStateStore(state.paths.database_path).get_domain_state("network") == old
    )
    assert "runtime restoration verified" in " ".join(caught.value.__notes__)
    assert "outcome is uncertain" not in " ".join(caught.value.__notes__)
    assert len(native) == 3


def test_commit_exception_after_durable_write_is_uncertain_not_reverted(
    tmp_path, monkeypatch, native
):
    state = service(tmp_path)
    write_desired(state.paths.interfaces_path.parent)
    state.apply()
    write_desired(state.paths.interfaces_path.parent, "192.168.20.1/24")

    class LostAcknowledgement(sqlite3.Connection):
        def commit(self):
            super().commit()
            raise KeyboardInterrupt("acknowledgement lost")

    monkeypatch.setattr(
        state.store,
        "_connect",
        lambda: sqlite3.connect(state.paths.database_path, factory=LostAcknowledgement),
    )
    with pytest.raises(KeyboardInterrupt) as caught:
        state.apply()
    assert "outcome is uncertain" in " ".join(caught.value.__notes__)
    assert "192.168.20.1/24" in next(iter(artifacts(state).values())).decode()
    assert (
        state.store.get_last_apply_outcome("network").kind is ApplyOutcomeKind.SUCCESS
    )
    assert len(native) == 2
