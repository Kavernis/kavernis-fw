from pathlib import Path

import pytest
from kavernis import cli
from kavernis.backends.networkd import NetworkdConfiguration
from kavernis.core.network import NetworkStateService


def write_network_config(path: Path) -> None:
    (path / "interfaces.yaml").write_text(
        "version: 1\ninterfaces:\n  - id: lan\n    name: LAN\n    device: eth1\n"
        "    ipv4: {mode: dhcp}\n    ipv6: {mode: disabled}\n"
    )
    (path / "gateways.yaml").write_text("version: 1\ngateways: []\n")
    (path / "routes.yaml").write_text("version: 1\nroutes: []\n")


def configure_paths(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    monkeypatch.setattr(cli, "INTERFACES_PATH", str(path / "interfaces.yaml"))
    monkeypatch.setattr(cli, "GATEWAYS_PATH", str(path / "gateways.yaml"))
    monkeypatch.setattr(cli, "ROUTES_PATH", str(path / "routes.yaml"))
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
    assert "--- 10-kavernis-lan.network" in capsys.readouterr().out
    assert not (tmp_path / "history").exists()
    assert not (tmp_path / "state.db").exists()


def test_apply_network_uses_one_network_transaction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    write_network_config(tmp_path)
    configure_paths(monkeypatch, tmp_path)
    candidate = NetworkdConfiguration(files={"10-kavernis-lan.network": "content\n"})
    applied: list[bool] = []

    def apply(
        _: NetworkStateService, desired: object | None = None
    ) -> NetworkdConfiguration:
        applied.append(True)
        return candidate

    monkeypatch.setattr(NetworkStateService, "apply", apply)
    assert cli.main(["apply", "network"]) == 0
    assert applied == [True]
    assert capsys.readouterr().out == "Applied 1 systemd-networkd file(s).\n"


@pytest.mark.parametrize("action", ["plan", "apply", "status", "history", "diff"])
def test_interfaces_is_rejected_as_a_cli_resource(action: str) -> None:
    with pytest.raises(SystemExit, match="2"):
        cli.main([action, "interfaces"])


@pytest.mark.parametrize("resource", ("interfaces", "gateways", "routes"))
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
