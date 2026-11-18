import sqlite3
import threading
from pathlib import Path

import pytest
from kavernis.state.sqlite import SCHEMA_VERSION, SQLiteStateStore, StateStoreError


def test_sqlite_records_success_and_preserves_it_after_failure(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    store = SQLiteStateStore(database)

    assert store.get_domain_state("interfaces") is None
    store.record_successful_apply(
        "interfaces", "a" * 40, {"10-kavernis-lan.network": "hash-a"}
    )
    store.record_failed_apply("interfaces", "b" * 40, "networkctl failed")

    state = store.get_domain_state("interfaces")
    assert state is not None
    assert state.revision == "a" * 40
    assert dict(state.artifact_hashes) == {"10-kavernis-lan.network": "hash-a"}

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchone() == (
            SCHEMA_VERSION,
        )
        assert connection.execute("SELECT count(*) FROM apply_failure").fetchone() == (
            1,
        )


def test_corrupt_database_fails_loudly(tmp_path):
    from kavernis.state.sqlite import StateStoreError

    path = tmp_path / "state.db"
    path.write_bytes(b"not sqlite")
    with pytest.raises(StateStoreError):
        SQLiteStateStore(path).get_domain_state("network")
    assert path.read_bytes() == b"not sqlite"


def test_incompatible_database_version_is_not_reset(tmp_path):
    import sqlite3

    from kavernis.state.sqlite import StateSchemaError

    path = tmp_path / "state.db"
    store = SQLiteStateStore(path)
    store.record_successful_apply("network", "a" * 40, {})
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE schema_version SET version = 999")
    with pytest.raises(StateSchemaError, match="unsupported"):
        store.get_domain_state("network")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT revision FROM domain_state").fetchone() == (
            "a" * 40,
        )


def test_v1_database_is_migrated_without_losing_failed_apply_history(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        connection.execute("INSERT INTO schema_version VALUES (1)")
        connection.execute(
            "CREATE TABLE domain_state ("
            "domain TEXT PRIMARY KEY, revision TEXT NOT NULL, applied_at TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE artifact_hash ("
            "domain TEXT NOT NULL, filename TEXT NOT NULL, sha256 TEXT NOT NULL, "
            "PRIMARY KEY(domain, filename))"
        )
        connection.execute(
            "CREATE TABLE apply_failure ("
            "id INTEGER PRIMARY KEY, domain TEXT NOT NULL, revision TEXT NOT NULL, "
            "failed_at TEXT NOT NULL, message TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO apply_failure VALUES ("
            "1, 'network', 'b', '2026-01-01T00:00:00+00:00', 'failed')"
        )

    outcome = SQLiteStateStore(path).get_last_apply_outcome("network")
    assert outcome is not None
    assert outcome.operation.value == "apply"
    assert outcome.kind.value == "failure"
    assert outcome.message == "failed"
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchone() == (
            SCHEMA_VERSION,
        )


def test_last_operation_outcome_is_separate_from_successful_applied_state(
    tmp_path: Path,
) -> None:
    store = SQLiteStateStore(tmp_path / "state.db")
    store.record_successful_apply("network", "a" * 40, {"a.network": "hash-a"})
    store.record_failed_apply("network", "b" * 40, "reload failed")

    state = store.get_domain_state("network")
    outcome = store.get_last_apply_outcome("network")
    assert state is not None and state.revision == "a" * 40
    assert outcome is not None
    assert outcome.revision == "b" * 40
    assert outcome.message == "reload failed"


def test_domain_state_read_uses_one_consistent_sqlite_snapshot(tmp_path: Path) -> None:
    writer_started = threading.Event()
    writer_finished = threading.Event()
    writer: list[threading.Thread] = []

    class CoordinatedStore(SQLiteStateStore):
        def _after_domain_state_row_read(self) -> None:
            if writer:
                return

            def write() -> None:
                writer_started.set()
                self.record_successful_apply(
                    "network", "b" * 40, {"b.network": "hash-b"}
                )
                writer_finished.set()

            thread = threading.Thread(target=write)
            thread.start()
            assert writer_started.wait(timeout=2)
            writer.append(thread)

    store = CoordinatedStore(tmp_path / "state.db")
    store.record_successful_apply("network", "a" * 40, {"a.network": "hash-a"})
    snapshot = store.get_domain_state("network")
    writer[0].join(timeout=2)
    assert writer_finished.is_set()
    assert snapshot is not None
    assert snapshot.revision == "a" * 40
    assert dict(snapshot.artifact_hashes) == {"a.network": "hash-a"}
    current = store.get_domain_state("network")
    assert current is not None
    assert current.revision == "b" * 40


def test_database_read_errors_are_wrapped(tmp_path: Path, monkeypatch) -> None:
    store = SQLiteStateStore(tmp_path / "state.db")
    store.record_successful_apply("network", "a" * 40, {})

    def fail_read() -> None:
        raise sqlite3.DatabaseError("read failed")

    monkeypatch.setattr(store, "_after_domain_state_row_read", fail_read)
    with pytest.raises(StateStoreError, match="cannot read"):
        store.get_domain_state("network")


def test_preflight_initializes_new_store_and_supports_reads(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")
    store.preflight("network")
    assert store.get_domain_state("network") is None
    assert store.get_last_apply_outcome("network") is None
    store.record_successful_apply("network", "a" * 40, {})
    store.preflight("network")
    assert store.get_domain_state("network").revision == "a" * 40


@pytest.mark.parametrize("version", [1, 999])
def test_preflight_never_migrates_existing_schema(tmp_path, version):
    store = SQLiteStateStore(tmp_path / "state.db")
    store.preflight("network")
    with sqlite3.connect(store.database_path) as db:
        db.execute("UPDATE schema_version SET version=?", (version,))
    with pytest.raises(StateStoreError, match="does not migrate"):
        store.preflight("network")
    with sqlite3.connect(store.database_path) as db:
        assert db.execute("SELECT version FROM schema_version").fetchall() == [
            (version,)
        ]


@pytest.mark.parametrize(
    "table",
    [
        "domain_state",
        "artifact_hash",
        "apply_failure",
        "apply_operation",
        "schema_version",
    ],
)
def test_preflight_detects_missing_required_tables_even_on_empty_store(tmp_path, table):
    store = SQLiteStateStore(tmp_path / "state.db")
    store.preflight("network")
    with sqlite3.connect(store.database_path) as db:
        db.execute(f"DROP TABLE {table}")
    with pytest.raises(StateStoreError):
        store.preflight("network")
    with sqlite3.connect(store.database_path) as db:
        assert not db.execute(
            "SELECT name FROM sqlite_master WHERE name=?", (table,)
        ).fetchall()


def test_preflight_rejects_corruption_and_duplicate_version_rows(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")
    store.preflight("network")
    with sqlite3.connect(store.database_path) as db:
        db.execute("INSERT INTO schema_version VALUES (?)", (SCHEMA_VERSION,))
    with pytest.raises(StateStoreError):
        store.preflight("network")
    store.database_path.write_bytes(b"not sqlite")
    with pytest.raises(StateStoreError):
        store.preflight("network")
    assert store.database_path.read_bytes() == b"not sqlite"


def test_preflight_checks_supported_reads(tmp_path, monkeypatch):
    store = SQLiteStateStore(tmp_path / "state.db")
    store.preflight("network")
    calls = []
    monkeypatch.setattr(
        store, "get_domain_state", lambda domain: calls.append(("state", domain))
    )
    monkeypatch.setattr(
        store,
        "get_last_apply_outcome",
        lambda domain: calls.append(("outcome", domain)),
    )
    store.preflight("network")
    assert calls == [("state", "network"), ("outcome", "network")]


def test_preflight_rejects_missing_upsert_key_constraint(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")
    store.preflight("network")
    with sqlite3.connect(store.database_path) as db:
        db.execute("DROP TABLE domain_state")
        db.execute(
            "CREATE TABLE domain_state (domain TEXT, revision TEXT NOT NULL, "
            "applied_at TEXT NOT NULL)"
        )
    with pytest.raises(StateStoreError, match="key constraints"):
        store.preflight("network")
