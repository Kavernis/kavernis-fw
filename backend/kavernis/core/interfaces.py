"""Plan, apply, audit, and recover interface desired state."""

import os
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from kavernis.backends.networkd import (
    NetworkdApplyError,
    NetworkdConfiguration,
    apply,
    artifact_hashes,
    generate,
    inspect_artifact_drift,
)
from kavernis.config.interfaces import InterfacesConfig
from kavernis.config.loader import load_interfaces_contents
from kavernis.config.resolver import resolve_interfaces
from kavernis.state.history import DesiredStateHistory
from kavernis.state.lock import state_lock
from kavernis.state.models import ArtifactDrift, HistoryRevision
from kavernis.state.sqlite import SQLiteStateStore
from kavernis.state.store import StateStore

INTERFACES_DOMAIN = "interfaces"


class InterfacesStatusKind(StrEnum):
    """Kavernis-level relationship among desired, applied, and artifacts."""

    IN_SYNC = "in sync"
    PENDING_CHANGES = "pending changes"
    DRIFT_DETECTED = "drift detected"
    NOT_YET_APPLIED = "not yet applied"


@dataclass(frozen=True)
class InterfacesStatus:
    """Evaluated status, intentionally independent of CLI formatting."""

    kind: InterfacesStatusKind
    desired_revision: str | None
    applied_revision: str | None
    drift: tuple[ArtifactDrift, ...]


@dataclass(frozen=True)
class InterfacesStatePaths:
    """Injectable production and test locations for the interfaces slice."""

    desired_path: Path = Path("/etc/kavernis/interfaces.yaml")
    history_path: Path = Path("/var/lib/kavernis/history")
    database_path: Path = Path("/var/lib/kavernis/state.db")
    networkd_path: Path = Path("/etc/systemd/network")
    lock_path: Path = Path("/var/lib/kavernis/state.lock")


class InterfacesStateService:
    """Coordinates desired history, safe native apply, and applied metadata."""

    def __init__(
        self,
        paths: InterfacesStatePaths = InterfacesStatePaths(),
        history: DesiredStateHistory | None = None,
        store: StateStore | None = None,
    ) -> None:
        self.paths = paths
        self.history = history or DesiredStateHistory(paths.history_path)
        self.store = store or SQLiteStateStore(paths.database_path)

    def apply(
        self, config: InterfacesConfig, desired_contents: bytes
    ) -> NetworkdConfiguration:
        """Apply then atomically advance the successful applied-state record."""
        candidate = plan_interfaces(config)
        with state_lock(self.paths.lock_path):
            revision = self.history.snapshot(
                {"interfaces.yaml": desired_contents}, "apply interfaces desired state"
            )
            try:
                apply(candidate, self.paths.networkd_path)
            except NetworkdApplyError as error:
                self.store.record_failed_apply(INTERFACES_DOMAIN, revision, str(error))
                raise
            self.store.record_successful_apply(
                INTERFACES_DOMAIN, revision, artifact_hashes(candidate)
            )
        return candidate

    def status(self, desired_contents: bytes) -> InterfacesStatus:
        """Classify desired/applied/artifact state without changing it."""
        desired_revision = self.history.revision_for_contents(
            {"interfaces.yaml": desired_contents}
        )
        applied = self.store.get_domain_state(INTERFACES_DOMAIN)
        if applied is None:
            return InterfacesStatus(
                InterfacesStatusKind.NOT_YET_APPLIED, desired_revision, None, ()
            )
        desired_matches_applied = desired_revision == applied.revision
        if desired_revision is None:
            desired_matches_applied = (
                self.history.read_revision(applied.revision, "interfaces.yaml")
                == desired_contents
            )
        if not desired_matches_applied:
            return InterfacesStatus(
                InterfacesStatusKind.PENDING_CHANGES,
                desired_revision,
                applied.revision,
                (),
            )
        drift = inspect_artifact_drift(
            applied.artifact_hashes, self.paths.networkd_path
        )
        return InterfacesStatus(
            InterfacesStatusKind.DRIFT_DETECTED
            if drift
            else InterfacesStatusKind.IN_SYNC,
            desired_revision,
            applied.revision,
            drift,
        )

    def history_revisions(self) -> list[HistoryRevision]:
        """List desired-state history newest first."""
        return self.history.list_revisions()

    def diff(self, desired_contents: bytes, revision: str) -> str:
        """Return a deterministic desired-YAML unified diff against a revision."""
        import difflib

        previous = self.history.read_revision(revision, "interfaces.yaml").decode(
            "utf-8"
        )
        current = desired_contents.decode("utf-8")
        return "".join(
            difflib.unified_diff(
                previous.splitlines(keepends=True),
                current.splitlines(keepends=True),
                fromfile=f"interfaces.yaml@{revision[:12]}",
                tofile="interfaces.yaml (current)",
            )
        )

    def rollback(self, revision: str) -> NetworkdConfiguration:
        """Restore historical desired YAML, then use the normal safe apply path.

        If native apply fails, restored YAML remains current and its snapshot remains
        auditable; the older successful SQLite applied revision is deliberately kept.
        """
        with state_lock(self.paths.lock_path):
            contents = self.history.read_revision(revision, "interfaces.yaml")
            config = load_interfaces_contents(
                contents.decode("utf-8"), f"revision {revision} interfaces.yaml"
            )
            candidate = plan_interfaces(config)
            _write_desired_atomically(self.paths.desired_path, contents)
            new_revision = self.history.snapshot(
                {"interfaces.yaml": contents}, f"rollback interfaces to {revision[:12]}"
            )
            try:
                apply(candidate, self.paths.networkd_path)
            except NetworkdApplyError as error:
                self.store.record_failed_apply(
                    INTERFACES_DOMAIN, new_revision, str(error)
                )
                raise
            self.store.record_successful_apply(
                INTERFACES_DOMAIN, new_revision, artifact_hashes(candidate)
            )
        return candidate


def plan_interfaces(config: InterfacesConfig) -> NetworkdConfiguration:
    """Resolve validated desired state and generate a validated candidate."""
    return generate(resolve_interfaces(config))


def apply_interfaces(
    config: InterfacesConfig,
    service: InterfacesStateService | None = None,
    desired_contents: bytes | None = None,
) -> NetworkdConfiguration:
    """Apply interfaces with injectable state dependencies for integrations/tests."""
    state_service = service or InterfacesStateService()
    contents = desired_contents or state_service.paths.desired_path.read_bytes()
    return state_service.apply(config, contents)


def _write_desired_atomically(path: Path, contents: bytes) -> None:
    """Replace only the known desired-state path without partial YAML exposure."""
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    descriptor, temporary_name = tempfile.mkstemp(prefix=".kavernis-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as temporary:
            temporary.write(contents)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_name, mode)
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise
