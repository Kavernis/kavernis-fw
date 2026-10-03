import sqlite3
from pathlib import Path

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
