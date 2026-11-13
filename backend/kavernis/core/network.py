"""Plan, apply, audit, and recover complete network desired state."""

import difflib
import hashlib
import os
import tempfile
from collections.abc import Callable, Iterator, Mapping
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
from kavernis.config.layout import (
    DESIRED_FILENAMES,
    MANDATORY_DESIRED_FILENAMES,
    NetworkDesiredState,
    desired_from_files,
)
from kavernis.config.network import (
    DevicesConfig,
    LogicalInterfacesConfig,
    RoutingConfig,
    parse_document,
)
from kavernis.config.resolver import resolve_network
from kavernis.core.topology import resolve_topology
from kavernis.state.history import DesiredStateHistory, HistoryError
from kavernis.state.lock import network_lock
from kavernis.state.models import (
    ApplyOperationKind,
    ApplyOutcome,
    ArtifactDrift,
    HistoryRevision,
)
from kavernis.state.sqlite import SQLiteStateStore
from kavernis.state.store import StateStore

NETWORK_DOMAIN = "network"
EDITABLE_RESOURCES = ("devices", "interfaces", "routing")


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
    last_outcome: ApplyOutcome | None


@dataclass(frozen=True)
class NetworkStatePaths:
    devices_path: Path = Path("/etc/kavernis/network/devices.yaml")
    interfaces_path: Path = Path("/etc/kavernis/network/interfaces.yaml")
    routing_path: Path = Path("/etc/kavernis/network/routing.yaml")
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
        """Fingerprint desired-state bytes and the presence of every resource.

        An absent optional file is semantically an empty typed document, but it
        remains distinct here: creating or deleting it while an editor is open
        must be detected as a concurrent desired-state change.
        """
        return _fingerprint_files(_read_desired_files(self._desired_paths()))

    def _require_current(self, files: Mapping[str, bytes]) -> None:
        if self.desired_fingerprint() != _fingerprint_files(files):
            raise DesiredStateChangedError(
                "Network desired state changed externally; reload and retry."
            )

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
            path = self._desired_paths()[filename]
            original = (
                path.read_bytes()
                if path.exists()
                else _empty_desired_resource(resource)
            )
            editable_original = _with_desired_header(resource, original)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".kavernis-{resource}-",
                suffix=".yaml",
                dir=path.parent,
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

    def apply(self) -> NetworkdConfiguration:
        with self.network_write_transaction():
            desired = load_network(self.paths)
            candidate = plan_network(desired)
            self._require_current(desired.files)
            revision = self.history.snapshot(
                desired.files, "apply network desired state"
            )
            self._require_current(desired.files)
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
        last_outcome = self.store.get_last_apply_outcome(NETWORK_DOMAIN)
        if applied is None:
            return NetworkStatus(
                NetworkStatusKind.NOT_YET_APPLIED,
                desired_revision,
                None,
                (),
                last_outcome,
            )
        if desired_revision != applied.revision:
            matches = desired_revision is None and self.history.contents_match(
                applied.revision, files
            )
            if not matches:
                return NetworkStatus(
                    NetworkStatusKind.PENDING_CHANGES,
                    desired_revision,
                    applied.revision,
                    (),
                    last_outcome,
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
            last_outcome,
        )

    def history_revisions(self) -> list[HistoryRevision]:
        return self.history.list_revisions()

    def diff(self, files: dict[str, bytes], revision: str) -> str:
        historical_files = set(self.history.file_names(revision))
        return "".join(
            "".join(
                difflib.unified_diff(
                    (
                        self.history.read_revision(revision, filename)
                        if filename in historical_files
                        else b""
                    )
                    .decode()
                    .splitlines(keepends=True),
                    contents.decode().splitlines(keepends=True),
                    fromfile=f"{filename}@{revision[:12]}",
                    tofile=f"{filename} (current)",
                )
            )
            for filename in sorted(set(files) | historical_files)
            for contents in (files.get(filename, b""),)
        )

    def rollback(self, revision: str) -> NetworkdConfiguration:
        with self.network_write_transaction():
            previous = _read_desired_files(self._desired_paths())
            history_files = set(self.history.file_names(revision))
            if not set(MANDATORY_DESIRED_FILENAMES).issubset(history_files):
                raise ConfigurationError(
                    f"revision {revision} does not contain mandatory network resources"
                )
            files = {
                filename: self.history.read_revision(revision, filename)
                for filename in DESIRED_FILENAMES
                if filename in history_files
            }
            desired = desired_from_files(files, f"revision {revision}")
            candidate = plan_network(desired)
            # Do not overwrite an uncommitted desired state during recovery. Its
            # exact bytes and optional-resource presence become a normal immutable
            # desired-state revision, never an applied-state revision.
            outgoing = desired_from_files(previous, "current network configuration")
            plan_network(outgoing)
            if self.history.revision_for_contents(previous) is None:
                self.history.snapshot(
                    previous, "preserve network desired state before rollback"
                )
            written_files = {
                filename: _with_desired_header(_filename_resource(filename), contents)
                for filename, contents in files.items()
            }
            self._require_current(previous)
            try:
                self._replace_desired_files(written_files)
                new_revision = self.history.snapshot(
                    written_files, f"rollback network to {revision[:12]}"
                )
            except (OSError, HistoryError):
                # Before native activation, restore the prior desired resource set
                # if a file replacement or history operation failed. Recovery
                # failures propagate; they must never be silently discarded.
                self._replace_desired_files(previous)
                raise
            self._require_current(written_files)
            try:
                apply(candidate, self.paths.networkd_path)
            except NetworkdApplyError as error:
                self.store.record_failed_apply(
                    NETWORK_DOMAIN,
                    new_revision,
                    str(error),
                    ApplyOperationKind.ROLLBACK,
                )
                raise
            self.store.record_successful_apply(
                NETWORK_DOMAIN,
                new_revision,
                artifact_hashes(candidate),
                ApplyOperationKind.ROLLBACK,
            )
        return candidate

    def _replace_desired_files(self, files: Mapping[str, bytes]) -> None:
        for filename, path in self._desired_paths().items():
            if filename in files:
                _write_desired_atomically(path, files[filename])
            else:
                _remove_desired(path)

    def _desired_paths(self) -> dict[str, Path]:
        return {
            "network/devices.yaml": self.paths.devices_path,
            "network/interfaces.yaml": self.paths.interfaces_path,
            "network/routing.yaml": self.paths.routing_path,
        }


def _read_desired_files(paths: Mapping[str, Path]) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for filename, path in paths.items():
        try:
            files[filename] = path.read_bytes()
        except FileNotFoundError:
            continue
    return files


def _fingerprint_files(files: Mapping[str, bytes]) -> dict[str, str]:
    return {
        filename: (
            f"present:{hashlib.sha256(files[filename]).hexdigest()}"
            if filename in files
            else "absent"
        )
        for filename in DESIRED_FILENAMES
    }


def load_network(paths: NetworkStatePaths) -> NetworkDesiredState:
    """Load the sole supported layout, preserving optional file presence."""
    files: dict[str, bytes] = {}
    for filename, path in zip(
        DESIRED_FILENAMES,
        (
            paths.devices_path,
            paths.interfaces_path,
            paths.routing_path,
        ),
        strict=True,
    ):
        try:
            files[filename] = path.read_bytes()
        except FileNotFoundError as error:
            if filename in MANDATORY_DESIRED_FILENAMES:
                raise ConfigurationError(
                    f"cannot load mandatory {filename}: {error}"
                ) from error
        except OSError as error:
            raise ConfigurationError(f"cannot load {filename}: {error}") from error
    return desired_from_files(files, "network configuration")


def plan_network(desired: NetworkDesiredState) -> NetworkdConfiguration:
    """Resolve complete network intent and generate a validated candidate."""
    model = resolve_network(desired.devices, desired.interfaces, desired.routing)
    return generate(resolve_topology(model))


def _validate_desired_resource(resource: str, contents: bytes) -> None:
    """Validate one document; cross-resource rules run during planning."""
    source = f"edited {resource} configuration"
    if resource == "devices":
        parse_document(contents, DevicesConfig, source)
    elif resource == "interfaces":
        parse_document(contents, LogicalInterfacesConfig, source)
    elif resource == "routing":
        parse_document(contents, RoutingConfig, source)
    else:
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


def _remove_desired(path: Path) -> None:
    """Remove an optional desired-state file and persist the directory change."""
    try:
        path.unlink()
    except FileNotFoundError:
        return
    directory_descriptor = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)


def _resource_filename(resource: str) -> str:
    if resource not in EDITABLE_RESOURCES:
        raise ValueError(f"unsupported network resource: {resource}")
    return f"network/{resource}.yaml"


def _filename_resource(filename: str) -> str:
    return Path(filename).stem


def _with_desired_header(resource: str, contents: bytes) -> bytes:
    header = (
        "# Kavernis desired-state configuration.\n"
        "# Direct editing is not supported.\n"
        f'# Use "kavernis edit {resource}" to modify this file safely.\n\n'
    ).encode()
    return contents if contents.startswith(header) else header + contents


def _empty_desired_resource(resource: str) -> bytes:
    if resource == "routing":
        return b"version: 2\ngateways: []\nroutes: []\n"
    raise ConfigurationError(f"{resource}.yaml is mandatory and cannot be initialized")
