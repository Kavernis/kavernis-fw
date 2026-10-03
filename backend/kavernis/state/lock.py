"""Single-host advisory locks for state-changing Kavernis operations."""

import fcntl
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class LockUnavailableError(RuntimeError):
    """Raised when another Kavernis operation already owns a lock."""


@contextmanager
def state_lock(path: Path, *, description: str = "state") -> Iterator[None]:
    """Acquire a non-blocking, process-scoped advisory lock.

    ``flock`` is released by the kernel when its owning process exits.  This is
    deliberately not a sentinel file: the file may remain after a crash
    without preventing a subsequent operation from acquiring the lock.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise LockUnavailableError(
                f"another Kavernis {description} operation currently owns the lock"
            ) from error
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@contextmanager
def network_lock(path: Path = Path("/run/kavernis/network.lock")) -> Iterator[None]:
    """Serialize all desired-state writes and applies for the network domain."""
    with state_lock(path, description="network"):
        yield
