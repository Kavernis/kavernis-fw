import sqlite3
from pathlib import Path

import pytest
from kavernis.state.sqlite import SCHEMA_VERSION, SQLiteStateStore


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
