import os
import stat
import subprocess
from pathlib import Path

import pytest
from kavernis.backends.networkd import (
    NetworkdApplyError,
    NetworkdConfiguration,
    NetworkdValidationError,
    validate,
)
from kavernis.core.network_transition import (
    NetworkTransitionPlan,
    TransitionOperation,
    TransitionOperationKind,
)
from kavernis.models.device import (
    BridgeSettings,
    NetworkDevice,
    PhysicalSettings,
    VLANSettings,
)
from kavernis.models.network import NetworkModel


def test_reject_unsafe_generated_candidate() -> None:
    candidate = NetworkdConfiguration(
        files={"../outside.network": "[Match]\n[Network]\n"}
    )

    with pytest.raises(NetworkdValidationError, match="unsafe"):
        validate(candidate)


def test_apply_restores_managed_files_when_networkctl_reload_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from kavernis.backends import networkd

    managed = tmp_path / "10-kavernis-old.network"
    managed.write_text("old\n", encoding="utf-8")
    managed.chmod(0o600)
    unmanaged = tmp_path / "20-unmanaged.network"
    unmanaged.write_text("keep\n", encoding="utf-8")

    calls = []

    def reload_fails(*args, **kwargs):
        calls.append((args, kwargs))
        if kwargs["check"]:
            raise subprocess.CalledProcessError(1, args[0], stderr="invalid config")
        return subprocess.CompletedProcess(args[0], 0)

    monkeypatch.setattr(networkd.subprocess, "run", reload_fails)
    candidate = NetworkdConfiguration(
        files={"10-kavernis-new.network": "[Match]\nName=eth1\n\n[Network]\n"}
    )

    with pytest.raises(NetworkdApplyError, match="invalid config"):
        networkd.apply(candidate, tmp_path)

    assert managed.read_text(encoding="utf-8") == "old\n"
    assert stat.S_IMODE(managed.stat().st_mode) == 0o644
    assert not (tmp_path / "10-kavernis-new.network").exists()
    assert unmanaged.read_text(encoding="utf-8") == "keep\n"
    assert len(calls) == 2


def test_apply_installs_networkd_artifacts_with_explicit_readable_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from kavernis.backends import networkd

    monkeypatch.setattr(
        networkd.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0),
    )
    candidate = NetworkdConfiguration(
        files={
            "10-kavernis-lan.network": "[Match]\nName=eth1\n\n[Network]\n",
            "10-kavernis-guests.netdev": (
                "[NetDev]\nName=eth1.20\nKind=vlan\n\n[VLAN]\nId=20\n"
            ),
        }
    )

    networkd.apply(candidate, tmp_path)

    for filename in candidate.files:
        assert stat.S_IMODE((tmp_path / filename).stat().st_mode) == 0o644


def test_apply_networkd_mode_is_independent_of_umask(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from kavernis.backends import networkd

    monkeypatch.setattr(
        networkd.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0),
    )
    candidate = NetworkdConfiguration(
        files={"10-kavernis-lan.network": "[Match]\nName=eth1\n\n[Network]\n"}
    )
    old_umask = os.umask(0o077)
    try:
        networkd.apply(candidate, tmp_path)
    finally:
        os.umask(old_umask)

    assert stat.S_IMODE((tmp_path / "10-kavernis-lan.network").stat().st_mode) == 0o644


def test_apply_corrects_existing_artifact_permissions_with_atomic_replace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from kavernis.backends import networkd

    monkeypatch.setattr(
        networkd.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0),
    )
    artifact = tmp_path / "10-kavernis-lan.network"
    artifact.write_text("old\n", encoding="utf-8")
    artifact.chmod(0o600)
    previous_inode = artifact.stat().st_ino
    candidate = NetworkdConfiguration(
        files={"10-kavernis-lan.network": "[Match]\nName=eth1\n\n[Network]\n"}
    )

    networkd.apply(candidate, tmp_path)

    assert artifact.stat().st_ino != previous_inode
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o644


def test_drift_reports_missing_modified_and_unexpected_owned_files(tmp_path):
    from kavernis.backends.networkd import inspect_artifact_drift
    from kavernis.state.models import ArtifactDriftKind

    (tmp_path / "10-kavernis-modified.network").write_bytes(b"modified\xff\n")
    (tmp_path / "10-kavernis-unexpected.netdev").write_bytes(b"unexpected\n")
    (tmp_path / "20-unrelated.network").write_bytes(b"unrelated\n")
    observed = inspect_artifact_drift(
        {
            "10-kavernis-missing.network": "a" * 64,
            "10-kavernis-modified.network": "b" * 64,
        },
        tmp_path,
    )
    assert {item.filename: item.kind for item in observed} == {
        "10-kavernis-missing.network": ArtifactDriftKind.MISSING,
        "10-kavernis-modified.network": ArtifactDriftKind.MODIFIED,
        "10-kavernis-unexpected.netdev": ArtifactDriftKind.UNEXPECTED,
    }


@pytest.mark.parametrize(
    "content",
    [
        "[Match]\nName=*\n\n[Network]\n",
        "[Match]\nName=eth0\n\n[Network]\nAddress=999.0.0.1/24\n",
        "[Match]\nName=eth0\n\n[Network]\nUnknown=yes\n",
        "[Match]\nName=eth0\n\n[Network]\nDHCP=maybe\n",
    ],
)
def test_native_validation_rejects_unsafe_or_invalid_network_syntax(content):
    with pytest.raises(NetworkdValidationError):
        validate(NetworkdConfiguration({"10-kavernis-nic.network": content}))


def test_owned_symlink_is_rejected_without_touching_target(tmp_path, monkeypatch):
    from kavernis.backends import networkd

    outside = tmp_path / "unrelated.network"
    outside.write_text("keep\n")
    link = tmp_path / "10-kavernis-nic.network"
    link.symlink_to(outside)
    monkeypatch.setattr(
        networkd.subprocess, "run", lambda *_args, **_kwargs: pytest.fail()
    )
    with pytest.raises(NetworkdApplyError, match="not a regular file"):
        networkd.apply(
            NetworkdConfiguration(
                {
                    "10-kavernis-nic.network": "[Match]\nName=eth0\n\n[Network]\n",
                }
            ),
            tmp_path,
        )
    assert outside.read_text() == "keep\n"
    assert link.is_symlink()


def test_failed_artifact_recovery_is_reported(tmp_path, monkeypatch):
    from kavernis.backends import networkd

    (tmp_path / "10-kavernis-old.network").write_text("previous\n")

    def fail(*_args, **_kwargs):
        raise OSError("disk failed")

    monkeypatch.setattr(networkd, "_write_atomically", fail)
    with pytest.raises(NetworkdApplyError, match="artifact recovery failed"):
        networkd.apply(
            NetworkdConfiguration(
                {
                    "10-kavernis-nic.network": "[Match]\nName=eth0\n\n[Network]\n",
                }
            ),
            tmp_path,
        )


def test_failed_recovery_reload_is_reported(tmp_path, monkeypatch):
    from kavernis.backends import networkd

    def reload_fails(arguments, **kwargs):
        if kwargs["check"]:
            raise subprocess.CalledProcessError(
                1, arguments, stderr="activation failed"
            )
        return subprocess.CompletedProcess(arguments, 1, stderr="recovery failed")

    monkeypatch.setattr(networkd.subprocess, "run", reload_fails)
    with pytest.raises(
        NetworkdApplyError, match="recovery reload failed: recovery failed"
    ):
        networkd.apply(
            NetworkdConfiguration(
                {
                    "10-kavernis-nic.network": "[Match]\nName=eth0\n\n[Network]\n",
                }
            ),
            tmp_path,
        )


def test_reload_timeout_restores_artifacts_and_is_bounded(tmp_path, monkeypatch):
    from kavernis.backends import networkd

    candidate = NetworkdConfiguration(
        {
            "10-kavernis-nic.network": "[Match]\nName=eth0\n\n[Network]\n",
        }
    )
    calls = []

    def run(args, **kwargs):
        calls.append(kwargs)
        if kwargs["check"]:
            raise subprocess.TimeoutExpired(args, kwargs["timeout"])
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(NetworkdApplyError, match="native reload"):
        networkd.apply(candidate, tmp_path, reload_timeout=0.1)
    assert not list(tmp_path.iterdir())
    assert [call["timeout"] for call in calls] == [0.1, 0.1]


def test_interrupt_reading_artifact_baseline_does_not_start_recovery(
    tmp_path, monkeypatch
):
    from kavernis.backends import networkd

    interruption = KeyboardInterrupt()

    def cancel(*args):
        raise interruption

    monkeypatch.setattr(networkd, "_managed_files_bytes", cancel)
    monkeypatch.setattr(
        networkd, "_restore", lambda *a: pytest.fail("recovery before mutation")
    )
    with pytest.raises(KeyboardInterrupt) as caught:
        networkd.apply(
            NetworkdConfiguration(
                {
                    "10-kavernis-nic.network": "[Match]\nName=eth0\n\n[Network]\n",
                }
            ),
            tmp_path,
        )
    assert caught.value is interruption
    assert "before native mutation" in " ".join(interruption.__notes__)


def test_structural_lifecycle_uses_scoped_netlink_commands_in_order(
    tmp_path, monkeypatch
):
    from kavernis.backends import networkd

    calls = []
    links = {"eth0", "vlan100"}

    def run(arguments, **kwargs):
        calls.append(arguments)
        if arguments[:5] == ["ip", "-j", "-d", "link", "show"]:
            name = arguments[-1]
            return subprocess.CompletedProcess(
                arguments, 0 if name in links else 1, "", "Cannot find device"
            )
        if arguments[:3] == ["ip", "link", "delete"]:
            links.remove(arguments[-1])
        if arguments[:3] == ["ip", "link", "add"]:
            links.add(arguments[arguments.index("name") + 1])
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(networkd.subprocess, "run", run)
    plan = NetworkTransitionPlan(
        (),
        (
            TransitionOperation(
                TransitionOperationKind.DETACH,
                "vlan",
                "vlan100",
                "detach from bridge",
            ),
            TransitionOperation(
                TransitionOperationKind.DELETE,
                "vlan",
                "vlan100",
                "delete old VLAN",
            ),
            TransitionOperation(
                TransitionOperationKind.RELOAD,
                "networkd",
                "networkd",
                "reload target artifacts",
            ),
            TransitionOperation(
                TransitionOperationKind.CREATE,
                "vlan",
                "vlan100",
                "recreate VLAN",
            ),
        ),
        ("vlan100",),
        ("vlan100",),
        ("vlan100",),
    )
    networkd.apply(
        NetworkdConfiguration(
            {"10-kavernis-vlan.network": "[Match]\nName=vlan100\n\n[Network]\n"}
        ),
        tmp_path,
        transition_plan=plan,
        model=NetworkModel(
            (
                NetworkDevice("parent", "eth0", PhysicalSettings()),
                NetworkDevice("vlan", "vlan100", VLANSettings("parent", 100)),
            ),
            (),
        ),
    )
    assert calls == [
        ["networkctl", "reload", "--no-reconfigure"],
        ["ip", "link", "set", "dev", "vlan100", "nomaster"],
        ["ip", "link", "delete", "dev", "vlan100"],
        ["ip", "-j", "-d", "link", "show", "dev", "vlan100"],
        ["ip", "-j", "-d", "link", "show", "dev", "vlan100"],
        [
            "ip",
            "link",
            "add",
            "link",
            "eth0",
            "name",
            "vlan100",
            "type",
            "vlan",
            "id",
            "100",
        ],
        ["ip", "-j", "-d", "link", "show", "dev", "vlan100"],
        ["ip", "-j", "-d", "link", "show", "dev", "eth0"],
        ["networkctl", "reconfigure", "eth0"],
        ["ip", "-j", "-d", "link", "show", "dev", "vlan100"],
        ["networkctl", "reconfigure", "vlan100"],
    ]


def test_structural_recovery_removes_only_plan_owned_virtual_identities(
    tmp_path, monkeypatch
):
    from kavernis.backends import networkd

    calls = []
    links = {"old0"}

    def run(arguments, **kwargs):
        calls.append(arguments)
        if arguments[:5] == ["ip", "-j", "-d", "link", "show"]:
            name = arguments[-1]
            return subprocess.CompletedProcess(
                arguments, 0 if name in links else 1, "", "Cannot find device"
            )
        if arguments[:3] == ["ip", "link", "delete"]:
            links.discard(arguments[-1])
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(networkd.subprocess, "run", run)
    (tmp_path / "10-kavernis-old.network").write_text(
        "[Match]\nName=old0\n\n[Network]\n"
    )
    plan = NetworkTransitionPlan(
        (),
        (
            TransitionOperation(
                TransitionOperationKind.DELETE,
                "old",
                "old0",
                "replace old virtual",
            ),
        ),
        ("old0",),
        ("new0",),
        ("old0", "new0"),
    )
    with pytest.raises(RuntimeError, match="verification failed"):
        networkd.apply(
            NetworkdConfiguration(
                {"10-kavernis-new.network": "[Match]\nName=new0\n\n[Network]\n"}
            ),
            tmp_path,
            transition_plan=plan,
            model=NetworkModel(
                (NetworkDevice("parent", "eth0", PhysicalSettings()),), ()
            ),
            verify=lambda: (_ for _ in ()).throw(RuntimeError("verification failed")),
        )
    assert ["ip", "link", "delete", "dev", "old0"] in calls
    assert ["ip", "link", "delete", "dev", "new0"] not in calls
    assert (tmp_path / "10-kavernis-old.network").exists()
    assert not (tmp_path / "10-kavernis-new.network").exists()


def test_recovery_continues_to_reload_when_scoped_cleanup_fails(tmp_path, monkeypatch):
    from kavernis.backends import networkd

    calls = []
    links = {"old0"}
    deletions = 0

    def run(arguments, **kwargs):
        nonlocal deletions
        calls.append(arguments)
        if arguments[:5] == ["ip", "-j", "-d", "link", "show"]:
            name = arguments[-1]
            return subprocess.CompletedProcess(
                arguments, 0 if name in links else 1, "", "Cannot find device"
            )
        if arguments[:3] == ["ip", "link", "delete"]:
            deletions += 1
            if deletions == 1:
                links.discard(arguments[-1])
                return subprocess.CompletedProcess(arguments, 0, "", "")
            return subprocess.CompletedProcess(arguments, 1, "", "permission denied")
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(networkd.subprocess, "run", run)
    plan = NetworkTransitionPlan(
        (),
        (
            TransitionOperation(
                TransitionOperationKind.DELETE,
                "old",
                "old0",
                "remove old VLAN",
            ),
        ),
        ("old0",),
        (),
        ("old0",),
    )
    with pytest.raises(RuntimeError, match="candidate verification failed") as caught:
        networkd.apply(
            NetworkdConfiguration(
                {"10-kavernis-new.network": "[Match]\nName=eth0\n\n[Network]\n"}
            ),
            tmp_path,
            transition_plan=plan,
            model=NetworkModel(
                (NetworkDevice("parent", "eth0", PhysicalSettings()),), ()
            ),
            verify=lambda: (_ for _ in ()).throw(
                RuntimeError("candidate verification failed")
            ),
        )
    notes = " ".join(caught.value.__notes__)
    assert "scoped virtual cleanup failed" in notes
    assert "previous configuration reloaded" in notes
    assert calls[-1] == ["networkctl", "reload"]


def test_attachment_failure_does_not_delete_previously_applied_devices(
    tmp_path, monkeypatch
):
    from kavernis.backends import networkd

    calls = []

    def run(arguments, **kwargs):
        calls.append(arguments)
        if arguments[:5] == ["ip", "-j", "-d", "link", "show"]:
            return subprocess.CompletedProcess(arguments, 0, "", "")
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(networkd.subprocess, "run", run)
    plan = NetworkTransitionPlan(
        (),
        (
            TransitionOperation(
                TransitionOperationKind.ATTACH,
                "member",
                "eth1",
                "attach to existing bridge",
            ),
        ),
        ("br0",),
        ("br0",),
        (),
    )
    model = NetworkModel(
        (
            NetworkDevice("parent", "eth0", PhysicalSettings()),
            NetworkDevice("member", "eth1", PhysicalSettings()),
            NetworkDevice("bridge", "br0", BridgeSettings(("member",))),
        ),
        (),
    )
    with pytest.raises(RuntimeError, match="verification failed"):
        networkd.apply(
            NetworkdConfiguration(
                {"10-kavernis-bridge.network": "[Match]\nName=br0\n\n[Network]\n"}
            ),
            tmp_path,
            transition_plan=plan,
            model=model,
            verify=lambda: (_ for _ in ()).throw(RuntimeError("verification failed")),
        )
    assert not any(command[:3] == ["ip", "link", "delete"] for command in calls)


def test_recovery_never_deletes_a_candidate_that_appeared_without_our_creation(
    tmp_path, monkeypatch
):
    from kavernis.backends import networkd

    calls = []

    def run(arguments, **kwargs):
        calls.append(arguments)
        if arguments[:5] == ["ip", "-j", "-d", "link", "show"]:
            # Simulate an identity that appeared between planning and explicit
            # creation. Recovery must treat it as unproven ownership.
            return subprocess.CompletedProcess(arguments, 0, "", "")
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(networkd.subprocess, "run", run)
    plan = NetworkTransitionPlan(
        (),
        (
            TransitionOperation(
                TransitionOperationKind.CREATE,
                "candidate",
                "candidate0",
                "create candidate VLAN",
            ),
        ),
        (),
        ("candidate0",),
        ("candidate0",),
    )
    with pytest.raises(RuntimeError, match="verification failed"):
        networkd.apply(
            NetworkdConfiguration(
                {
                    "10-kavernis-candidate.network": (
                        "[Match]\nName=candidate0\n\n[Network]\n"
                    )
                }
            ),
            tmp_path,
            transition_plan=plan,
            model=NetworkModel(
                (
                    NetworkDevice("parent", "eth0", PhysicalSettings()),
                    NetworkDevice(
                        "candidate", "candidate0", VLANSettings("parent", 10)
                    ),
                ),
                (),
            ),
            verify=lambda: (_ for _ in ()).throw(RuntimeError("verification failed")),
        )
    assert ["ip", "link", "delete", "dev", "candidate0"] not in calls
