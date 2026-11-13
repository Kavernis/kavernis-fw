"""Deterministic systemd-networkd configuration generation for network devices."""

import hashlib
import os
import re
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from ipaddress import ip_address, ip_interface, ip_network
from pathlib import Path

from kavernis.models.device import BridgeSettings, VLANSettings
from kavernis.models.interface import (
    InterfaceAddressing,
    IPv4AddressMode,
    IPv6AddressMode,
)
from kavernis.models.network import NetworkModel
from kavernis.rendering import render_template
from kavernis.state.models import ArtifactDrift, ArtifactDriftKind

NETWORKD_FILE_MODE = 0o644
MANAGED_FILENAME = re.compile(r"10-kavernis-[a-z][a-z0-9-]*\.(network|netdev)")


class NetworkdValidationError(ValueError):
    """Raised when generated networkd configuration is unsafe or malformed."""


class NetworkdApplyError(RuntimeError):
    """Raised when a generated networkd candidate cannot be activated."""


@dataclass(frozen=True)
class NetworkdConfiguration:
    """Generated files, kept in memory until a future safe apply operation."""

    files: Mapping[str, str]


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
                gateway_onlink="yes" if gateway.onlink else None,
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
) -> None:
    """Install a validated candidate and ask systemd-networkd to reload it.

    Only Kavernis-owned files are replaced or removed. If networkctl cannot
    activate the candidate, the previously managed files are restored before
    the error is reported.
    """
    validate(candidate)
    try:
        previous = _managed_files_bytes(directory)
    except OSError as error:
        raise NetworkdApplyError(f"cannot read managed artifacts: {error}") from error
    try:
        directory.mkdir(parents=True, exist_ok=True)
        for filename, content in candidate.files.items():
            _write_atomically(directory / filename, content)
        for filename in sorted(set(previous) - set(candidate.files)):
            (directory / filename).unlink()
        _sync_directory(directory)
        subprocess.run(
            ["networkctl", "reload"], check=True, capture_output=True, text=True
        )
    except (OSError, subprocess.CalledProcessError) as error:
        try:
            _restore(directory, previous)
        except OSError as restore_error:
            raise NetworkdApplyError(
                f"native apply failed ({error}); artifact recovery failed: "
                f"{restore_error}"
            ) from restore_error
        recovery_detail = ""
        try:
            recovery = subprocess.run(
                ["networkctl", "reload"], check=False, capture_output=True, text=True
            )
            if recovery.returncode:
                recovery_detail = (
                    "; previous files restored but recovery reload failed: "
                    f"{recovery.stderr.strip() or recovery.returncode}"
                )
        except OSError as recovery_error:
            recovery_detail = (
                "; previous files restored but recovery reload failed: "
                f"{recovery_error}"
            )
        detail = str(error)
        if isinstance(error, subprocess.CalledProcessError):
            detail = str(error.stderr).strip()
        raise NetworkdApplyError(
            f"cannot apply networkd configuration: {detail}{recovery_detail}"
        ) from error


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
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
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
