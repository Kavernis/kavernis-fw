"""Unit-suite isolation: native network management is never host execution."""

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

_NETWORK_MANAGEMENT_COMMANDS = frozenset(
    {
        "bridge",
        "ifconfig",
        "ip",
        "networkctl",
        "nmcli",
        "service",
        "systemctl",
    }
)
_ALLOWED_SUBPROCESSES = frozenset({"git"})


def _command_name(arguments: object) -> str | None:
    if isinstance(arguments, Sequence) and not isinstance(arguments, str) and arguments:
        return Path(str(arguments[0])).name
    return None


def _reject_native_network_command(arguments: object, shell: object = False) -> None:
    if shell:
        raise AssertionError("unit test attempted shell command execution")
    command = _command_name(arguments)
    if command in _NETWORK_MANAGEMENT_COMMANDS:
        raise AssertionError(
            "unit test attempted native network-management command "
            f"{command!r}; install an explicit fake instead"
        )
    if command not in _ALLOWED_SUBPROCESSES:
        raise AssertionError(
            "unit test attempted unknown subprocess command "
            f"{command!r}; install an explicit fake instead"
        )


@pytest.fixture(autouse=True)
def block_native_network_management(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail closed if a unit fixture accidentally forwards a host mutation.

    Tests may run Git and other local, non-network commands when needed. Any
    Linux network-management executable must be explicitly replaced by that
    test's fake; forwarding it to the developer host is never permitted.
    """
    original_run = subprocess.run
    original_popen = subprocess.Popen

    def run(arguments: object = None, *args: object, **kwargs: object):
        _reject_native_network_command(
            kwargs.get("args", arguments), kwargs.get("shell", False)
        )
        if arguments is None and "args" in kwargs:
            return original_run(*args, **kwargs)
        return original_run(arguments, *args, **kwargs)

    def popen(arguments: object = None, *args: object, **kwargs: object):
        _reject_native_network_command(
            kwargs.get("args", arguments), kwargs.get("shell", False)
        )
        if arguments is None and "args" in kwargs:
            return original_popen(*args, **kwargs)
        return original_popen(arguments, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(subprocess, "Popen", popen)
