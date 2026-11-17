"""Small explicit SQLite store for Kavernis applied-state metadata."""

import sqlite3
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from kavernis.state.models import (
    AppliedDomainState,
    ApplyCommit,
    ApplyOperationKind,
    ApplyOutcome,
    ApplyOutcomeKind,
    CommitPhase,
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

    def preflight(self, domain: str) -> None:
        """Check an existing store without migration; initialize only a new store.

        This acquires and releases a SQLite write reservation, not a transaction
        spanning Linux activation. Later writes can still fail.
        """
        if not self.database_path.exists():
            self._initialize()
        required = {
            "schema_version": "version",
            "domain_state": "domain, revision, applied_at",
            "artifact_hash": "domain, filename, sha256",
            "apply_failure": "id, domain, revision, failed_at, message",
            "apply_operation": (
                "id, domain, operation, outcome, revision, recorded_at, message"
            ),
        }
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                versions = connection.execute(
                    "SELECT version FROM schema_version"
                ).fetchall()
                if versions != [(SCHEMA_VERSION,)]:
                    raise StateSchemaError(
                        f"unsupported Kavernis state schema {versions}; "
                        f"expected version {SCHEMA_VERSION}; apply does not migrate"
                    )
                for table, columns in required.items():
                    # Identifiers are fixed internal constants, never user input.
                    connection.execute(f"SELECT {columns} FROM {table} LIMIT 0")
                constraints = {
                    "domain_state": (("domain",),),
                    "artifact_hash": (("domain", "filename"),),
                    "apply_failure": (("id",),),
                    "apply_operation": (("id",),),
                }
                for table, expected_keys in constraints.items():
                    actual_keys: set[tuple[str, ...]] = set()
                    for index in connection.execute(
                        f"PRAGMA index_list({table})"
                    ).fetchall():
                        if not index[2]:
                            continue
                        indexed_columns: tuple[str, ...] = tuple(
                            str(row[2])
                            for row in connection.execute(
                                f"PRAGMA index_info({index[1]})"
                            ).fetchall()
                        )
                        if columns:
                            actual_keys.add(indexed_columns)
                    primary_key = tuple(
                        name
                        for _, name in sorted(
                            (row[5], row[1])
                            for row in connection.execute(
                                f"PRAGMA table_info({table})"
                            ).fetchall()
                            if row[5]
                        )
                    )
                    if primary_key:
                        actual_keys.add(primary_key)
                    if not set(expected_keys) <= actual_keys:
                        raise StateSchemaError(
                            f"incompatible {table} key constraints: "
                            f"expected {expected_keys}, observed {sorted(actual_keys)}"
                        )
                if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                    raise StateStoreError("Kavernis state integrity check failed")
                if connection.execute("PRAGMA foreign_key_check").fetchall():
                    raise StateStoreError("Kavernis state foreign-key check failed")
                connection.rollback()
            self.get_domain_state(domain)
            self.get_last_apply_outcome(domain)
        except (sqlite3.Error, ValueError, OSError) as error:
            raise StateStoreError(
                f"Kavernis state preflight failed: {error}"
            ) from error

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
        *,
        commit: ApplyCommit | None = None,
    ) -> None:
        receipt = commit if commit is not None else ApplyCommit()
        if receipt.phase is not CommitPhase.BEFORE_TRANSACTION:
            raise ValueError("successful-apply receipt must not be reused")
        connection: sqlite3.Connection | None = None
        try:
            self._initialize()
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            receipt.phase = CommitPhase.TRANSACTION_STARTED
            self._write_success(
                connection, domain, revision, artifact_hashes, operation
            )
            self._before_success_commit()
            receipt.phase = CommitPhase.COMMIT_ATTEMPTED
            connection.commit()
            receipt.phase = CommitPhase.COMMITTED
            self._after_success_commit()
        except BaseException as error:
            if connection is not None and receipt.phase is not CommitPhase.COMMITTED:
                try:
                    connection.rollback()
                except BaseException as rollback_error:
                    error.add_note(f"SQLite rollback failed: {rollback_error}")
            if isinstance(error, sqlite3.Error):
                raise StateStoreError(
                    f"cannot record applied Kavernis state: {error}"
                ) from error
            raise
        finally:
            if connection is not None:
                original = sys.exception()
                try:
                    connection.close()
                except BaseException as close_error:
                    if original is None:
                        raise
                    original.add_note(f"SQLite close failed: {close_error}")

    def _before_success_commit(self) -> None:
        """Failure-injection seam after writes, before commit is attempted."""

    def _after_success_commit(self) -> None:
        """Failure-injection seam after the durable success is acknowledged."""

    def _write_success(
        self,
        connection: sqlite3.Connection,
        domain: str,
        revision: str,
        artifact_hashes: Mapping[str, str],
        operation: ApplyOperationKind,
    ) -> None:
        applied_at = datetime.now(UTC).isoformat()
        connection.execute(
            "INSERT INTO domain_state(domain, revision, applied_at) VALUES (?, ?, ?) "
            "ON CONFLICT(domain) DO UPDATE SET revision = excluded.revision, "
            "applied_at = excluded.applied_at",
            (domain, revision, applied_at),
        )
        connection.execute("DELETE FROM artifact_hash WHERE domain = ?", (domain,))
        connection.executemany(
            "INSERT INTO artifact_hash(domain, filename, sha256) VALUES (?, ?, ?)",
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

    def record_failed_apply(
        self,
        domain: str,
        revision: str | None,
        message: str,
        operation: ApplyOperationKind = ApplyOperationKind.APPLY,
    ) -> None:
        self._initialize()
        # The legacy failure table requires a string; the typed operation row
        # records NULL when preflight failed before identifying a revision.
        failed_at = datetime.now(UTC).isoformat()
        try:
            with self._connect() as connection:
                with connection:
                    connection.execute(
                        "INSERT INTO apply_failure(domain, revision, failed_at, "
                        "message) "
                        "VALUES (?, ?, ?, ?)",
                        (domain, revision or "", failed_at, message),
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
        connection.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
