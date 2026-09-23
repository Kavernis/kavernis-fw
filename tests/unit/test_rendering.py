"""Tests for package-owned native configuration templates."""

from importlib.resources import files

import pytest
from jinja2 import UndefinedError
from kavernis.rendering import render_template


def test_network_template_renders_resolved_context() -> None:
    content = render_template(
        "interfaces/network.j2",
        {
            "device": "eth1",
            "addresses": ("192.0.2.1/24",),
            "dhcp": None,
            "ipv6_accept_ra": "no",
            "link_local_addressing": None,
            "vlans": (),
            "bridge": None,
        },
    )

    assert content == (
        "[Match]\nName=eth1\n\n[Network]\nAddress=192.0.2.1/24\n"
        "IPv6AcceptRA=no\n"
    )


def test_missing_template_context_is_an_error() -> None:
    with pytest.raises(UndefinedError):
        render_template("interfaces/network.j2", {})


def test_templates_are_available_through_package_resources() -> None:
    template = files("kavernis").joinpath("templates/interfaces/network.j2")

    assert template.is_file()
    assert template.read_text(encoding="utf-8").startswith("[Match]\n")
