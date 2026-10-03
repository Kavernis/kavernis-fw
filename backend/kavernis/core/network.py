"""Plan, apply, audit, and recover complete network desired state."""

import difflib
import hashlib
import os
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
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
from kavernis.config.errors import ConfigurationError
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
from kavernis.state.lock import network_lock
from kavernis.state.models import ArtifactDrift, HistoryRevision
from kavernis.state.sqlite import SQLiteStateStore
from kavernis.state.store import StateStore

NETWORK_DOMAIN = "network"
DESIRED_FILENAMES = ("interfaces.yaml", "gateways.yaml", "routes.yaml")
EDITABLE_RESOURCES = ("interfaces", "gateways", "routes")


class DesiredStateChangedError(RuntimeError):
    """Raised when an edit would overwrite an externally changed desired state."""


class EditorFailedError(RuntimeError):
    """Raised when an editor exits unsuccessfully."""


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
    lock_path: Path = Path("/run/kavernis/network.lock")


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

    @contextmanager
    def network_write_transaction(self) -> Iterator[None]:
        """Acquire the reusable network desired-state write transaction lock.

        Callers such as a future HTTP API can use this boundary around loading,
        validating, and ``replace_desired_resource`` without knowing about
        ``fcntl`` or the lock-file location.
        """
        with network_lock(self.paths.lock_path):
            yield

    def desired_fingerprint(self) -> dict[str, str]:
        """Return SHA-256 fingerprints of the actual desired-state bytes."""
        return {
            filename: hashlib.sha256(path.read_bytes()).hexdigest()
            for filename, path in self._desired_paths().items()
        }

    def replace_desired_resource(
        self,
        resource: str,
        contents: bytes,
        baseline: dict[str, str],
    ) -> None:
        """Validate and atomically replace one resource inside a write transaction.

        The caller must own ``network_write_transaction``.  Checking all three
        fingerprints makes the operation suitable for future API optimistic
        concurrency without treating generated artifact hashes as revisions.

        The network lock and optimistic-concurrency transaction deliberately
        cover all three desired-state files.  Validation here deliberately
        covers only ``resource``: cross-resource resolution belongs to
        ``plan_network`` and ``apply``.
        """
        filename = _resource_filename(resource)
        _validate_desired_resource(resource, contents)
        if self.desired_fingerprint() != baseline:
            raise DesiredStateChangedError(
                "Network desired state changed externally while editing.\n\n"
                "The edited configuration was not installed because doing so could "
                "overwrite concurrent changes.\n\nReload the current configuration "
                "and retry."
            )
        rendered = _with_desired_header(resource, contents)
        _write_desired_atomically(self._desired_paths()[filename], rendered)

    def edit_resource(
        self,
        resource: str,
        invoke_editor: Callable[[Path], int],
        retry_invalid: Callable[[ConfigurationError], bool] | None = None,
    ) -> bool:
        """Edit one desired YAML resource safely; return whether it changed.

        Core retains ownership of locking, candidate lifecycle, resource
        validation, fingerprints, and atomic replacement.  The optional retry
        callback lets the CLI implement a visudo-like correction prompt without
        placing interactive input in business logic.  It receives a validation
        error and returns whether the same candidate should be edited again.
        """
        filename = _resource_filename(resource)
        with self.network_write_transaction():
            baseline = self.desired_fingerprint()
            original = self._desired_paths()[filename].read_bytes()
            editable_original = _with_desired_header(resource, original)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".kavernis-{resource}-",
                suffix=".yaml",
                dir=self._desired_paths()[filename].parent,
            )
            candidate_path = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as temporary:
                    temporary.write(editable_original)
                    temporary.flush()
                    os.fsync(temporary.fileno())
                while True:
                    editor_status = invoke_editor(candidate_path)
                    if editor_status != 0:
                        raise EditorFailedError(
                            "editor exited with status "
                            f"{editor_status}; desired state was unchanged"
                        )
                    contents = candidate_path.read_bytes()
                    if contents == editable_original:
                        return False
                    try:
                        self.replace_desired_resource(resource, contents, baseline)
                    except ConfigurationError as error:
                        if retry_invalid is None or not retry_invalid(error):
                            return False
                        # Reopen this exact candidate; do not recopy live state.
                        continue
                    return True
            finally:
                if candidate_path.exists():
                    candidate_path.unlink()

    def apply(
        self, desired: NetworkDesiredState | None = None
    ) -> NetworkdConfiguration:
        # ``desired`` remains accepted for compatibility.  Reload under the lock
        # so the candidate, history snapshot, and native apply use one revision.
        with self.network_write_transaction():
            desired = load_network(self.paths)
            candidate = plan_network(desired)
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
        with self.network_write_transaction():
            files = {
                filename: self.history.read_revision(revision, filename)
                for filename in DESIRED_FILENAMES
            }
            desired = _desired_from_files(files, f"revision {revision}")
            candidate = plan_network(desired)
            written_files = {
                filename: _with_desired_header(_filename_resource(filename), contents)
                for filename, contents in files.items()
            }
            for filename, path in self._desired_paths().items():
                _write_desired_atomically(path, written_files[filename])
            new_revision = self.history.snapshot(
                written_files, f"rollback network to {revision[:12]}"
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

    def _desired_paths(self) -> dict[str, Path]:
        return {
            "interfaces.yaml": self.paths.interfaces_path,
            "gateways.yaml": self.paths.gateways_path,
            "routes.yaml": self.paths.routes_path,
        }


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


def _validate_desired_resource(resource: str, contents: bytes) -> None:
    """Run schema/model validation that belongs to one desired-state document."""
    source = f"edited {resource} configuration"
    text = contents.decode("utf-8")
    if resource == "interfaces":
        load_interfaces_contents(text, source)
    elif resource == "gateways":
        load_gateways_contents(text, source)
    elif resource == "routes":
        load_routes_contents(text, source)
    else:  # Keep this guard for callers that bypass _resource_filename.
        raise ValueError(f"unsupported network resource: {resource}")


def _write_desired_atomically(path: Path, contents: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata = path.stat() if path.exists() else None
    mode = metadata.st_mode & 0o777 if metadata else 0o644
    descriptor, temporary_name = tempfile.mkstemp(prefix=".kavernis-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as temporary:
            temporary.write(contents)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_name, mode)
        if metadata is not None:
            os.chown(temporary_name, metadata.st_uid, metadata.st_gid)
        os.replace(temporary_name, path)
        directory_descriptor = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _resource_filename(resource: str) -> str:
    if resource not in EDITABLE_RESOURCES:
        raise ValueError(f"unsupported network resource: {resource}")
    return f"{resource}.yaml"


def _filename_resource(filename: str) -> str:
    return filename.removesuffix(".yaml")


def _with_desired_header(resource: str, contents: bytes) -> bytes:
    header = (
        "# Kavernis desired-state configuration.\n"
        "# Direct editing is not supported.\n"
        f'# Use "kavernis edit {resource}" to modify this file safely.\n\n'
    ).encode()
    return contents if contents.startswith(header) else header + contents
