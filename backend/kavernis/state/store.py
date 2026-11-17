"""Abstract applied-state persistence boundary."""

from collections.abc import Mapping
from typing import Protocol

from kavernis.state.models import (
    AppliedDomainState,
    ApplyCommit,
    ApplyOperationKind,
    ApplyOutcome,
)


class StateStore(Protocol):
    """Persistence used by core orchestration, independent of SQLite."""

    def preflight(self, domain: str) -> None: ...

    def get_domain_state(self, domain: str) -> AppliedDomainState | None: ...

    def get_last_apply_outcome(self, domain: str) -> ApplyOutcome | None: ...

    def record_successful_apply(
        self,
        domain: str,
        revision: str,
        artifact_hashes: Mapping[str, str],
        operation: ApplyOperationKind = ApplyOperationKind.APPLY,
        *,
        commit: ApplyCommit | None = None,
    ) -> None: ...

    def record_failed_apply(
        self,
        domain: str,
        revision: str | None,
        message: str,
        operation: ApplyOperationKind = ApplyOperationKind.APPLY,
    ) -> None: ...
