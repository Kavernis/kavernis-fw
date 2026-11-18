from pathlib import Path

import pytest
from kavernis.state.history import DesiredStateHistory, RevisionNotFoundError


def test_history_snapshots_only_changed_desired_state_and_reads_revisions(
    tmp_path: Path,
) -> None:
    history = DesiredStateHistory(tmp_path / "history")
    first_contents = b"version: 2\ninterfaces: []\n"
    first = history.snapshot(
        {"network/interfaces.yaml": first_contents}, "apply network desired state"
    )

    assert (tmp_path / "history" / ".git").is_dir()
    assert (
        history.snapshot({"network/interfaces.yaml": first_contents}, "ignored")
        == first
    )

    second_contents = b"version: 2\ninterfaces: [changed]\n"
    second = history.snapshot(
        {"network/interfaces.yaml": second_contents}, "apply network desired state"
    )

    assert second != first
    assert [item.identifier for item in history.list_revisions()] == [second, first]
    assert history.read_revision(first, "network/interfaces.yaml") == first_contents
    assert (
        history.revision_for_contents({"network/interfaces.yaml": second_contents})
        == second
    )
    assert (
        history.revision_for_contents({"network/interfaces.yaml": first_contents})
        is None
    )


def test_history_rejects_invalid_revision_and_filename(tmp_path: Path) -> None:
    history = DesiredStateHistory(tmp_path / "history")
    history.snapshot({"network/interfaces.yaml": b"version: 2\n"}, "initial")

    with pytest.raises(RevisionNotFoundError, match="invalid"):
        history.read_revision("--help", "network/interfaces.yaml")
    with pytest.raises(RevisionNotFoundError, match="not found"):
        history.read_revision("a" * 40, "network/interfaces.yaml")


def test_history_preserves_exact_desired_bytes_and_optional_presence(tmp_path):
    history = DesiredStateHistory(tmp_path / "history")
    files = {
        "network/devices.yaml": b"version: 2\r\ndevices: []\r\n",
        "network/interfaces.yaml": b"version: 2\ninterfaces: []\n# R\xc3\xa9seau\n",
    }
    first = history.snapshot(files, "initial")
    assert history.contents_match(first, files)
    assert (
        history.read_revision(first, "network/devices.yaml")
        == files["network/devices.yaml"]
    )
    second = history.snapshot(
        {**files, "network/routing.yaml": b"version: 2\n"}, "add routing"
    )
    assert set(history.file_names(second)) == {*files, "network/routing.yaml"}
    third = history.snapshot(files, "remove routing")
    assert first != third
    assert history.contents_match(third, files)
    assert not (tmp_path / "history/network/routing.yaml").exists()


@pytest.mark.parametrize(
    "filename",
    [
        "../escape.yaml",
        "network/../escape.yaml",
        "/network/devices.yaml",
        "network/devices.yaml/extra",
        "network/native.network",
        "state.db",
    ],
)
def test_history_rejects_paths_outside_desired_resources(tmp_path, filename):
    from kavernis.state.history import HistoryError

    history = DesiredStateHistory(tmp_path / "history")
    with pytest.raises(HistoryError, match="unsafe"):
        history.snapshot({filename: b"bad"}, "invalid")
    assert not (tmp_path / "history").exists()


def test_empty_history_lists_no_revisions(tmp_path):
    assert DesiredStateHistory(tmp_path / "history").list_revisions() == []


@pytest.mark.parametrize("revision", ["--all", "HEAD", "HEAD~1", "abcd; echo bad"])
def test_all_revision_access_validates_identifiers(tmp_path, revision):
    history = DesiredStateHistory(tmp_path / "history")
    with pytest.raises(RevisionNotFoundError, match="invalid"):
        history.file_names(revision)
