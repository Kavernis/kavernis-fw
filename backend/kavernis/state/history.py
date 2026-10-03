"""Internal Git history for desired-state snapshots only."""

import re
import subprocess
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import NoReturn

from kavernis.state.models import HistoryRevision

_REVISION_PATTERN = re.compile(r"[0-9a-f]{7,40}\Z")
_DESIRED_FILENAME_PATTERN = re.compile(r"[a-z][a-z0-9-]*\.yaml\Z")


class HistoryError(RuntimeError):
    """Raised when desired-state history cannot be used safely."""


class RevisionNotFoundError(HistoryError):
    """Raised when a requested immutable revision is unavailable."""


class DesiredStateHistory:
    """Git-backed snapshots behind a Kavernis-level revision interface."""

    def __init__(self, repository_path: Path) -> None:
        self.repository_path = repository_path

    def snapshot(self, files: Mapping[str, bytes], message: str) -> str:
        """Commit a changed desired-state snapshot, or return the existing revision."""
        self._validate_files(files)
        self._initialize()
        tracked = set(self.file_names())
        for filename, contents in files.items():
            (self.repository_path / filename).write_bytes(contents)
        for filename in tracked - set(files):
            (self.repository_path / filename).unlink(missing_ok=True)
        self._run("add", "-A", "--", *sorted(tracked | set(files)))
        diff = self._run("diff", "--cached", "--quiet", check=False)
        if diff.returncode == 0:
            return self.current_revision() or self._unexpected(
                "history has no revision"
            )
        if diff.returncode != 1:
            raise HistoryError("cannot determine whether desired state changed")
        self._run("commit", "-m", message)
        return self.current_revision() or self._unexpected(
            "committed history has no revision"
        )

    def current_revision(self) -> str | None:
        self._initialize()
        result = self._run("rev-parse", "--verify", "HEAD", check=False)
        if result.returncode != 0:
            return None
        return result.stdout.strip()

    def revision_for_contents(self, files: Mapping[str, bytes]) -> str | None:
        """Return HEAD only if it exactly represents these current desired files."""
        self._validate_files(files)
        revision = self.current_revision()
        if revision is None:
            return None
        return revision if self.contents_match(revision, files) else None

    def contents_match(self, revision: str, files: Mapping[str, bytes]) -> bool:
        """Whether a revision exactly represents the supplied filesystem state."""
        self._validate_files(files)
        if set(self.file_names(revision)) != set(files):
            return False
        return all(
            self.read_revision(revision, filename) == contents
            for filename, contents in files.items()
        )

    def file_names(self, revision: str | None = None) -> tuple[str, ...]:
        """Return the desired-state files physically present in a revision."""
        self._initialize()
        target = revision or "HEAD"
        if revision is not None:
            target = self._resolve_revision(revision)
        result = self._run("ls-tree", "-r", "--name-only", target, check=False)
        if result.returncode != 0:
            if revision is None:
                return ()
            raise RevisionNotFoundError(f"desired-state revision not found: {revision}")
        names = tuple(name for name in result.stdout.splitlines() if name)
        if names:
            self._validate_files({name: b"" for name in names})
        return names

    def list_revisions(self) -> list[HistoryRevision]:
        self._initialize()
        result = self._run("log", "--format=%H%x00%cI%x00%s")
        revisions: list[HistoryRevision] = []
        for line in result.stdout.splitlines():
            identifier, created_at, message = line.split("\x00", 2)
            revisions.append(
                HistoryRevision(identifier, datetime.fromisoformat(created_at), message)
            )
        return revisions

    def read_revision(self, revision: str, filename: str) -> bytes:
        self._validate_revision(revision)
        self._validate_files({filename: b""})
        resolved = self._resolve_revision(revision)
        result = self._run("show", f"{resolved}:{filename}", check=False)
        if result.returncode != 0:
            raise RevisionNotFoundError(
                f"revision {revision} does not contain {filename}"
            )
        return result.stdout.encode()

    def _resolve_revision(self, revision: str) -> str:
        result = self._run(
            "rev-parse", "--verify", f"{revision}^{{commit}}", check=False
        )
        if result.returncode != 0:
            raise RevisionNotFoundError(f"desired-state revision not found: {revision}")
        return result.stdout.strip()

    def _initialize(self) -> None:
        if not (self.repository_path / ".git").is_dir():
            try:
                self.repository_path.mkdir(parents=True, exist_ok=True)
            except OSError as error:
                raise HistoryError(
                    f"cannot create desired-state history: {error}"
                ) from error
            self._run("init")
            self._run("config", "user.name", "Kavernis")
            self._run("config", "user.email", "kavernis@localhost")

    def _run(
        self, *arguments: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                ["git", "-C", str(self.repository_path), *arguments],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError as error:
            raise HistoryError(
                f"cannot execute Git for desired-state history: {error}"
            ) from error
        if check and result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise HistoryError(f"desired-state history operation failed: {detail}")
        return result

    @staticmethod
    def _validate_files(files: Mapping[str, bytes]) -> None:
        if not files:
            raise HistoryError("desired-state snapshot must contain at least one file")
        if any(_DESIRED_FILENAME_PATTERN.fullmatch(name) is None for name in files):
            raise HistoryError("unsafe desired-state history filename")

    @staticmethod
    def _validate_revision(revision: str) -> None:
        if _REVISION_PATTERN.fullmatch(revision) is None:
            raise RevisionNotFoundError(f"invalid desired-state revision: {revision}")

    @staticmethod
    def _unexpected(message: str) -> NoReturn:
        raise HistoryError(message)
