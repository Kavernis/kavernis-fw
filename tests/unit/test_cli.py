from pathlib import Path

from kavernis import cli
from kavernis.backends.networkd import NetworkdConfiguration


def write_interfaces_config(path: Path) -> None:
    path.write_text(
        """version: 1
interfaces:
  - uid: 8f3a7c22-1c7d-4d6b-a901-000000000001
    id: lan
    name: Users LAN
    device: eth1
    ipv4: {mode: dhcp}
    ipv6: {mode: disabled}
""",
        encoding="utf-8",
    )


def test_plan_interfaces_prints_candidate_without_applying(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    config_path = tmp_path / "interfaces.yaml"
    write_interfaces_config(config_path)
    monkeypatch.setattr(cli, "INTERFACES_PATH", str(config_path))

    result = cli.main(["plan", "interfaces"])

    assert result == 0
    assert capsys.readouterr().out == (
        "--- 10-kavernis-lan.network\n"
        "[Match]\nName=eth1\n\n[Network]\nIPv6AcceptRA=no\nDHCP=ipv4\n"
    )


def test_apply_interfaces_uses_the_planned_candidate(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    config_path = tmp_path / "interfaces.yaml"
    write_interfaces_config(config_path)
    monkeypatch.setattr(cli, "INTERFACES_PATH", str(config_path))
    candidate = NetworkdConfiguration(files={"10-kavernis-lan.network": "content\n"})
    applied: list[bool] = []

    def apply(_: object) -> NetworkdConfiguration:
        applied.append(True)
        return candidate

    monkeypatch.setattr(cli, "apply_interfaces", apply)

    result = cli.main(["apply", "interfaces"])

    assert result == 0
    assert applied == [True]
    assert capsys.readouterr().out == "Applied 1 systemd-networkd file(s).\n"
