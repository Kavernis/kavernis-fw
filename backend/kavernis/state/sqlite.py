"""Small explicit SQLite store for Kavernis applied-state metadata."""

import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from kavernis.state.models import (
    AppliedDomainState,
    ApplyOperationKind,
    ApplyOutcome,
    ApplyOutcomeKind,
)

SCHEMA_VERSION = 2


class StateStoreError(RuntimeError):
    """Raised when operational state cannot be safely read or recorded."""


class StateSchemaError(StateStoreError):
    """Raised for an unsupported on-disk state schema."""


class SQLiteStateStore:
    """SQLite implementation retaining only operational metadata."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def get_domain_state(self, domain: str) -> AppliedDomainState | None:
        self._initialize()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN")
                row = connection.execute(
                    "SELECT revision, applied_at FROM domain_state WHERE domain = ?",
                    (domain,),
                ).fetchone()
                if row is None:
                    connection.rollback()
                    return None
                self._after_domain_state_row_read()
                artifacts = connection.execute(
                    "SELECT filename, sha256 FROM artifact_hash "
                    "WHERE domain = ? ORDER BY filename",
                    (domain,),
                ).fetchall()
                connection.rollback()
        except sqlite3.Error as error:
            raise StateStoreError(f"cannot read Kavernis state: {error}") from error
        return AppliedDomainState(
            domain=domain,
            revision=str(row[0]),
            applied_at=datetime.fromisoformat(str(row[1])),
            artifact_hashes={
                str(filename): str(sha256) for filename, sha256 in artifacts
            },
        )

    def get_last_apply_outcome(self, domain: str) -> ApplyOutcome | None:
        self._initialize()
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT operation, outcome, revision, recorded_at, message "
                    "FROM apply_operation WHERE domain = ? "
                    "ORDER BY id DESC LIMIT 1",
                    (domain,),
                ).fetchone()
        except sqlite3.Error as error:
            raise StateStoreError(
                f"cannot read Kavernis apply outcome: {error}"
            ) from error
        if row is None:
            return None
        return ApplyOutcome(
            operation=ApplyOperationKind(str(row[0])),
            kind=ApplyOutcomeKind(str(row[1])),
            revision=str(row[2]) if row[2] is not None else None,
            recorded_at=datetime.fromisoformat(str(row[3])),
            message=str(row[4]) if row[4] is not None else None,
        )

    def record_successful_apply(
        self,
        domain: str,
        revision: str,
        artifact_hashes: Mapping[str, str],
        operation: ApplyOperationKind = ApplyOperationKind.APPLY,
    ) -> None:
        self._initialize()
        applied_at = datetime.now(UTC).isoformat()
        try:
            with self._connect() as connection:
                with connection:
                    connection.execute(
                        "INSERT INTO domain_state(domain, revision, applied_at) "
                        "VALUES (?, ?, ?) "
                        "ON CONFLICT(domain) DO UPDATE SET "
                        "revision = excluded.revision, "
                        "applied_at = excluded.applied_at",
                        (domain, revision, applied_at),
                    )
                    connection.execute(
                        "DELETE FROM artifact_hash WHERE domain = ?", (domain,)
                    )
                    connection.executemany(
                        "INSERT INTO artifact_hash(domain, filename, sha256) "
                        "VALUES (?, ?, ?)",
                        [
                            (domain, filename, sha256)
                            for filename, sha256 in sorted(artifact_hashes.items())
                        ],
                    )
                    self._record_outcome(
                        connection,
                        domain,
                        operation,
                        ApplyOutcomeKind.SUCCESS,
                        revision,
                        None,
                        applied_at,
                    )
        except sqlite3.Error as error:
            raise StateStoreError(
                f"cannot record applied Kavernis state: {error}"
            ) from error

    def record_failed_apply(
        self,
        domain: str,
        revision: str,
        message: str,
        operation: ApplyOperationKind = ApplyOperationKind.APPLY,
    ) -> None:
        self._initialize()
        failed_at = datetime.now(UTC).isoformat()
        try:
            with self._connect() as connection:
                with connection:
                    connection.execute(
                        "INSERT INTO apply_failure(domain, revision, failed_at, "
                        "message) "
                        "VALUES (?, ?, ?, ?)",
                        (domain, revision, failed_at, message),
                    )
                    self._record_outcome(
                        connection,
                        domain,
                        operation,
                        ApplyOutcomeKind.FAILURE,
                        revision,
                        message,
                        failed_at,
                    )
        except sqlite3.Error as error:
            raise StateStoreError(
                f"cannot record failed Kavernis apply: {error}"
            ) from error

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.database_path)

    def _after_domain_state_row_read(self) -> None:
        """Test seam between the two reads in one SQLite snapshot transaction."""

    @staticmethod
    def _record_outcome(
        connection: sqlite3.Connection,
        domain: str,
        operation: ApplyOperationKind,
        outcome: ApplyOutcomeKind,
        revision: str | None,
        message: str | None,
        recorded_at: str,
    ) -> None:
        connection.execute(
            "INSERT INTO apply_operation("
            "domain, operation, outcome, revision, recorded_at, message) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (domain, operation.value, outcome.value, revision, recorded_at, message),
        )

    def _initialize(self) -> None:
        try:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as connection:
                with connection:
                    connection.execute(
                        "CREATE TABLE IF NOT EXISTS schema_version "
                        "(version INTEGER NOT NULL)"
                    )
                    row = connection.execute(
                        "SELECT version FROM schema_version"
                    ).fetchone()
                    if row is None:
                        connection.execute(
                            "INSERT INTO schema_version(version) VALUES (?)",
                            (SCHEMA_VERSION,),
                        )
                        self._create_schema(connection)
                    elif int(row[0]) == 1:
                        self._migrate_v1_to_v2(connection)
                    elif int(row[0]) != SCHEMA_VERSION:
                        raise StateSchemaError(
                            "unsupported Kavernis state schema version "
                            f"{row[0]} (expected {SCHEMA_VERSION})"
                        )
        except StateSchemaError:
            raise
        except sqlite3.Error as error:
            raise StateStoreError(
                f"cannot initialize Kavernis state: {error}"
            ) from error

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            "CREATE TABLE domain_state ("
            "domain TEXT PRIMARY KEY, revision TEXT NOT NULL, applied_at TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE artifact_hash ("
            "domain TEXT NOT NULL, filename TEXT NOT NULL, sha256 TEXT NOT NULL, "
            "PRIMARY KEY(domain, filename), "
            "FOREIGN KEY(domain) REFERENCES domain_state(domain))"
        )
        connection.execute(
            "CREATE TABLE apply_failure ("
            "id INTEGER PRIMARY KEY, domain TEXT NOT NULL, revision TEXT NOT NULL, "
            "failed_at TEXT NOT NULL, message TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE apply_operation ("
            "id INTEGER PRIMARY KEY, domain TEXT NOT NULL, operation TEXT NOT NULL, "
            "outcome TEXT NOT NULL, revision TEXT, recorded_at TEXT NOT NULL, "
            "message TEXT)"
        )

    @staticmethod
    def _migrate_v1_to_v2(connection: sqlite3.Connection) -> None:
        """Add typed operation outcomes without altering successful state."""
        connection.execute(
            "CREATE TABLE apply_operation ("
            "id INTEGER PRIMARY KEY, domain TEXT NOT NULL, operation TEXT NOT NULL, "
            "outcome TEXT NOT NULL, revision TEXT, recorded_at TEXT NOT NULL, "
            "message TEXT)"
        )
        connection.execute(
            "INSERT INTO apply_operation("
            "domain, operation, outcome, revision, recorded_at, message) "
            "SELECT domain, 'apply', 'failure', revision, failed_at, message "
            "FROM apply_failure ORDER BY id"
        )
        connection.execute(
            "UPDATE schema_version SET version = ?", (SCHEMA_VERSION,)
        )
