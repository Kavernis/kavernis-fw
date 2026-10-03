"""Plan, apply, audit, and recover complete network desired state."""

import difflib
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
from kavernis.config.gateways import GatewaysConfig
from kavernis.config.interfaces import InterfacesConfig
from kavernis.config.loader import (
    load_gateways_contents,
    load_interfaces_contents,
    load_routes_contents,
)
from kavernis.config.resolver import resolve_network
from kavernis.config.routes import RoutesConfig
from kavernis.state.history import DesiredStateHistory
from kavernis.state.lock import state_lock
from kavernis.state.models import ArtifactDrift, HistoryRevision
from kavernis.state.sqlite import SQLiteStateStore
from kavernis.state.store import StateStore

NETWORK_DOMAIN = "network"
DESIRED_FILENAMES = ("interfaces.yaml", "gateways.yaml", "routes.yaml")


class NetworkStatusKind(StrEnum):
    IN_SYNC = "in sync"
    PENDING_CHANGES = "pending changes"
    DRIFT_DETECTED = "drift detected"
    NOT_YET_APPLIED = "not yet applied"


@dataclass(frozen=True)
class NetworkStatus:
    kind: NetworkStatusKind
    desired_revision: str | None
    applied_revision: str | None
    drift: tuple[ArtifactDrift, ...]


@dataclass(frozen=True)
class NetworkDesiredState:
    """Complete desired input for one atomic network transaction."""

    interfaces: InterfacesConfig
    gateways: GatewaysConfig
    routes: RoutesConfig
    files: dict[str, bytes]


@dataclass(frozen=True)
class NetworkStatePaths:
    interfaces_path: Path = Path("/etc/kavernis/interfaces.yaml")
    gateways_path: Path = Path("/etc/kavernis/gateways.yaml")
    routes_path: Path = Path("/etc/kavernis/routes.yaml")
    history_path: Path = Path("/var/lib/kavernis/history")
    database_path: Path = Path("/var/lib/kavernis/state.db")
    networkd_path: Path = Path("/etc/systemd/network")
    lock_path: Path = Path("/var/lib/kavernis/state.lock")


class NetworkStateService:
    """Coordinate history, native apply, and applied metadata for network."""

    def __init__(
        self,
        paths: NetworkStatePaths = NetworkStatePaths(),
        history: DesiredStateHistory | None = None,
        store: StateStore | None = None,
    ) -> None:
        self.paths = paths
        self.history = history or DesiredStateHistory(paths.history_path)
        self.store = store or SQLiteStateStore(paths.database_path)

    def apply(self, desired: NetworkDesiredState) -> NetworkdConfiguration:
        candidate = plan_network(desired)
        with state_lock(self.paths.lock_path):
            revision = self.history.snapshot(
                desired.files, "apply network desired state"
            )
            try:
                apply(candidate, self.paths.networkd_path)
            except NetworkdApplyError as error:
                self.store.record_failed_apply(NETWORK_DOMAIN, revision, str(error))
                raise
            self.store.record_successful_apply(
                NETWORK_DOMAIN, revision, artifact_hashes(candidate)
            )
        return candidate

    def status(self, files: dict[str, bytes]) -> NetworkStatus:
        desired_revision = self.history.revision_for_contents(files)
        applied = self.store.get_domain_state(NETWORK_DOMAIN)
        if applied is None:
            return NetworkStatus(
                NetworkStatusKind.NOT_YET_APPLIED, desired_revision, None, ()
            )
        if desired_revision != applied.revision:
            matches = desired_revision is None and all(
                self.history.read_revision(applied.revision, filename) == contents
                for filename, contents in files.items()
            )
            if not matches:
                return NetworkStatus(
                    NetworkStatusKind.PENDING_CHANGES,
                    desired_revision,
                    applied.revision,
                    (),
                )
            desired_revision = applied.revision
        drift = inspect_artifact_drift(
            applied.artifact_hashes, self.paths.networkd_path
        )
        return NetworkStatus(
            NetworkStatusKind.DRIFT_DETECTED if drift else NetworkStatusKind.IN_SYNC,
            desired_revision,
            applied.revision,
            drift,
        )

    def history_revisions(self) -> list[HistoryRevision]:
        return self.history.list_revisions()

    def diff(self, files: dict[str, bytes], revision: str) -> str:
        return "".join(
            "".join(
                difflib.unified_diff(
                    self.history.read_revision(revision, filename)
                    .decode()
                    .splitlines(keepends=True),
                    contents.decode().splitlines(keepends=True),
                    fromfile=f"{filename}@{revision[:12]}",
                    tofile=f"{filename} (current)",
                )
            )
            for filename, contents in sorted(files.items())
        )

    def rollback(self, revision: str) -> NetworkdConfiguration:
        with state_lock(self.paths.lock_path):
            files = {
                filename: self.history.read_revision(revision, filename)
                for filename in DESIRED_FILENAMES
            }
            desired = _desired_from_files(files, f"revision {revision}")
            candidate = plan_network(desired)
            for path, filename in (
                (self.paths.interfaces_path, "interfaces.yaml"),
                (self.paths.gateways_path, "gateways.yaml"),
                (self.paths.routes_path, "routes.yaml"),
            ):
                _write_desired_atomically(path, files[filename])
            new_revision = self.history.snapshot(
                files, f"rollback network to {revision[:12]}"
            )
            try:
                apply(candidate, self.paths.networkd_path)
            except NetworkdApplyError as error:
                self.store.record_failed_apply(NETWORK_DOMAIN, new_revision, str(error))
                raise
            self.store.record_successful_apply(
                NETWORK_DOMAIN, new_revision, artifact_hashes(candidate)
            )
        return candidate


def load_network(paths: NetworkStatePaths) -> NetworkDesiredState:
    """Load all required network desired-state files without side effects."""
    files = {
        "interfaces.yaml": paths.interfaces_path.read_bytes(),
        "gateways.yaml": paths.gateways_path.read_bytes(),
        "routes.yaml": paths.routes_path.read_bytes(),
    }
    return _desired_from_files(files, "network configuration")


def plan_network(desired: NetworkDesiredState) -> NetworkdConfiguration:
    """Resolve complete network intent and generate a validated candidate."""
    return generate(
        resolve_network(desired.interfaces, desired.gateways, desired.routes)
    )


def _desired_from_files(files: dict[str, bytes], source: str) -> NetworkDesiredState:
    return NetworkDesiredState(
        interfaces=load_interfaces_contents(files["interfaces.yaml"].decode(), source),
        gateways=load_gateways_contents(files["gateways.yaml"].decode(), source),
        routes=load_routes_contents(files["routes.yaml"].decode(), source),
        files=files,
    )


def _write_desired_atomically(path: Path, contents: bytes) -> None:
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
