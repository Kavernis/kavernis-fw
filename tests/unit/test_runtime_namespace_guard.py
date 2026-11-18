"""Regression coverage for the disposable network namespace safety guard."""

import importlib.util
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def namespace_check_module():
    script = Path(__file__).parents[1] / "integration" / "runtime_namespace_check.py"
    spec = importlib.util.spec_from_file_location("runtime_namespace_check", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_namespace_guard_accepts_a_different_namespace(namespace_check_module):
    namespace_check_module.require_isolated_network_namespace(
        {namespace_check_module.ORIGINAL_NETNS_ENV: "net:[100]"},
        lambda path: "net:[200]",
    )


@pytest.mark.parametrize(
    "environment, current_namespace, message",
    [
        ({}, "net:[100]", "original network namespace identity is required"),
        (
            {"KAVERNIS_ORIGINAL_NETNS": "net:[100]"},
            "net:[100]",
            "must run in an isolated network namespace",
        ),
    ],
)
def test_namespace_guard_rejects_missing_or_matching_identity(
    namespace_check_module, environment, current_namespace, message
):
    with pytest.raises(RuntimeError, match=message):
        namespace_check_module.require_isolated_network_namespace(
            environment,
            lambda path: current_namespace,
        )


def test_main_never_creates_devices_when_namespace_guard_fails(
    monkeypatch, namespace_check_module
):
    calls = []
    monkeypatch.setattr(
        namespace_check_module,
        "require_isolated_network_namespace",
        lambda environment: (_ for _ in ()).throw(RuntimeError("not isolated")),
    )
    monkeypatch.setattr(namespace_check_module, "ip", lambda *args: calls.append(args))

    with pytest.raises(RuntimeError):
        namespace_check_module.main()

    assert calls == []
