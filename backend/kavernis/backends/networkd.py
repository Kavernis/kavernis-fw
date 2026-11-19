"""Deterministic systemd-networkd configuration generation for network devices."""

import hashlib
import os
import re
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from ipaddress import ip_address, ip_interface, ip_network
from math import isfinite
from pathlib import Path
from typing import Protocol

from kavernis.backends.network_runtime import effective_onlink
from kavernis.core.network_transition import (
    NetworkTransitionPlan,
    TransitionOperationKind,
)
from kavernis.models.device import BridgeSettings, NetworkDevice, VLANSettings
from kavernis.models.interface import (
    InterfaceAddressing,
    IPv4AddressMode,
    IPv6AddressMode,
)
from kavernis.models.network import NetworkModel
from kavernis.rendering import render_template
from kavernis.state.models import (
    ApplyCommit,
    ArtifactDrift,
    ArtifactDriftKind,
    CommitPhase,
)

NETWORKD_FILE_MODE = 0o644
MANAGED_FILENAME = re.compile(r"10-kavernis-[a-z][a-z0-9-]*\.(network|netdev)")


class NetworkdValidationError(ValueError):
    """Raised when generated networkd configuration is unsafe or malformed."""


class NetworkdApplyError(RuntimeError):
    """Raised when a generated networkd candidate cannot be activated."""


class NativeNetworkdOperations:
    """The only production boundary that executes networkd/netlink commands.

    Commands deliberately operate on names emitted by a validated transition
    plan. The plan contains only managed virtual devices for delete operations;
    physical links are never removed or recreated.
    """

    def __init__(self, timeout: float) -> None:
        self.timeout = timeout

    def reload(
        self, *, check: bool, reconfigure: bool = True
    ) -> subprocess.CompletedProcess[str]:
        arguments = ["networkctl", "reload"]
        if not reconfigure:
            arguments.append("--no-reconfigure")
        return subprocess.run(
            arguments,
            check=check,
            capture_output=True,
            text=True,
            timeout=self.timeout,
        )

    def execute_lifecycle(
        self,
        plan: NetworkTransitionPlan,
        model: NetworkModel,
        progress: "NativeLifecycleProgress",
    ) -> None:
        for operation in plan.operations:
            if operation.kind is TransitionOperationKind.DETACH:
                self._ip(["link", "set", "dev", operation.device_name, "nomaster"])
            elif operation.kind is TransitionOperationKind.DELETE:
                self._ip(["link", "delete", "dev", operation.device_name])
                progress.deleted_names.add(operation.device_name)
                self.wait_for_link(operation.device_name, present=False)

        devices = {device.id: device for device in model.devices}
        for operation in plan.operations:
            if operation.kind is not TransitionOperationKind.CREATE:
                continue
            device = devices[operation.device_id]
            # A config-only reload may already have created a previously absent
            # target netdev. It is usable for this candidate, but it was not
            # created by this invocation's explicit netlink action. Do not add
            # it to automatic recovery cleanup: a concurrent foreign creator
            # must never be deleted merely because it has a desired name.
            if self.link_exists(device.name):
                continue
            if isinstance(device.settings, VLANSettings):
                parent = devices[device.settings.parent]
                self._ip(
                    [
                        "link",
                        "add",
                        "link",
                        parent.name,
                        "name",
                        device.name,
                        "type",
                        "vlan",
                        "id",
                        str(device.settings.tag),
                    ]
                )
            elif isinstance(device.settings, BridgeSettings):
                self._ip(
                    [
                        "link",
                        "add",
                        "name",
                        device.name,
                        "type",
                        "bridge",
                        "stp_state",
                        "1" if device.settings.stp else "0",
                    ]
                )
            else:  # Defensive: transition planning must never create physical links.
                raise NetworkdApplyError(
                    f"cannot create physical device {device.name} through networkd"
                )
            progress.created_names.add(device.name)
            self.wait_for_link(device.name, present=True)

        for operation in plan.operations:
            if operation.kind is not TransitionOperationKind.ATTACH:
                continue
            bridge = _bridge_for_member(model, operation.device_id)
            if bridge is None:
                raise NetworkdApplyError(
                    f"no target bridge owns member {operation.device_name}"
                )
            self.wait_for_link(operation.device_name, present=True)
            self.wait_for_link(bridge.name, present=True)
            self._ip(
                ["link", "set", "dev", operation.device_name, "master", bridge.name]
            )

    def reconfigure_target(
        self, plan: NetworkTransitionPlan, model: NetworkModel
    ) -> None:
        devices = {device.id: device for device in model.devices}
        names: set[str] = set()
        for operation in plan.operations:
            if operation.kind is TransitionOperationKind.CREATE:
                names.add(operation.device_name)
                device = devices[operation.device_id]
                if isinstance(device.settings, VLANSettings):
                    names.add(devices[device.settings.parent].name)
            elif (
                operation.kind
                in (
                    TransitionOperationKind.ATTACH,
                    TransitionOperationKind.DETACH,
                )
                and operation.device_id in devices
            ):
                names.add(operation.device_name)
        for name in sorted(names):
            self.wait_for_link(name, present=True)
            subprocess.run(
                ["networkctl", "reconfigure", name],
                check=True,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )

    def remove_recovery_identities(self, names: set[str]) -> None:
        failures: list[str] = []
        for name in sorted(names):
            result = subprocess.run(
                ["ip", "link", "delete", "dev", name],
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
            if result.returncode and not _missing_link(result.stderr):
                failures.append(f"{name}: {result.stderr.strip() or result.returncode}")
        if failures:
            raise NetworkdApplyError(
                "cannot remove managed virtual device(s) during recovery: "
                + "; ".join(failures)
            )

    def wait_for_link(self, name: str, *, present: bool) -> None:
        deadline = time.monotonic() + self.timeout
        last_error = "no observation"
        while True:
            exists = self.link_exists(
                name, timeout=max(0.1, min(self.timeout, deadline - time.monotonic()))
            )
            if exists == present:
                return
            last_error = "link exists" if exists else "link is absent"
            if time.monotonic() >= deadline:
                state = "appear" if present else "disappear"
                raise NetworkdApplyError(
                    f"timed out waiting for {name} to {state}: {last_error}"
                )
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))

    def link_exists(self, name: str, *, timeout: float | None = None) -> bool:
        result = subprocess.run(
            ["ip", "-j", "-d", "link", "show", "dev", name],
            check=False,
            capture_output=True,
            text=True,
            timeout=self.timeout if timeout is None else timeout,
        )
        if result.returncode == 0:
            return True
        if _missing_link(result.stderr):
            return False
        raise NetworkdApplyError(
            f"cannot inspect link {name}: {result.stderr.strip() or result.returncode}"
        )

    def _ip(self, arguments: list[str]) -> None:
        subprocess.run(
            ["ip", *arguments],
            check=True,
            capture_output=True,
            text=True,
            timeout=self.timeout,
        )


def _missing_link(stderr: str) -> bool:
    return "Cannot find device" in stderr or "does not exist" in stderr


@dataclass
class NativeLifecycleProgress:
    """Facts established by this invocation, used to scope recovery cleanup."""

    deleted_names: set[str] = field(default_factory=set)
    created_names: set[str] = field(default_factory=set)

    @property
    def recovery_cleanup_names(self) -> set[str]:
        return self.deleted_names | self.created_names


def _bridge_for_member(model: NetworkModel, member_id: str) -> NetworkDevice | None:
    return next(
        (
            device
            for device in model.devices
            if isinstance(device.settings, BridgeSettings)
            and member_id in device.settings.members
        ),
        None,
    )


@dataclass(frozen=True)
class NetworkdConfiguration:
    """Generated files, kept in memory until a future safe apply operation."""

    files: Mapping[str, str]


class NetworkBackend(Protocol):
    """Minimal boundary used by network-state orchestration.

    The sole implementation is Debian 13's systemd-networkd backend. Keeping
    this protocol small prevents native command details from leaking into the
    domain/state layer without creating a runtime plugin framework.
    """

    def artifact_hashes(self, candidate: NetworkdConfiguration) -> dict[str, str]: ...

    def inspect_artifact_drift(
        self, expected_hashes: Mapping[str, str], directory: Path
    ) -> tuple[ArtifactDrift, ...]: ...

    def activate(
        self,
        candidate: NetworkdConfiguration,
        directory: Path,
        *,
        model: NetworkModel,
        verify: Callable[[], None] | None,
        verify_recovery: Callable[[], None] | None,
        record: Callable[[], None] | None,
        commit: ApplyCommit | None,
        reload_timeout: float,
        transition_plan: NetworkTransitionPlan | None,
    ) -> None: ...


class SystemdNetworkdBackend:
    """Small production backend facade for Debian 13 systemd-networkd.

    The domain and transition planner pass backend-neutral models/plans here;
    this implementation alone owns networkd rendering, artifact activation, and
    the scoped netlink lifecycle commands.
    """

    def __init__(self, activation: Callable[..., None] | None = None) -> None:
        # The optional composition hook keeps the public low-level ``apply``
        # primitive independently testable without adding a second backend.
        self._activation = activation

    def render(self, model: NetworkModel) -> NetworkdConfiguration:
        return generate(model)

    def validate(self, candidate: NetworkdConfiguration) -> None:
        validate(candidate)

    def artifact_hashes(self, candidate: NetworkdConfiguration) -> dict[str, str]:
        return artifact_hashes(candidate)

    def inspect_artifact_drift(
        self, expected_hashes: Mapping[str, str], directory: Path
    ) -> tuple[ArtifactDrift, ...]:
        return inspect_artifact_drift(expected_hashes, directory)

    def activate(
        self,
        candidate: NetworkdConfiguration,
        directory: Path,
        *,
        model: NetworkModel,
        verify: Callable[[], None] | None,
        verify_recovery: Callable[[], None] | None,
        record: Callable[[], None] | None,
        commit: ApplyCommit | None,
        reload_timeout: float,
        transition_plan: NetworkTransitionPlan | None,
    ) -> None:
        activation = self._activation or apply
        activation(
            candidate,
            directory,
            model=model,
            verify=verify,
            verify_recovery=verify_recovery,
            record=record,
            commit=commit,
            reload_timeout=reload_timeout,
            transition_plan=transition_plan,
        )


@dataclass(frozen=True)
class NetworkTemplateContext:
    """Fully resolved values needed to render one systemd-networkd file."""

    device: str
    addresses: tuple[str, ...]
    dhcp: str | None
    dhcp_v4: "DHCPv4TemplateContext | None"
    ipv6_accept_ra: str | None
    link_local_addressing: str | None
    vlans: tuple[str, ...]
    bridge: str | None
    routes: tuple["RouteTemplateContext", ...]


@dataclass(frozen=True)
class RouteTemplateContext:
    """Native syntax values for one fully resolved static route."""

    destination: str
    gateway: str | None
    gateway_onlink: str | None


@dataclass(frozen=True)
class DHCPv4TemplateContext:
    """Explicit native DHCPv4 policy derived from Kavernis domain intent."""

    use_hostname: str
    send_hostname: str
    use_dns: str
    use_routes: str
    use_ntp: str
    route_metric: int | None


@dataclass(frozen=True)
class VlanNetdevTemplateContext:
    """Fully resolved values needed to render one VLAN .netdev file."""

    device: str
    tag: int


@dataclass(frozen=True)
class BridgeNetdevTemplateContext:
    """Fully resolved values needed to render one bridge .netdev file."""

    device: str
    stp: str


def generate(model: NetworkModel) -> NetworkdConfiguration:
    """Render a resolved network model, with one native configuration per device."""
    interfaces = {interface.device: interface for interface in model.interfaces}
    children: dict[str, list[str]] = {device.id: [] for device in model.devices}
    bridges: dict[str, str] = {}
    for device in model.devices:
        if isinstance(device.settings, VLANSettings):
            children[device.settings.parent].append(device.name)
        elif isinstance(device.settings, BridgeSettings):
            for member in device.settings.members:
                bridges[member] = device.name

    gateways = {gateway.id: gateway for gateway in model.routing.gateways}
    routes: dict[str, list[RouteTemplateContext]] = {
        interface.id: [] for interface in model.interfaces
    }
    for route in sorted(model.routing.routes, key=lambda item: item.id):
        gateway = gateways[route.gateway]
        routes[gateway.interface].append(
            RouteTemplateContext(
                destination=str(route.network),
                gateway=str(gateway.address) if gateway.address is not None else None,
                gateway_onlink=(
                    "yes"
                    if effective_onlink(
                        next(i for i in model.interfaces if i.id == gateway.interface),
                        gateway,
                    )
                    else None
                ),
            )
        )

    files: dict[str, str] = {}
    for device in sorted(model.devices, key=lambda item: item.id):
        interface = interfaces.get(device.id)
        stem = f"10-kavernis-{device.id}"
        files[f"{stem}.network"] = _render_network(
            device.name,
            interface.addressing if interface else InterfaceAddressing(),
            sorted(children[device.id]),
            bridges.get(device.id),
            tuple(routes[interface.id]) if interface else (),
        )
        if isinstance(device.settings, VLANSettings):
            files[f"{stem}.netdev"] = render_template(
                "network/vlan.netdev.j2",
                asdict(VlanNetdevTemplateContext(device.name, device.settings.tag)),
            )
        elif isinstance(device.settings, BridgeSettings):
            files[f"{stem}.netdev"] = render_template(
                "network/bridge.netdev.j2",
                asdict(
                    BridgeNetdevTemplateContext(
                        device.name,
                        "yes" if device.settings.stp else "no",
                    )
                ),
            )
    candidate = NetworkdConfiguration(files=files)
    validate(candidate)
    return candidate


def validate(candidate: NetworkdConfiguration) -> None:
    """Validate the generated networkd candidate before any future apply step.

    systemd-networkd has no standalone, side-effect-free syntax verifier for
    .network files. This validation therefore verifies the constrained native
    syntax generated by this backend. Native service validation belongs to the
    controlled integration/apply environment.
    """
    if not candidate.files:
        raise NetworkdValidationError(
            "networkd candidate must contain at least one file"
        )

    for filename, content in candidate.files.items():
        if not MANAGED_FILENAME.fullmatch(filename):
            raise NetworkdValidationError(f"unsafe networkd filename: {filename}")
        if "/" in filename or "\\" in filename:
            raise NetworkdValidationError(f"unsafe networkd filename: {filename}")
        if filename.endswith(".netdev"):
            if "\nKind=bridge\n" in content:
                _validate_bridge_netdev(filename, content)
            else:
                _validate_vlan_netdev(filename, content)
        else:
            _validate_network(filename, content)
        if not content.endswith("\n"):
            raise NetworkdValidationError(
                f"networkd content must end with newline: {filename}"
            )


def apply(
    candidate: NetworkdConfiguration,
    directory: Path = Path("/etc/systemd/network"),
    *,
    model: NetworkModel | None = None,
    verify: Callable[[], None] | None = None,
    verify_recovery: Callable[[], None] | None = None,
    record: Callable[[], None] | None = None,
    commit: ApplyCommit | None = None,
    reload_timeout: float = 15.0,
    transition_plan: NetworkTransitionPlan | None = None,
) -> None:
    """Install/reload within a narrow recovery boundary, then verify and record.

    Without callbacks this is an artifact activation primitive, not evidence of
    runtime convergence. Core supplies verification and durable-record callbacks.
    """
    validate(candidate)
    if not isfinite(reload_timeout) or reload_timeout <= 0:
        raise ValueError("reload timeout must be positive")
    try:
        previous = _managed_files_bytes(directory)
    except OSError as error:
        raise NetworkdApplyError(f"cannot read managed artifacts: {error}") from error
    except BaseException as error:
        error.add_note("apply interrupted before native mutation; recovery not needed")
        raise
    phase = "artifact mutation"
    operations = NativeNetworkdOperations(reload_timeout)
    lifecycle = NativeLifecycleProgress()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        for filename, content in candidate.files.items():
            _write_atomically(directory / filename, content)
        for filename in sorted(set(previous) - set(candidate.files)):
            (directory / filename).unlink()
        _sync_directory(directory)
        if transition_plan is not None and transition_plan.has_structural_operations:
            if model is None:
                raise ValueError(
                    "structural network transition requires a target model"
                )
            phase = "load target networkd configuration"
            # systemd 257 does not apply immutable netdev changes to an existing
            # link. Load the candidate without broad L3 reconfiguration, then
            # perform exact scoped lifecycle actions below.
            operations.reload(check=True, reconfigure=False)
            phase = "native device lifecycle"
            operations.execute_lifecycle(transition_plan, model, lifecycle)
            phase = "target interface reconfiguration"
            operations.reconfigure_target(transition_plan, model)
        else:
            phase = "native reload"
            operations.reload(check=True)
        phase = "runtime verification"
        if verify is not None:
            verify()
        phase = "applied-state recording"
        if record is not None:
            record()
    except BaseException as error:
        # Deliberately narrow: after persistent mutation starts, even cancellation
        # owes best-effort restoration. Never replace the original cancellation.
        if commit is not None and commit.phase is CommitPhase.COMMITTED:
            error.add_note(
                "interrupted after verified success was durably recorded; "
                "native configuration and successful outcome retained"
            )
            raise
        if commit is not None and commit.phase is CommitPhase.COMMIT_ATTEMPTED:
            error.add_note(
                "durable recording outcome is uncertain after commit was attempted; "
                "native recovery not attempted; inspect SQLite and Linux"
            )
            raise
        recovery = _recover(
            directory,
            previous,
            verify_recovery,
            operations,
            lifecycle,
        )
        detail = f"apply failed during {phase}: {error}; {recovery}"
        if isinstance(error, subprocess.CalledProcessError):
            detail += f"; {error.stderr}"
        if isinstance(error, (OSError, subprocess.SubprocessError)):
            raise NetworkdApplyError(detail) from error
        error.add_note(detail)
        raise


def _recover(
    directory: Path,
    previous: Mapping[str, bytes],
    verify: Callable[[], None] | None,
    operations: NativeNetworkdOperations,
    lifecycle: NativeLifecycleProgress,
) -> str:
    """Best effort cleanup, reporting each boundary without losing the cause."""
    try:
        _restore(directory, previous)
    except BaseException as error:
        return f"artifact recovery failed: {type(error).__name__}: {error}"
    details = ["previous files restored"]
    if lifecycle.recovery_cleanup_names:
        try:
            operations.remove_recovery_identities(lifecycle.recovery_cleanup_names)
            details.append("scoped virtual cleanup completed")
        except BaseException as error:
            details.append(f"scoped virtual cleanup failed: {error}")
    try:
        recovery = operations.reload(check=False)
        if recovery.returncode:
            details.append(
                "recovery reload failed: "
                f"{recovery.stderr.strip() or recovery.returncode}"
            )
            return "; ".join(details)
        details.append("previous configuration reloaded")
    except BaseException as error:
        details.append(f"recovery reload failed: {error}")
        return "; ".join(details)
    if verify is None:
        return "; ".join([*details, "runtime restoration unverified"])
    try:
        verify()
    except BaseException as error:
        return "; ".join(
            [*details, f"runtime recovery failed: {type(error).__name__}: {error}"]
        )
    return "; ".join([*details, "required static runtime restoration verified"])


def artifact_hashes(candidate: NetworkdConfiguration) -> dict[str, str]:
    """Return deterministic SHA-256 hashes for a validated managed candidate."""
    validate(candidate)
    return {
        filename: hashlib.sha256(content.encode("utf-8")).hexdigest()
        for filename, content in sorted(candidate.files.items())
    }


def inspect_artifact_drift(
    expected_hashes: Mapping[str, str],
    directory: Path = Path("/etc/systemd/network"),
) -> tuple[ArtifactDrift, ...]:
    """Compare only Kavernis-owned networkd artifacts with applied hashes."""
    try:
        actual = _managed_files_bytes(directory)
    except OSError as error:
        raise NetworkdApplyError(
            f"cannot inspect networkd artifacts: {error}"
        ) from error
    drift: list[ArtifactDrift] = []
    for filename, expected_hash in sorted(expected_hashes.items()):
        contents = actual.pop(filename, None)
        if contents is None:
            drift.append(ArtifactDrift(filename, ArtifactDriftKind.MISSING))
        elif hashlib.sha256(contents).hexdigest() != expected_hash:
            drift.append(ArtifactDrift(filename, ArtifactDriftKind.MODIFIED))
    drift.extend(
        ArtifactDrift(filename, ArtifactDriftKind.UNEXPECTED)
        for filename in sorted(actual)
    )
    return tuple(drift)


def _managed_files_bytes(directory: Path) -> dict[str, bytes]:
    """Read only regular owned artifacts, preserving exact bytes for recovery."""
    if not directory.exists():
        return {}
    result: dict[str, bytes] = {}
    for path in directory.iterdir():
        if MANAGED_FILENAME.fullmatch(path.name):
            if path.is_symlink() or not path.is_file():
                raise OSError(f"managed artifact is not a regular file: {path}")
            result[path.name] = path.read_bytes()
    return result


def _write_atomically(path: Path, content: str | bytes) -> None:
    """Replace one native configuration file without exposing partial content.

    Native networkd artifacts must be world-readable irrespective of the
    process umask, tempfile defaults, or an existing artifact's mode.
    """
    descriptor, temporary_name = tempfile.mkstemp(prefix=".kavernis-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as temporary:
            temporary.write(
                content.encode("utf-8") if isinstance(content, str) else content
            )
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_name, NETWORKD_FILE_MODE)
        os.replace(temporary_name, path)
    except BaseException as error:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        except OSError as cleanup_error:
            error.add_note(f"temporary-file cleanup failed: {cleanup_error}")
        raise


def _sync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _restore(directory: Path, previous: Mapping[str, bytes]) -> None:
    """Atomically restore prior files, then remove newly introduced artifacts."""
    if not directory.exists():
        return
    for filename, content in previous.items():
        _write_atomically(directory / filename, content)
    for filename in sorted(set(_managed_files_bytes(directory)) - set(previous)):
        (directory / filename).unlink()
    _sync_directory(directory)


def _validate_network(filename: str, content: str) -> None:
    """Accept only the constrained syntax emitted by the packaged template."""
    pattern = (
        r"\[Match\]\nName=[A-Za-z0-9_.:-]+\n\n\[Network\]\n"
        r"(?:Address=[A-Za-z0-9_.:%/-]+\n)*"
        r"(?:IPv6AcceptRA=(?:yes|no)\n)?"
        r"(?:DHCP=(?:yes|no|ipv4|ipv6)\n)?"
        r"(?:LinkLocalAddressing=(?:no|ipv6)\n)?"
        r"(?:VLAN=[A-Za-z0-9_.-]{1,15}\n)*"
        r"(?:Bridge=[A-Za-z0-9_.-]{1,15}\n)?"
        r"(?:\n\[DHCPv4\]\nUseHostname=(?:yes|no)\n"
        r"SendHostname=(?:yes|no)\nUseDNS=(?:yes|no)\n"
        r"UseRoutes=(?:yes|no)\nUseNTP=(?:yes|no)\n(?:RouteMetric=[0-9]+\n)?)?"
        r"(?:\n\[Route\]\nDestination=[A-Za-z0-9_.:%/-]+\n"
        r"(?:Gateway=[A-Za-z0-9_.:%-]+\n)?(?:GatewayOnLink=yes\n)?)*"
    )
    if re.fullmatch(pattern, content) is None:
        raise NetworkdValidationError(f"invalid networkd content for {filename}")
    try:
        for line in content.splitlines():
            key, _, value = line.partition("=")
            if key == "Address":
                ip_interface(value)
            elif key == "Destination":
                ip_network(value)
            elif key == "Gateway":
                ip_address(value)
            elif key == "RouteMetric" and not 0 <= int(value) <= 4294967295:
                raise ValueError("invalid DHCP route metric")
    except ValueError as error:
        raise NetworkdValidationError(
            f"invalid networkd content for {filename}: {error}"
        ) from error


def _validate_vlan_netdev(filename: str, content: str) -> None:
    match = re.fullmatch(
        r"\[NetDev\]\nName=([A-Za-z0-9_.-]{1,15})\nKind=vlan\n\n"
        r"\[VLAN\]\nId=([0-9]+)\n",
        content,
    )
    if match is None or match[1] in {".", ".."} or not 1 <= int(match[2]) <= 4094:
        raise NetworkdValidationError(f"invalid VLAN netdev content for {filename}")


def _validate_bridge_netdev(filename: str, content: str) -> None:
    match = re.fullmatch(
        r"\[NetDev\]\nName=([A-Za-z0-9_.-]{1,15})\nKind=bridge\n\n"
        r"\[Bridge\]\nSTP=(yes|no)\n",
        content,
    )
    if match is None or match[1] in {".", ".."}:
        raise NetworkdValidationError(f"invalid bridge netdev content for {filename}")


def _render_network(
    device_name: str,
    addressing: InterfaceAddressing,
    vlans: Sequence[str] = (),
    bridge: str | None = None,
    routes: tuple[RouteTemplateContext, ...] = (),
) -> str:
    """Resolve network intent into the limited context used by the template."""
    addresses: list[str] = []
    ipv6_accept_ra: str | None = None

    if addressing.ipv4.mode is IPv4AddressMode.STATIC:
        if addressing.ipv4.address is None:
            raise NetworkdValidationError(
                f"static IPv4 interface {device_name} has no address"
            )
        addresses.append(str(addressing.ipv4.address))

    if addressing.ipv6.mode is IPv6AddressMode.STATIC:
        if addressing.ipv6.address is None:
            raise NetworkdValidationError(
                f"static IPv6 interface {device_name} has no address"
            )
        addresses.append(str(addressing.ipv6.address))
        ipv6_accept_ra = "no"
    elif addressing.ipv6.mode is IPv6AddressMode.SLAAC:
        ipv6_accept_ra = "yes"
    elif addressing.ipv6.mode is IPv6AddressMode.DISABLED:
        ipv6_accept_ra = "no"

    dhcp: str | None = "no"
    dhcp_v4: DHCPv4TemplateContext | None = None
    if (
        addressing.ipv4.mode is IPv4AddressMode.DHCP
        and addressing.ipv6.mode is IPv6AddressMode.DHCP6
    ):
        dhcp = "yes"
    elif addressing.ipv4.mode is IPv4AddressMode.DHCP:
        dhcp = "ipv4"
    elif addressing.ipv6.mode is IPv6AddressMode.DHCP6:
        dhcp = "ipv6"

    if addressing.ipv4.mode is IPv4AddressMode.DHCP:
        if addressing.ipv4.dhcp is None:
            raise NetworkdValidationError(
                f"DHCP IPv4 interface {device_name} has no DHCPv4 policy"
            )
        dhcp_v4 = DHCPv4TemplateContext(
            use_hostname="yes" if addressing.ipv4.dhcp.use_hostname else "no",
            send_hostname="yes" if addressing.ipv4.dhcp.send_hostname else "no",
            use_dns="yes" if addressing.ipv4.dhcp.use_dns else "no",
            use_routes="yes" if addressing.ipv4.dhcp.use_routes else "no",
            use_ntp="yes" if addressing.ipv4.dhcp.use_ntp else "no",
            route_metric=addressing.ipv4.dhcp.route_metric,
        )

    link_local_addressing = (
        "no" if addressing.ipv6.mode is IPv6AddressMode.DISABLED else "ipv6"
    )

    context = NetworkTemplateContext(
        device=device_name,
        addresses=tuple(addresses),
        dhcp=dhcp,
        dhcp_v4=dhcp_v4,
        ipv6_accept_ra=ipv6_accept_ra,
        link_local_addressing=link_local_addressing,
        vlans=tuple(vlans),
        bridge=bridge,
        routes=routes,
    )
    return render_template("network/network.j2", asdict(context))
