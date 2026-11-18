"""Disposable-VM-only recovery checks against a real systemd-networkd instance."""

import hashlib
import json
import os
import sqlite3
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml
from kavernis.backends import networkd
from kavernis.core import network as network_core
from kavernis.core.network import NETWORK_DOMAIN, NetworkStatePaths, NetworkStateService
from kavernis.state.models import ApplyOutcomeKind
from kavernis.state.sqlite import StateStoreError


def command(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=20)


def _require_disposable_networkd_vm() -> Path:
    marker = Path("/run/kavernis-disposable-networkd-test-vm")
    if os.environ.get("KAVERNIS_DISPOSABLE_NETWORKD_VM") != "1" or not marker.exists():
        pytest.skip("requires explicitly designated disposable systemd-networkd VM")
    if os.geteuid() != 0:
        pytest.skip("disposable VM test requires root")
    command("systemd-detect-virt", "--vm")
    command("systemctl", "is-active", "systemd-networkd")
    return Path("/run/systemd/network")


def _link_exists(name: str) -> bool:
    return subprocess.run(
        ["ip", "link", "show", "dev", name], capture_output=True, text=True, timeout=3
    ).returncode == 0


def _addresses(name: str) -> set[str]:
    links = json.loads(command("ip", "-j", "address", "show", "dev", name).stdout)
    return {
        f"{address['local']}/{address['prefixlen']}"
        for address in links[0].get("addr_info", [])
        if address["family"] == "inet"
    }


def _routes(name: str) -> list[dict[str, object]]:
    return json.loads(command("ip", "-j", "-4", "route", "show", "dev", name).stdout)


def _artifact_hashes(files: dict[str, bytes]) -> dict[str, str]:
    return {
        name: hashlib.sha256(contents).hexdigest()
        for name, contents in sorted(files.items())
    }


@dataclass(frozen=True)
class Baseline:
    desired_revision: str
    applied_revision: str
    artifacts: dict[str, bytes]
    artifact_hashes: dict[str, str]
    addresses: set[str]
    routes: list[dict[str, object]]
    networkd_status: str
    setup_state: str | None


@dataclass(frozen=True)
class RecoveryTopology:
    state: NetworkStateService
    native: Path
    artifact_names: tuple[str, ...]
    link: str
    peer: str
    old_address: str
    new_address: str

    def write_address(self, address: str) -> None:
        interfaces = yaml.safe_load(self.state.paths.interfaces_path.read_text())
        interfaces["interfaces"][0]["ipv4"]["address"] = address
        self.state.paths.interfaces_path.write_text(yaml.safe_dump(interfaces))

    def baseline(self) -> Baseline:
        applied = self.state.store.get_domain_state(NETWORK_DOMAIN)
        assert applied is not None
        artifacts = {
            name: (self.native / name).read_bytes() for name in self.artifact_names
        }
        link = next(
            link
            for link in self.state.inspector.inspect(10).links
            if link.name == self.link
        )
        desired_revision = self.state.history.revision_for_contents(
            network_core.load_network(self.state.paths).files
        )
        assert desired_revision is not None
        return Baseline(
            desired_revision=desired_revision,
            applied_revision=applied.revision,
            artifacts=artifacts,
            artifact_hashes=_artifact_hashes(artifacts),
            addresses=_addresses(self.link),
            routes=_routes(self.link),
            networkd_status=command(
                "networkctl", "--json=short", "status", self.link
            ).stdout,
            setup_state=link.setup_state,
        )

    def assert_recovered(self, baseline: Baseline) -> None:
        artifacts = {
            name: (self.native / name).read_bytes() for name in self.artifact_names
        }
        assert artifacts == baseline.artifacts
        assert _artifact_hashes(artifacts) == baseline.artifact_hashes
        assert _addresses(self.link) == baseline.addresses
        assert self.old_address in _addresses(self.link)
        assert self.new_address not in _addresses(self.link)
        assert _routes(self.link) == baseline.routes
        link = next(
            link
            for link in self.state.inspector.inspect(10).links
            if link.name == self.link
        )
        assert link.setup_state == baseline.setup_state == "configured"
        assert link.network_file == str(self.native / self.artifact_names[0])


@contextmanager
def recovery_topology(tmp_path: Path, suffix: str) -> Iterator[RecoveryTopology]:
    native = _require_disposable_networkd_vm()
    link = f"kv-rec-{suffix}"
    peer = f"kv-rec-{suffix}p"
    device_id = f"recovery-{suffix}"
    artifact_name = f"10-kavernis-{device_id}.network"
    assert not (native / artifact_name).exists(), (
        f"refusing to replace existing test artifact {artifact_name}"
    )
    assert not _link_exists(link), f"refusing to replace existing test link {link}"
    assert not _link_exists(peer), f"refusing to replace existing test link {peer}"

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
    old_address = f"192.0.2.{10 + ord(suffix) - ord('a')}/24"
    new_address = f"192.0.3.{10 + ord(suffix) - ord('a')}/24"
    state.paths.devices_path.write_text(
        yaml.safe_dump(
            {
                "version": 2,
                "devices": [{"id": device_id, "name": link, "type": "physical"}],
            }
        )
    )
    state.paths.interfaces_path.write_text(
        yaml.safe_dump(
            {
                "version": 2,
                "interfaces": [
                    {
                        "id": f"recovery-lan-{suffix}",
                        "name": f"Recovery LAN {suffix}",
                        "device": device_id,
                        "ipv4": {"mode": "static", "address": old_address},
                    }
                ],
            }
        )
    )
    state.paths.routing_path.write_text(
        yaml.safe_dump({"version": 2, "gateways": [], "routes": []})
    )
    command("ip", "link", "add", link, "type", "veth", "peer", "name", peer)
    try:
        command("ip", "link", "set", peer, "up")
        yield RecoveryTopology(
            state=state,
            native=native,
            artifact_names=(artifact_name,),
            link=link,
            peer=peer,
            old_address=old_address,
            new_address=new_address,
        )
    finally:
        cleanup_errors: list[str] = []
        try:
            (native / artifact_name).unlink(missing_ok=True)
        except OSError as error:
            cleanup_errors.append(f"could not remove {artifact_name}: {error}")
        try:
            command("networkctl", "reload")
        except (subprocess.SubprocessError, OSError) as error:
            cleanup_errors.append(f"could not reload networkd during cleanup: {error}")
        if _link_exists(link):
            try:
                command("ip", "link", "delete", link)
            except (subprocess.SubprocessError, OSError) as error:
                cleanup_errors.append(f"could not delete {link}: {error}")
        if cleanup_errors:
            raise RuntimeError("; ".join(cleanup_errors))


@pytest.fixture
def observed_networkd_reloads(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    original_run = networkd.subprocess.run
    reloads: list[list[str]] = []

    def observe(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args == ["networkctl", "reload"]:
            reloads.append(args)
        return original_run(args, **kwargs)

    monkeypatch.setattr(networkd.subprocess, "run", observe)
    return reloads


def test_runtime_verification_failure_recovers_native_networkd_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    observed_networkd_reloads: list[list[str]],
) -> None:
    """A post-reload verification failure restores files, kernel state, and SQLite."""
    with recovery_topology(tmp_path, "a") as topology:
        topology.state.apply()
        baseline = topology.baseline()
        assert baseline.desired_revision == baseline.applied_revision
        topology.write_address(topology.new_address)
        original_verify = network_core.verify_runtime
        candidate_verified = False
        reloads_before = len(observed_networkd_reloads)

        def fail_candidate_verification(
            model: object, *args: object, **kwargs: object
        ) -> None:
            nonlocal candidate_verified
            if topology.new_address in _addresses(topology.link):
                candidate_verified = True
                raise RuntimeError("injected candidate runtime verification failure")
            original_verify(model, *args, **kwargs)

        monkeypatch.setattr(network_core, "verify_runtime", fail_candidate_verification)
        with pytest.raises(
            RuntimeError, match="injected candidate runtime verification"
        ):
            topology.state.apply()

        assert candidate_verified
        assert len(observed_networkd_reloads) >= reloads_before + 2
        topology.assert_recovered(baseline)
        applied = topology.state.store.get_domain_state(NETWORK_DOMAIN)
        assert applied is not None
        assert applied.revision == baseline.applied_revision
        outcome = topology.state.store.get_last_apply_outcome(NETWORK_DOMAIN)
        assert outcome is not None
        assert outcome.kind is ApplyOutcomeKind.FAILURE
        assert outcome.revision != baseline.applied_revision
        assert topology.state.history.read_revision(
            baseline.applied_revision, "network/interfaces.yaml"
        )


def test_sqlite_precommit_failure_recovers_native_networkd_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    observed_networkd_reloads: list[list[str]],
) -> None:
    """A definite SQLite pre-commit failure rolls real native state back."""
    with recovery_topology(tmp_path, "b") as topology:
        topology.state.apply()
        baseline = topology.baseline()
        topology.write_address(topology.new_address)
        candidate_activated = False
        reloads_before = len(observed_networkd_reloads)

        def fail_before_commit() -> None:
            nonlocal candidate_activated
            assert topology.new_address in _addresses(topology.link)
            assert topology.old_address not in _addresses(topology.link)
            candidate_activated = True
            raise StateStoreError("injected SQLite failure before commit")

        monkeypatch.setattr(
            topology.state.store, "_before_success_commit", fail_before_commit
        )
        with pytest.raises(
            StateStoreError, match="injected SQLite failure before commit"
        ):
            topology.state.apply()

        assert candidate_activated
        assert len(observed_networkd_reloads) >= reloads_before + 2
        topology.assert_recovered(baseline)
        applied = topology.state.store.get_domain_state(NETWORK_DOMAIN)
        assert applied is not None
        assert applied.revision == baseline.applied_revision
        outcome = topology.state.store.get_last_apply_outcome(NETWORK_DOMAIN)
        assert outcome is not None
        assert outcome.kind is ApplyOutcomeKind.FAILURE
        assert outcome.revision != baseline.applied_revision


def test_postcommit_interruption_retains_confirmed_native_networkd_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    observed_networkd_reloads: list[list[str]],
) -> None:
    """A post-commit interruption is observable but must not trigger recovery."""
    with recovery_topology(tmp_path, "c") as topology:
        topology.state.apply()
        baseline = topology.baseline()
        topology.write_address(topology.new_address)
        reloads_before = len(observed_networkd_reloads)
        with sqlite3.connect(topology.state.paths.database_path) as connection:
            failures_before = connection.execute(
                "SELECT count(*) FROM apply_failure WHERE domain = ?", (NETWORK_DOMAIN,)
            ).fetchone()[0]
        candidate_verified = False

        def interrupt_after_commit() -> None:
            nonlocal candidate_verified
            assert topology.new_address in _addresses(topology.link)
            candidate_verified = True
            raise KeyboardInterrupt("injected interruption after confirmed commit")

        monkeypatch.setattr(
            topology.state.store, "_after_success_commit", interrupt_after_commit
        )
        with pytest.raises(KeyboardInterrupt, match="injected interruption"):
            topology.state.apply()

        assert candidate_verified
        assert len(observed_networkd_reloads) == reloads_before + 1
        applied = topology.state.store.get_domain_state(NETWORK_DOMAIN)
        assert applied is not None
        assert applied.revision != baseline.applied_revision
        assert topology.new_address in _addresses(topology.link)
        assert topology.old_address not in _addresses(topology.link)
        artifacts = {
            name: (topology.native / name).read_bytes()
            for name in topology.artifact_names
        }
        assert artifacts != baseline.artifacts
        outcome = topology.state.store.get_last_apply_outcome(NETWORK_DOMAIN)
        assert outcome is not None
        assert outcome.kind is ApplyOutcomeKind.SUCCESS
        assert outcome.revision == applied.revision
        with sqlite3.connect(topology.state.paths.database_path) as connection:
            failures_after = connection.execute(
                "SELECT count(*) FROM apply_failure WHERE domain = ?", (NETWORK_DOMAIN,)
            ).fetchone()[0]
        assert failures_after == failures_before
