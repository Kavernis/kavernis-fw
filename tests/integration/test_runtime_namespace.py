"""Opt-in kernel tests. These do not exercise systemd-networkd activation."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def test_kernel_observations_in_disposable_user_network_namespace():
    if os.environ.get("KAVERNIS_RUN_NETNS_TESTS") != "1":
        pytest.skip(
            "isolated kernel test requires KAVERNIS_RUN_NETNS_TESTS=1 "
            "and user/net namespaces"
        )
    if not shutil.which("unshare") or not shutil.which("ip"):
        pytest.skip("unshare and iproute2 are required")
    probe = subprocess.run(
        ["unshare", "--user", "--map-root-user", "--net", "true"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if probe.returncode:
        pytest.skip(f"user/network namespaces unavailable: {probe.stderr.strip()}")
    script = Path(__file__).with_name("runtime_namespace_check.py")
    environment = os.environ.copy()
    environment["KAVERNIS_ORIGINAL_NETNS"] = os.readlink("/proc/self/ns/net")
    result = subprocess.run(
        ["unshare", "--user", "--map-root-user", "--net", sys.executable, str(script)],
        capture_output=True,
        env=environment,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
