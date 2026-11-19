"""Regression tests for the unit-suite native-command guard."""

import subprocess

import pytest


@pytest.mark.parametrize(
    "command",
    [
        ["ip", "link", "delete", "test0"],
        ["ip", "link", "set", "dev", "test0", "nomaster"],
        ["networkctl", "reload"],
        ["networkctl", "reconfigure", "test0"],
        ["systemctl", "restart", "systemd-networkd"],
    ],
)
def test_unit_suite_intercepts_native_network_commands(command: list[str]) -> None:
    with pytest.raises(AssertionError, match="native network-management"):
        subprocess.run(command, check=True)


def test_unit_suite_intercepts_native_network_popen() -> None:
    with pytest.raises(AssertionError, match="native network-management"):
        subprocess.Popen(["ip", "link", "delete", "test0"])


def test_unit_suite_intercepts_unknown_native_commands() -> None:
    with pytest.raises(AssertionError, match="unknown subprocess command"):
        subprocess.run(["ethtool", "eth0"], check=True)
