"""Abstract applied-state persistence boundary."""

from collections.abc import Mapping
from typing import Protocol

from kavernis.state.models import AppliedDomainState


class StateStore(Protocol):
    """Persistence used by core orchestration, independent of SQLite."""

    def get_domain_state(self, domain: str) -> AppliedDomainState | None: ...

    def record_successful_apply(
        self, domain: str, revision: str, artifact_hashes: Mapping[str, str]
    ) -> None: ...

    def record_failed_apply(self, domain: str, revision: str, message: str) -> None: ...
