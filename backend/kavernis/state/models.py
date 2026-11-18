"""Typed values crossing state/history boundaries."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


@dataclass(frozen=True)
class HistoryRevision:
    """One immutable desired-state revision."""

    identifier: str
    created_at: datetime
    message: str


@dataclass(frozen=True)
class AppliedDomainState:
    """Last successfully applied state for one Kavernis domain."""

    domain: str
    revision: str
    applied_at: datetime
    artifact_hashes: Mapping[str, str]


class ApplyOperationKind(StrEnum):
    """State-changing operation recorded by the applied-state store."""

    APPLY = "apply"
    ROLLBACK = "rollback"


class ApplyOutcomeKind(StrEnum):
    """Outcome of the most recently recorded state-changing operation."""

    SUCCESS = "success"
    FAILURE = "failure"


@dataclass(frozen=True)
class ApplyOutcome:
    """Outcome metadata separate from the last successful applied state."""

    operation: ApplyOperationKind
    kind: ApplyOutcomeKind
    recorded_at: datetime
    revision: str | None
    message: str | None


class ArtifactDriftKind(StrEnum):
    """Kinds of managed-artifact mismatch."""

    MODIFIED = "modified"
    MISSING = "missing"
    UNEXPECTED = "unexpected"


@dataclass(frozen=True)
class ArtifactDrift:
    """One detected difference in a Kavernis-owned native artifact."""

    filename: str
    kind: ArtifactDriftKind


class CommitPhase(StrEnum):
    """Lifecycle of the successful-apply write, not of native activation."""

    BEFORE_TRANSACTION = "before transaction"
    TRANSACTION_STARTED = "transaction started"
    COMMIT_ATTEMPTED = "commit attempted; outcome uncertain"
    COMMITTED = "commit confirmed"


@dataclass
class ApplyCommit:
    """Shared write receipt, updated by the store even when cancellation escapes.

    Each operation owns one receipt. Only the state store advances its phase;
    core/backend use it to decide recovery without speculative database reads.
    """

    phase: CommitPhase = CommitPhase.BEFORE_TRANSACTION
