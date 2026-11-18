"""Command line interface for Kavernis desired-state operations."""

import argparse
import os
import shlex
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from kavernis.backends.network_runtime import RuntimeVerificationError
from kavernis.backends.networkd import NetworkdApplyError, NetworkdConfiguration
from kavernis.config.errors import ConfigurationError
from kavernis.core.network import (
    NETWORK_DOMAIN,
    DesiredStateChangedError,
    EditorFailedError,
    NetworkStatePaths,
    NetworkStateService,
    NetworkStatus,
    load_network,
)
from kavernis.core.network_transition import NetworkTransitionPlan
from kavernis.state.history import HistoryError
from kavernis.state.lock import LockUnavailableError
from kavernis.state.models import ApplyOutcomeKind
from kavernis.state.sqlite import StateStoreError

DEVICES_PATH = "/etc/kavernis/network/devices.yaml"
INTERFACES_PATH = "/etc/kavernis/network/interfaces.yaml"
ROUTING_PATH = "/etc/kavernis/network/routing.yaml"
HISTORY_PATH = "/var/lib/kavernis/history"
STATE_DATABASE_PATH = "/var/lib/kavernis/state.db"
NETWORKD_PATH = "/etc/systemd/network"
STATE_LOCK_PATH = "/run/kavernis/network.lock"


def main(arguments: Sequence[str] | None = None) -> int:
    """Run the Kavernis command line interface."""
    parser = argparse.ArgumentParser(prog="kavernis")
    action = parser.add_subparsers(dest="action", required=True)
    for name, help_text in (
        ("plan", "generate and validate configuration without applying it"),
        ("apply", "generate, validate, and apply configuration"),
        ("status", "show desired, applied, and artifact state"),
        ("history", "show desired-state revision history"),
    ):
        command = action.add_parser(name, help=help_text)
        command.add_argument("resource", choices=("network",))
    edit_command = action.add_parser(
        "edit", help="safely edit one network desired-state resource"
    )
    edit_command.add_argument("resource", choices=("devices", "interfaces", "routing"))
    diff_command = action.add_parser("diff", help="show desired-state differences")
    diff_command.add_argument("resource", choices=("network",))
    diff_command.add_argument("revision", nargs="?")
    rollback_command = action.add_parser(
        "rollback", help="restore and apply a revision"
    )
    rollback_command.add_argument("resource", choices=("network",))
    rollback_command.add_argument("revision")

    parsed = parser.parse_args(arguments)
    try:
        service = _state_service()
        if parsed.action == "history":
            for history_revision in service.history_revisions():
                print(
                    f"{history_revision.identifier[:12]} "
                    f"{history_revision.created_at.isoformat()} "
                    f"{history_revision.message}"
                )
        elif parsed.action == "rollback":
            candidate = service.rollback(parsed.revision)
            print(
                "Rolled back and applied "
                f"{len(candidate.files)} systemd-networkd file(s)."
            )
            print(
                "Configuration activation and static runtime verified; "
                "dynamic acquisition and connectivity are not verified."
            )
        elif parsed.action == "edit":
            changed = service.edit_resource(
                parsed.resource, _invoke_editor, _retry_invalid_edit
            )
            print("Updated desired state." if changed else "No changes.")
        elif parsed.action == "apply":
            candidate = service.apply()
            print(f"Applied {len(candidate.files)} systemd-networkd file(s).")
            print(
                "Configuration activation and static runtime verified; "
                "dynamic acquisition and connectivity are not verified."
            )
        else:
            desired = load_network(service.paths)
            if parsed.action == "plan":
                candidate, transition = service.preview(desired)
                _print_candidate(candidate, transition)
            else:
                if parsed.action == "status":
                    _print_status(service.status(desired.files))
                else:
                    applied = service.store.get_domain_state(NETWORK_DOMAIN)
                    revision = parsed.revision or (
                        applied.revision if applied else None
                    )
                    if revision is None:
                        raise ValueError(
                            "network has not yet been successfully applied"
                        )
                    print(service.diff(desired.files, revision), end="")
    except (
        ConfigurationError,
        HistoryError,
        NetworkdApplyError,
        StateStoreError,
        RuntimeVerificationError,
        LockUnavailableError,
        DesiredStateChangedError,
        EditorFailedError,
        ValueError,
        OSError,
    ) as error:
        print(f"Error: {error}", file=sys.stderr)
        for note in getattr(error, "__notes__", []):
            print(note, file=sys.stderr)
        return 1
    except KeyboardInterrupt as error:
        print("Interrupted.", file=sys.stderr)
        for note in getattr(error, "__notes__", []):
            print(note, file=sys.stderr)
        return 1
    except SystemExit as error:
        for note in getattr(error, "__notes__", []):
            print(note, file=sys.stderr)
        raise
    return 0


def _print_candidate(
    candidate: NetworkdConfiguration,
    transition: NetworkTransitionPlan | None = None,
) -> None:
    """Render a deterministic, reviewable plan to standard output."""
    if transition is not None:
        print("Network transition plan:")
        if transition.operations:
            for operation in transition.operations:
                print(
                    f"  {operation.kind.value.upper()} {operation.device_id} "
                    f"({operation.device_name}): {operation.detail}"
                )
        else:
            print("  RELOAD networkd artifacts and L3 configuration")
        print()
        mode = (
            "LIVE STRUCTURAL RECONFIGURATION"
            if transition.has_structural_operations
            else "NETWORKD RELOAD"
        )
        print(f"Apply mode: {mode}")
        print("Reboot required: no")
        print()
    for index, (filename, content) in enumerate(sorted(candidate.files.items())):
        if index:
            print()
        print(f"--- {filename}")
        print(content, end="")


def _editor_command() -> list[str]:
    """Select a conventional editor command without using a shell."""
    configured = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "nano"
    command = shlex.split(configured)
    if not command:
        raise ValueError("VISUAL or EDITOR does not contain an editor command")
    return command


def _invoke_editor(path: Path) -> int:
    """Run the selected editor on a controlled temporary YAML file."""
    try:
        completed = subprocess.run([*_editor_command(), str(path)], check=False)
    except OSError as error:
        raise EditorFailedError(f"cannot start editor: {error}") from error
    return completed.returncode


def _retry_invalid_edit(error: ConfigurationError) -> bool:
    """Offer the CLI's visudo-like retry/abort choice after validation fails."""
    print(f"Configuration is invalid:\n\n{error}\n", file=sys.stderr)
    while True:
        try:
            choice = input("What now? (e) edit again, (x) exit without saving [e]: ")
        except EOFError:
            return False
        choice = choice.strip().lower()
        if choice in ("", "e", "edit"):
            return True
        if choice in ("x", "exit"):
            return False
        print("Please choose 'e' or 'x'.", file=sys.stderr)


def _state_service() -> NetworkStateService:
    """Construct dependencies at the CLI composition boundary."""
    return NetworkStateService(
        NetworkStatePaths(
            interfaces_path=Path(INTERFACES_PATH),
            devices_path=Path(DEVICES_PATH),
            routing_path=Path(ROUTING_PATH),
            history_path=Path(HISTORY_PATH),
            database_path=Path(STATE_DATABASE_PATH),
            networkd_path=Path(NETWORKD_PATH),
            lock_path=Path(STATE_LOCK_PATH),
        )
    )


def _print_status(status: NetworkStatus) -> None:
    """Print Kavernis concepts rather than state-store implementation details."""
    print(f"Network configuration: {status.kind.value}")
    print(f"Desired revision: {status.desired_revision or 'unrecorded desired state'}")
    print(f"Last successful revision: {status.applied_revision or 'none'}")
    if status.last_outcome is not None:
        outcome = status.last_outcome
        outcome_text = (
            "failed" if outcome.kind is ApplyOutcomeKind.FAILURE else "success"
        )
        print(f"Last apply: {outcome_text}")
        print(f"Operation: {outcome.operation.value}")
        print(f"Last operation at: {outcome.recorded_at.isoformat()}")
        if outcome.revision is not None:
            print(f"Last operation revision: {outcome.revision}")
        if outcome.kind is ApplyOutcomeKind.FAILURE and outcome.message is not None:
            print(f"Failure: {outcome.message}")
    for item in status.drift:
        print(f"Drift: {item.kind.value} {item.filename}")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
