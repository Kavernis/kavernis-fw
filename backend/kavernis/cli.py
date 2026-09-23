"""Command line interface for Kavernis desired-state operations."""

import argparse
import sys
from collections.abc import Sequence

from kavernis.backends.networkd import NetworkdApplyError, NetworkdConfiguration
from kavernis.config.errors import ConfigurationError
from kavernis.config.loader import load_interfaces
from kavernis.core.interfaces import apply_interfaces, plan_interfaces

INTERFACES_PATH = "/etc/kavernis/interfaces.yaml"


def main(arguments: Sequence[str] | None = None) -> int:
    """Run the Kavernis command line interface."""
    parser = argparse.ArgumentParser(prog="kavernis")
    action = parser.add_subparsers(dest="action", required=True)
    for name, help_text in (
        ("plan", "generate and validate configuration without applying it"),
        ("apply", "generate, validate, and apply configuration"),
    ):
        command = action.add_parser(name, help=help_text)
        command.add_argument("resource", choices=("interfaces",))

    parsed = parser.parse_args(arguments)
    try:
        config = load_interfaces(INTERFACES_PATH)
        if parsed.action == "plan":
            _print_candidate(plan_interfaces(config))
        else:
            candidate = apply_interfaces(config)
            print(f"Applied {len(candidate.files)} systemd-networkd file(s).")
    except (ConfigurationError, NetworkdApplyError, ValueError) as error:
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


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
