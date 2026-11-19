from datetime import UTC, datetime
from pathlib import Path

import pytest
from kavernis import cli
from kavernis.backends.networkd import NetworkdConfiguration
from kavernis.core.network import NetworkStateService, NetworkStatus, NetworkStatusKind
from kavernis.core.network_transition import (
    NetworkTransitionPlan,
    TransitionOperation,
    TransitionOperationKind,
)
from kavernis.state.models import ApplyOperationKind, ApplyOutcome, ApplyOutcomeKind
from network_helpers import write_desired as write_network_config


def configure_paths(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    monkeypatch.setattr(cli, "INTERFACES_PATH", str(path / "interfaces.yaml"))
    monkeypatch.setattr(cli, "DEVICES_PATH", str(path / "devices.yaml"))
    monkeypatch.setattr(cli, "ROUTING_PATH", str(path / "routing.yaml"))
    monkeypatch.setattr(cli, "HISTORY_PATH", str(path / "history"))
    monkeypatch.setattr(cli, "STATE_DATABASE_PATH", str(path / "state.db"))
    monkeypatch.setattr(cli, "NETWORKD_PATH", str(path / "networkd"))
    monkeypatch.setattr(cli, "STATE_LOCK_PATH", str(path / "state.lock"))


def test_plan_network_is_side_effect_free(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    write_network_config(tmp_path)
    configure_paths(monkeypatch, tmp_path)
    assert cli.main(["plan", "network"]) == 0
    assert "--- 10-kavernis-lan-nic.network" in capsys.readouterr().out
    assert not (tmp_path / "history").exists()
    assert not (tmp_path / "state.db").exists()


def test_plan_prints_structural_lifecycle_operations(capsys) -> None:
    cli._print_candidate(
        NetworkdConfiguration(
            {"10-kavernis-vlan.network": "[Match]\nName=vlan100\n\n[Network]\n"}
        ),
        NetworkTransitionPlan(
            (),
            (
                TransitionOperation(
                    TransitionOperationKind.DELETE,
                    "vlan",
                    "vlan100",
                    "delete managed VLAN before recreation",
                ),
                TransitionOperation(
                    TransitionOperationKind.CREATE,
                    "vlan",
                    "vlan100",
                    "create managed VLAN",
                ),
            ),
            ("vlan100",),
            ("vlan100",),
            ("vlan100",),
        ),
    )
    output = capsys.readouterr().out
    assert "Network transition plan:" in output
    assert "DELETE vlan (vlan100)" in output
    assert "LIVE STRUCTURAL RECONFIGURATION" in output
    assert "Reboot required: no" in output
    assert "Affected kernel interfaces: vlan100" in output
    assert "may interrupt traffic" in output


def test_apply_network_uses_one_network_transaction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    write_network_config(tmp_path)
    configure_paths(monkeypatch, tmp_path)
    candidate = NetworkdConfiguration(
        files={"10-kavernis-lan-nic.network": "content\n"}
    )
    applied: list[bool] = []

    def apply(_: NetworkStateService) -> NetworkdConfiguration:
        applied.append(True)
        return candidate

    monkeypatch.setattr(NetworkStateService, "apply", apply)
    assert cli.main(["apply", "network"]) == 0
    assert applied == [True]
    assert (
        "Configuration activation and static runtime verified"
        in capsys.readouterr().out
    )


@pytest.mark.parametrize("action", ["plan", "apply", "status", "history", "diff"])
def test_interfaces_is_rejected_as_a_cli_resource(action: str) -> None:
    with pytest.raises(SystemExit, match="2"):
        cli.main([action, "interfaces"])


@pytest.mark.parametrize("resource", ("devices", "interfaces", "routing"))
def test_edit_accepts_network_resources(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys, resource: str
) -> None:
    write_network_config(tmp_path)
    configure_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(NetworkStateService, "edit_resource", lambda *_: False)

    assert cli.main(["edit", resource]) == 0
    assert capsys.readouterr().out == "No changes.\n"


def test_visual_precedes_editor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VISUAL", "visual --wait")
    monkeypatch.setenv("EDITOR", "editor")
    assert cli._editor_command() == ["visual", "--wait"]


def test_editor_is_used_when_visual_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setenv("EDITOR", "editor --wait")
    assert cli._editor_command() == ["editor", "--wait"]


def test_editor_fallback_is_deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.delenv("EDITOR", raising=False)
    assert cli._editor_command() == ["nano"]


def test_unknown_edit_resource_is_rejected() -> None:
    with pytest.raises(SystemExit, match="2"):
        cli.main(["edit", "network"])


def test_edit_retries_invalid_candidate_in_same_cli_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    write_network_config(tmp_path)
    configure_paths(monkeypatch, tmp_path)
    candidates: list[Path] = []

    def editor(path: Path) -> int:
        candidates.append(path)
        if len(candidates) == 1:
            path.write_text("routes: [\n")
        else:
            assert path.read_text() == "routes: [\n"
            path.write_text("version: 2\nroutes: []\n")
        return 0

    monkeypatch.setattr(cli, "_invoke_editor", editor)
    monkeypatch.setattr("builtins.input", lambda _: "e")

    assert cli.main(["edit", "routing"]) == 0
    assert candidates[0] == candidates[1]
    assert "Configuration is invalid:" in capsys.readouterr().err


def test_status_prints_synchronization_and_failed_operation_separately(capsys) -> None:
    cli._print_status(
        NetworkStatus(
            NetworkStatusKind.IN_SYNC,
            "desired",
            "successful",
            (),
            ApplyOutcome(
                ApplyOperationKind.APPLY,
                ApplyOutcomeKind.FAILURE,
                datetime(2026, 1, 1, tzinfo=UTC),
                "failed",
                "networkctl reload failed",
            ),
        )
    )
    output = capsys.readouterr().out
    assert "Network configuration: in sync" in output
    assert "Last successful revision: successful" in output
    assert "Last apply: failed" in output
    assert "Failure: networkctl reload failed" in output
