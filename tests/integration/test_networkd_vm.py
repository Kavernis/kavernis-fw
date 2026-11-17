"""Explicitly opt-in, disposable-VM-only networkd activation test.

Never enable on a developer workstation or an administered router.
"""

import os
import subprocess
from pathlib import Path

import pytest
import yaml
from kavernis.core.network import NetworkStatePaths, NetworkStateService
from kavernis.core.network_transition import UnsupportedNetworkTransition


def command(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=20)


def test_apply_update_rollback_and_structural_rejection_in_disposable_vm(tmp_path):
    marker = Path("/run/kavernis-disposable-networkd-test-vm")
    if os.environ.get("KAVERNIS_DISPOSABLE_NETWORKD_VM") != "1" or not marker.exists():
        pytest.skip("requires explicitly designated disposable systemd-networkd VM")
    if os.geteuid() != 0:
        pytest.skip("disposable VM test requires root")
    command("systemd-detect-virt", "--vm")
    command("systemctl", "is-active", "systemd-networkd")
    native = Path("/run/systemd/network")
    assert not list(native.glob("10-kavernis-*")), (
        "VM must have no existing Kavernis artifacts"
    )
    state = NetworkStateService(
        NetworkStatePaths(
            devices_path=tmp_path / "devices.yaml",
            interfaces_path=tmp_path / "interfaces.yaml",
            routing_path=tmp_path / "routing.yaml",
            history_path=tmp_path / "history",
            database_path=tmp_path / "state.db",
            networkd_path=native,
            lock_path=tmp_path / "network.lock",
        )
    )
    devices = {
        "version": 2,
        "devices": [
            {"id": "test-nic", "name": "kv-test-a", "type": "physical"},
            {
                "id": "test-vlan",
                "name": "kv-test-vlan",
                "type": "vlan",
                "vlan": {"parent": "test-nic", "tag": 30},
            },
            {
                "id": "test-bridge",
                "name": "kv-test-br",
                "type": "bridge",
                "bridge": {"members": ["test-vlan"], "stp": False},
            },
        ],
    }
    interfaces = {
        "version": 2,
        "interfaces": [
            {
                "id": "test-lan",
                "name": "Test LAN",
                "device": "test-bridge",
                "ipv4": {"mode": "static", "address": "192.0.2.1/24"},
                "ipv6": {"mode": "static", "address": "2001:db8::1/64"},
            },
        ],
    }
    state.paths.devices_path.write_text(yaml.safe_dump(devices))
    state.paths.interfaces_path.write_text(yaml.safe_dump(interfaces))
    state.paths.routing_path.write_text(
        yaml.safe_dump(
            {
                "version": 2,
                "gateways": [{"id": "direct", "interface": "test-lan"}],
                "routes": [
                    {
                        "id": "test-route",
                        "network": "198.51.100.0/24",
                        "gateway": "direct",
                    }
                ],
            }
        )
    )
    # Refuse name collisions before creating anything. Cleanup uses only objects
    # created by this fixture and never enumerates unrelated kernel devices.
    existing = command("ip", "-j", "link", "show").stdout
    for name in ("kv-test-a", "kv-test-peer", "kv-test-vlan", "kv-test-br"):
        assert f'"{name}"' not in existing
    command(
        "ip", "link", "add", "kv-test-a", "type", "veth", "peer", "name", "kv-test-peer"
    )
    candidate = None
    try:
        command("ip", "link", "set", "kv-test-peer", "up")
        candidate = state.apply()
        first = state.store.get_domain_state("network").revision
        interfaces["interfaces"][0]["ipv4"]["address"] = "192.0.3.1/24"
        state.paths.interfaces_path.write_text(yaml.safe_dump(interfaces))
        state.apply()
        state.rollback(first)
        devices["devices"][1]["vlan"]["tag"] = 40
        state.paths.devices_path.write_text(yaml.safe_dump(devices))
        before = {p.name: p.read_bytes() for p in native.glob("10-kavernis-*")}
        with pytest.raises(UnsupportedNetworkTransition):
            state.apply()
        assert before == {p.name: p.read_bytes() for p in native.glob("10-kavernis-*")}
    finally:
        # Files may have been installed even if first apply failed. Exact fixture
        # names are known independently of whether apply returned a candidate.
        filenames = (
            candidate.files
            if candidate
            else {
                "10-kavernis-test-nic.network",
                "10-kavernis-test-vlan.network",
                "10-kavernis-test-vlan.netdev",
                "10-kavernis-test-bridge.network",
                "10-kavernis-test-bridge.netdev",
            }
        )
        for filename in filenames:
            (native / filename).unlink(missing_ok=True)
        command("networkctl", "reload")
        command("ip", "link", "delete", "kv-test-a")
        result = subprocess.run(
            ["ip", "link", "show", "kv-test-br"], capture_output=True, timeout=3
        )
        if result.returncode == 0:
            command("ip", "link", "delete", "kv-test-br")
