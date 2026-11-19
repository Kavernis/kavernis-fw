"""Explicitly opt-in, disposable-VM-only networkd activation test.

Never enable on a developer workstation or an administered router.
"""

import os
import subprocess
from pathlib import Path

import pytest
import yaml
from kavernis.backends import networkd
from kavernis.core import network as network_core
from kavernis.core.network import NetworkStatePaths, NetworkStateService


def command(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=20)


def test_apply_update_rollback_and_structural_reconfiguration_in_disposable_vm(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
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
            {"id": "test-nic-two", "name": "kv-test-c", "type": "physical"},
            {"id": "test-nic-three", "name": "kv-test-d", "type": "physical"},
            {
                "id": "test-vlan",
                "name": "kv-test-vlan",
                "type": "vlan",
                "vlan": {"parent": "test-nic", "tag": 30},
            },
            {
                "id": "test-unrelated",
                "name": "kv-test-unrelated",
                "type": "vlan",
                "vlan": {"parent": "test-nic", "tag": 200},
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
    for name in (
        "kv-test-a",
        "kv-test-peer",
        "kv-test-c",
        "kv-test-cpeer",
        "kv-test-d",
        "kv-test-dpeer",
        "kv-test-vlan",
        "kv-test-unrelated",
        "kv-test-br",
    ):
        assert f'"{name}"' not in existing
    command(
        "ip", "link", "add", "kv-test-a", "type", "veth", "peer", "name", "kv-test-peer"
    )
    command(
        "ip",
        "link",
        "add",
        "kv-test-c",
        "type",
        "veth",
        "peer",
        "name",
        "kv-test-cpeer",
    )
    command(
        "ip",
        "link",
        "add",
        "kv-test-d",
        "type",
        "veth",
        "peer",
        "name",
        "kv-test-dpeer",
    )
    candidate = None
    try:
        command("ip", "link", "set", "kv-test-peer", "up")
        command("ip", "link", "set", "kv-test-cpeer", "up")
        command("ip", "link", "set", "kv-test-dpeer", "up")
        candidate = state.apply()
        first = state.store.get_domain_state("network").revision
        interfaces["interfaces"][0]["ipv4"]["address"] = "192.0.3.1/24"
        state.paths.interfaces_path.write_text(yaml.safe_dump(interfaces))
        state.apply()
        state.rollback(first)

        # Failure after native deletion/recreation but before reconfiguration
        # must reconstruct the former topology and retain the old receipt.
        previous_applied = state.store.get_domain_state("network")
        assert previous_applied is not None
        devices["devices"][3]["vlan"]["tag"] = 40
        state.paths.devices_path.write_text(yaml.safe_dump(devices))
        original_lifecycle = networkd.NativeNetworkdOperations.execute_lifecycle

        def fail_after_lifecycle(self, plan, model, progress):
            original_lifecycle(self, plan, model, progress)
            assert progress.deleted_names == {"kv-test-vlan"}
            raise RuntimeError("injected failure after native lifecycle")

        with monkeypatch.context() as context:
            context.setattr(
                networkd.NativeNetworkdOperations,
                "execute_lifecycle",
                fail_after_lifecycle,
            )
            with pytest.raises(RuntimeError, match="after native lifecycle"):
                state.apply()
        observed = {link.name: link for link in state.inspector.inspect(10).links}
        assert observed["kv-test-vlan"].vlan_id == 30
        assert observed["kv-test-vlan"].master == "kv-test-br"
        assert state.store.get_domain_state("network") == previous_applied

        # A candidate which was recreated but fails convergence is likewise
        # recovered before the successful receipt can advance.
        original_verify = network_core.verify_runtime

        def fail_after_recreation(model, *args, **kwargs):
            original_verify(model, *args, **kwargs)
            observed = {link.name: link for link in state.inspector.inspect(10).links}
            if observed["kv-test-vlan"].vlan_id == 40:
                raise RuntimeError("injected failure after VLAN recreation")

        with monkeypatch.context() as context:
            context.setattr(network_core, "verify_runtime", fail_after_recreation)
            with pytest.raises(RuntimeError, match="after VLAN recreation"):
                state.apply()
        observed = {link.name: link for link in state.inspector.inspect(10).links}
        assert observed["kv-test-vlan"].vlan_id == 30
        assert state.store.get_domain_state("network") == previous_applied

        # VLAN tag recreation removes the old identity while leaving the
        # independently managed VLAN untouched.
        state.apply()
        observed = {link.name: link for link in state.inspector.inspect(10).links}
        assert observed["kv-test-vlan"].vlan_id == 40
        assert observed["kv-test-unrelated"].vlan_id == 200

        # Parent replacement and bridge recreation are both live operations.
        devices["devices"][3]["vlan"]["parent"] = "test-nic-two"
        state.paths.devices_path.write_text(yaml.safe_dump(devices))
        state.apply()
        observed = {link.name: link for link in state.inspector.inspect(10).links}
        assert observed["kv-test-vlan"].parent == "kv-test-c"
        assert observed["kv-test-unrelated"].parent == "kv-test-a"

        devices["devices"][5]["bridge"]["members"].append("test-nic-three")
        devices["devices"][5]["bridge"]["stp"] = True
        state.paths.devices_path.write_text(yaml.safe_dump(devices))
        state.apply()
        observed = {link.name: link for link in state.inspector.inspect(10).links}
        assert observed["kv-test-br"].stp is True
        assert observed["kv-test-d"].master == "kv-test-br"

        devices["devices"][5]["bridge"]["members"].remove("test-nic-three")
        state.paths.devices_path.write_text(yaml.safe_dump(devices))
        state.apply()
        observed = {link.name: link for link in state.inspector.inspect(10).links}
        assert observed["kv-test-d"].master is None

        # Retire the bridge and its dependent VLAN without deleting physical or
        # unrelated virtual devices.
        devices["devices"] = [
            devices["devices"][0],
            devices["devices"][1],
            devices["devices"][2],
            devices["devices"][4],
        ]
        interfaces["interfaces"] = []
        state.paths.devices_path.write_text(yaml.safe_dump(devices))
        state.paths.interfaces_path.write_text(yaml.safe_dump(interfaces))
        state.apply()
        observed = {link.name: link for link in state.inspector.inspect(10).links}
        assert "kv-test-vlan" not in observed
        assert "kv-test-br" not in observed
        assert "kv-test-a" in observed
        assert observed["kv-test-unrelated"].vlan_id == 200
    finally:
        # Files may have been installed even if first apply failed. Exact fixture
        # names are known independently of whether apply returned a candidate.
        filenames = (
            candidate.files
            if candidate
            else {
                "10-kavernis-test-nic.network",
                "10-kavernis-test-nic-two.network",
                "10-kavernis-test-nic-three.network",
                "10-kavernis-test-vlan.network",
                "10-kavernis-test-vlan.netdev",
                "10-kavernis-test-unrelated.network",
                "10-kavernis-test-unrelated.netdev",
                "10-kavernis-test-bridge.network",
                "10-kavernis-test-bridge.netdev",
            }
        )
        for filename in filenames:
            (native / filename).unlink(missing_ok=True)
        command("networkctl", "reload")
        command("ip", "link", "delete", "kv-test-a")
        command("ip", "link", "delete", "kv-test-c")
        command("ip", "link", "delete", "kv-test-d")
        result = subprocess.run(
            ["ip", "link", "show", "kv-test-br"], capture_output=True, timeout=3
        )
        if result.returncode == 0:
            command("ip", "link", "delete", "kv-test-br")
