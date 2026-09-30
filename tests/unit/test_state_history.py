from pathlib import Path

import pytest
from kavernis.state.history import DesiredStateHistory, RevisionNotFoundError


def test_history_snapshots_only_changed_desired_state_and_reads_revisions(
    tmp_path: Path,
) -> None:
    history = DesiredStateHistory(tmp_path / "history")
    first_contents = b"version: 1\ninterfaces: []\n"
    first = history.snapshot(
        {"interfaces.yaml": first_contents}, "apply interfaces desired state"
    )

    assert (tmp_path / "history" / ".git").is_dir()
    assert history.snapshot({"interfaces.yaml": first_contents}, "ignored") == first

    second_contents = b"version: 1\ninterfaces: [changed]\n"
    second = history.snapshot(
        {"interfaces.yaml": second_contents}, "apply interfaces desired state"
    )

    assert second != first
    assert [item.identifier for item in history.list_revisions()] == [second, first]
    assert history.read_revision(first, "interfaces.yaml") == first_contents
    assert history.revision_for_contents({"interfaces.yaml": second_contents}) == second
    assert history.revision_for_contents({"interfaces.yaml": first_contents}) is None


def test_history_rejects_invalid_revision_and_filename(tmp_path: Path) -> None:
    history = DesiredStateHistory(tmp_path / "history")
    history.snapshot({"interfaces.yaml": b"version: 1\n"}, "initial")

    with pytest.raises(RevisionNotFoundError, match="invalid"):
        history.read_revision("--help", "interfaces.yaml")
    with pytest.raises(RevisionNotFoundError, match="not found"):
        history.read_revision("a" * 40, "interfaces.yaml")
