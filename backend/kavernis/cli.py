"""Command line interface for Kavernis desired-state operations."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from kavernis.backends.networkd import NetworkdApplyError, NetworkdConfiguration
from kavernis.config.errors import ConfigurationError
from kavernis.core.network import (
    NETWORK_DOMAIN,
    NetworkStatePaths,
    NetworkStateService,
    NetworkStatus,
    load_network,
    plan_network,
)
from kavernis.state.history import HistoryError
from kavernis.state.sqlite import StateStoreError

INTERFACES_PATH = "/etc/kavernis/interfaces.yaml"
GATEWAYS_PATH = "/etc/kavernis/gateways.yaml"
ROUTES_PATH = "/etc/kavernis/routes.yaml"
HISTORY_PATH = "/var/lib/kavernis/history"
STATE_DATABASE_PATH = "/var/lib/kavernis/state.db"
NETWORKD_PATH = "/etc/systemd/network"
STATE_LOCK_PATH = "/var/lib/kavernis/state.lock"


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
        else:
            desired = load_network(service.paths)
            if parsed.action == "plan":
                _print_candidate(plan_network(desired))
            elif parsed.action == "apply":
                candidate = service.apply(desired)
                print(f"Applied {len(candidate.files)} systemd-networkd file(s).")
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
        ValueError,
        OSError,
    ) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


def _print_candidate(candidate: NetworkdConfiguration) -> None:
    """Render a deterministic, reviewable plan to standard output."""
    for index, (filename, content) in enumerate(sorted(candidate.files.items())):
        if index:
            print()
        print(f"--- {filename}")
        print(content, end="")


def _state_service() -> NetworkStateService:
    """Construct dependencies at the CLI composition boundary."""
    return NetworkStateService(
        NetworkStatePaths(
            interfaces_path=Path(INTERFACES_PATH),
            gateways_path=Path(GATEWAYS_PATH),
            routes_path=Path(ROUTES_PATH),
            history_path=Path(HISTORY_PATH),
            database_path=Path(STATE_DATABASE_PATH),
            networkd_path=Path(NETWORKD_PATH),
            lock_path=Path(STATE_LOCK_PATH),
        )
    )


def _print_status(status: NetworkStatus) -> None:
    """Print Kavernis concepts rather than state-store implementation details."""
    print(f"Status: {status.kind.value}")
    print(f"Desired revision: {status.desired_revision or 'unrecorded desired state'}")
    print(f"Applied revision: {status.applied_revision or 'none'}")
    for item in status.drift:
        print(f"Drift: {item.kind.value} {item.filename}")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
