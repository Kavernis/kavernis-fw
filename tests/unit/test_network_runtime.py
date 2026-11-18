"""Static verification tests; observations are explicit, never host networking."""

import json
import subprocess
from dataclasses import replace
from ipaddress import ip_interface
from pathlib import Path

import pytest
from kavernis.backends.network_runtime import (
    LinuxNetworkRuntimeInspector,
    NetworkRuntimeSnapshot,
    RuntimeLink,
    RuntimeRoute,
    RuntimeVerificationError,
    convergence_issues,
    parse_snapshot,
    static_routes,
    verify_runtime,
)
from network_helpers import desired, interface, physical, resolved_model

DIRECTORY = Path("/test/networkd")


def topology():
    return resolved_model(
        desired(
            [
                physical(),
                {
                    "id": "vlan",
                    "name": "vlan30",
                    "type": "vlan",
                    "vlan": {"parent": "nic", "tag": 30},
                },
                {
                    "id": "br",
                    "name": "br0",
                    "type": "bridge",
                    "bridge": {"members": ["vlan"]},
                },
            ],
            [
                interface(
                    device="br",
                    ipv4={"mode": "static", "address": "192.0.2.1/24"},
                    ipv6={"mode": "static", "address": "2001:db8::1/64"},
                )
            ],
            [{"id": "gw", "interface": "lan", "address": "192.0.2.2"}],
            [{"id": "default", "network": "0.0.0.0/0", "gateway": "gw"}],
        )
    )


def snapshot():
    return NetworkRuntimeSnapshot(
        (
            RuntimeLink(
                "eth0",
                network_file=str(DIRECTORY / "10-kavernis-nic.network"),
                setup_state="configured",
            ),
            RuntimeLink(
                "vlan30",
                "vlan",
                "eth0",
                30,
                "br0",
                network_file=str(DIRECTORY / "10-kavernis-vlan.network"),
                setup_state="configured",
            ),
            RuntimeLink(
                "br0",
                "bridge",
                stp=True,
                addresses=frozenset({"192.0.2.1/24", "2001:db8::1/64", "fe80::1/64"}),
                configured_static_addresses=frozenset(
                    {"192.0.2.1/24", "2001:db8::1/64"}
                ),
                network_file=str(DIRECTORY / "10-kavernis-br.network"),
                setup_state="configured",
            ),
        ),
        frozenset({RuntimeRoute("0.0.0.0/0", "192.0.2.2", "br0")}),
    )


class FixedInspector:
    def __init__(self, value):
        self.value = value
        self.calls = 0

    def inspect(self, timeout):
        assert timeout > 0
        self.calls += 1
        return self.value


def test_static_runtime_success_allows_unrelated_addresses_and_routes():
    observed = snapshot()
    observed = replace(
        observed,
        routes=observed.routes
        | {RuntimeRoute("0.0.0.0/0", "198.51.100.1", "unrelated")},
    )
    verify_runtime(topology(), FixedInspector(observed), DIRECTORY)


def test_undeclared_bridge_member_is_rejected():
    observed = snapshot()
    extra = RuntimeLink("unmanaged-wan", master="br0")
    issues = convergence_issues(
        topology(), replace(observed, links=observed.links + (extra,)), DIRECTORY
    )
    assert "bridge br0: unexpected member unmanaged-wan" in issues


def test_missing_expected_bridge_member_is_rejected():
    observed = snapshot()
    vlan = replace(observed.links[1], master=None)
    issues = convergence_issues(
        topology(),
        replace(observed, links=(observed.links[0], vlan, observed.links[2])),
        DIRECTORY,
    )
    assert "bridge br0: missing member vlan30" in issues


@pytest.mark.parametrize(
    "index, changes, message",
    [
        (1, {"vlan_id": 40}, "expected VLAN ID 30, observed 40"),
        (1, {"parent": "eth9"}, "expected parent eth0"),
        (1, {"master": None}, "expected bridge br0"),
        (2, {"kind": "dummy"}, "observed type dummy"),
        (2, {"stp": False}, "expected STP True"),
        (2, {"addresses": frozenset({"192.0.2.1/24"})}, "2001:db8::1/64"),
        (2, {"addresses": frozenset({"2001:db8::1/64"})}, "192.0.2.1/24"),
        (0, {"network_file": "/unrelated.network"}, "expected network file"),
        (0, {"setup_state": "failed"}, "setup failed"),
        (0, {"dropins": ("/override.conf",)}, "unverified networkd drop-ins"),
    ],
)
def test_static_property_mismatches(index, changes, message):
    observed = snapshot()
    links = list(observed.links)
    links[index] = replace(links[index], **changes)
    issues = convergence_issues(
        topology(), replace(observed, links=tuple(links)), DIRECTORY
    )
    assert message in "; ".join(issues)


def test_dynamic_or_unproven_address_cannot_satisfy_static_intent():
    observed = snapshot()
    bridge = replace(
        observed.links[2],
        dynamic_addresses=frozenset({"192.0.2.1/24"}),
        configured_static_addresses=frozenset(),
    )
    issues = convergence_issues(
        topology(), replace(observed, links=(*observed.links[:2], bridge)), DIRECTORY
    )
    assert any(
        "lacks confirmed static networkd provenance" in issue for issue in issues
    )


def test_slaac_interface_link_local_gateway_uses_networkd_inferred_onlink():
    model = resolved_model(
        desired(
            [physical()],
            [interface(ipv6={"mode": "slaac"})],
            [{"id": "gw", "interface": "lan", "address": "fe80::1"}],
            [{"id": "default", "network": "::/0", "gateway": "gw"}],
        )
    )
    expected = next(iter(static_routes(model)))
    assert expected.onlink
    link = RuntimeLink(
        "eth0",
        network_file=str(DIRECTORY / "10-kavernis-nic.network"),
        setup_state="configured",
    )
    observed = NetworkRuntimeSnapshot((link,), frozenset({expected}))
    assert not convergence_issues(model, observed, DIRECTORY)


def test_missing_device():
    observed = replace(snapshot(), links=snapshot().links[1:])
    assert "device 'nic' (eth0) is missing" in convergence_issues(
        topology(), observed, DIRECTORY
    )


@pytest.mark.parametrize(
    "route",
    [
        RuntimeRoute("0.0.0.0/0", "192.0.2.3", "br0"),
        RuntimeRoute("0.0.0.0/0", "192.0.2.2", "other"),
        RuntimeRoute("192.0.2.0/24", "192.0.2.2", "br0"),
        RuntimeRoute("0.0.0.0/0", "192.0.2.2", "br0", True),
    ],
)
def test_route_identity_is_not_destination_alone(route):
    observed = replace(snapshot(), routes=frozenset({route}))
    assert "missing static route" in "; ".join(
        convergence_issues(topology(), observed, DIRECTORY)
    )


@pytest.mark.parametrize(
    "selector",
    [
        {"from": "2001:db8:abcd::/64"},
        {"tos": "0x10"},
        {"table": 100},
        {"gateway": "192.0.2.3"},
        {"dev": "other"},
    ],
)
def test_restricted_observed_route_cannot_satisfy_unrestricted_route(selector):
    entry = {
        "dst": "default",
        "gateway": "192.0.2.2",
        "dev": "br0",
        "protocol": "static",
        **selector,
    }
    parsed = parse_snapshot([], [], [entry], [], {"Interfaces": []})
    assert RuntimeRoute("0.0.0.0/0", "192.0.2.2", "br0") not in parsed.routes


@pytest.mark.parametrize(
    "families",
    [
        {"ipv4": {"mode": "dhcp"}},
        {"ipv6": {"mode": "slaac"}},
        {"ipv6": {"mode": "dhcp6"}},
    ],
)
def test_dynamic_acquisition_pending_does_not_block_static_activation(families):
    model = resolved_model(desired([physical()], [interface(**families)]))
    observed = NetworkRuntimeSnapshot(
        (replace(snapshot().links[0], setup_state="configuring"),)
    )
    assert not observed.links[0].addresses
    verify_runtime(model, FixedInspector(observed), DIRECTORY)


def test_monotonic_timeout_is_bounded_and_reports_last_mismatch():
    now = [0.0]
    inspector = FixedInspector(NetworkRuntimeSnapshot(()))
    with pytest.raises(RuntimeVerificationError, match="eth0.*missing"):
        verify_runtime(
            topology(),
            inspector,
            DIRECTORY,
            timeout=0.3,
            interval=0.1,
            clock=lambda: now[0],
            sleep=lambda t: now.__setitem__(0, now[0] + t),
        )
    assert inspector.calls == 3
    assert now[0] == 0.3


def test_polling_can_converge_before_deadline():
    class Inspector:
        calls = 0

        def inspect(self, timeout):
            self.calls += 1
            return NetworkRuntimeSnapshot(()) if self.calls == 1 else snapshot()

    inspector = Inspector()
    verify_runtime(topology(), inspector, DIRECTORY, sleep=lambda _: None)
    assert inspector.calls == 2


def test_retired_static_values_must_disappear():
    before = topology()
    after = replace(before, interfaces=(), routing=type(before.routing)())
    issues = convergence_issues(after, snapshot(), DIRECTORY, before)
    assert sum("retired static address" in issue for issue in issues) == 2
    assert sum("retired static route" in issue for issue in issues) == 1


def test_retired_managed_virtual_device_must_disappear():
    before = topology()
    after = replace(
        before,
        devices=(before.devices[0],),
        interfaces=(),
        routing=type(before.routing)(),
    )
    issues = convergence_issues(after, snapshot(), DIRECTORY, before)
    assert "retired managed virtual device remains: vlan30" in issues
    assert "retired managed virtual device remains: br0" in issues


def json_documents():
    return [
        [
            {"ifindex": 2, "ifname": "eth0"},
            {
                "ifindex": 3,
                "ifname": "vlan30",
                "link": "eth0",
                "master": "br0",
                "linkinfo": {"info_kind": "vlan", "info_data": {"id": 30}},
            },
            {
                "ifindex": 4,
                "ifname": "br0",
                "linkinfo": {"info_kind": "bridge", "info_data": {"stp_state": 1}},
            },
        ],
        [
            {
                "ifindex": 4,
                "addr_info": [
                    {"local": "192.0.2.1", "prefixlen": 24},
                    {"local": "2001:0db8::1", "prefixlen": 64},
                    {"local": "2001:db8::2", "prefixlen": 64, "tentative": True},
                    {"local": "2001:db8::3", "prefixlen": 64, "dadfailed": True},
                ],
            }
        ],
        [
            {
                "dst": "default",
                "gateway": "192.0.2.2",
                "dev": "br0",
                "protocol": "static",
            },
            {"dst": "192.0.2.0/24", "dev": "br0", "protocol": "kernel"},
            {"dst": "198.51.100.0/24", "dev": "br0", "protocol": "dhcp"},
        ],
        [
            {
                "dst": "default",
                "gateway": "fe80::1",
                "dev": "br0",
                "protocol": "static",
                "flags": ["onlink"],
            }
        ],
        {
            "Interfaces": [
                {
                    "Index": idx,
                    "NetworkFile": str(DIRECTORY / f"10-kavernis-{id}.network"),
                    "AdministrativeState": "configured",
                    "NetworkFileDropins": [],
                    "Addresses": [
                        {
                            "Address": list(ip_interface(value).ip.packed),
                            "Family": 2 if ip_interface(value).version == 4 else 10,
                            "PrefixLength": ip_interface(value).network.prefixlen,
                            "ConfigSource": "static",
                            "ConfigState": "configured",
                        }
                        for value in ("192.0.2.1/24", "2001:db8::1/64")
                    ]
                    if idx == 4
                    else [],
                }
                for idx, id in ((2, "nic"), (3, "vlan"), (4, "br"))
            ]
        },
    ]


def test_real_json_shapes_normalize_kernel_and_networkd_properties(monkeypatch):
    documents = iter(json_documents())
    calls = []

    def run(arguments, **kwargs):
        calls.append((arguments, kwargs))
        return subprocess.CompletedProcess(arguments, 0, json.dumps(next(documents)))

    monkeypatch.setattr(subprocess, "run", run)
    observed = LinuxNetworkRuntimeInspector().inspect(2.0)
    assert observed.links[1].parent == "eth0"
    assert observed.links[1].vlan_id == 30
    assert observed.links[1].master == "br0"
    assert observed.links[2].addresses == {"192.0.2.1/24", "2001:db8::1/64"}
    assert RuntimeRoute("::/0", "fe80::1", "br0", True) in observed.routes
    assert len(observed.routes) == 2
    assert not convergence_issues(topology(), observed, DIRECTORY)
    assert len(calls) == 5 and all(0 < opts["timeout"] <= 2 for _, opts in calls)


@pytest.mark.parametrize(
    "error",
    [
        subprocess.TimeoutExpired("ip", 1),
        OSError("ip missing"),
        subprocess.CalledProcessError(1, "networkctl"),
    ],
)
def test_inspection_errors_are_explicit(monkeypatch, error):
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(RuntimeVerificationError, match="cannot inspect"):
        LinuxNetworkRuntimeInspector().inspect(1)


def test_malformed_json_is_rejected(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a, 0, "not JSON"),
    )
    with pytest.raises(RuntimeVerificationError):
        LinuxNetworkRuntimeInspector().inspect(1)


def test_cross_namespace_link_index_is_not_a_local_parent():
    documents = json_documents()
    documents[0][1].update(link_netnsid=0, link_index=2)
    assert parse_snapshot(*documents).links[1].parent is None


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_nonfinite_or_nonpositive_waits_are_rejected(timeout):
    with pytest.raises(ValueError):
        verify_runtime(
            topology(), FixedInspector(snapshot()), DIRECTORY, timeout=timeout
        )


def test_retired_static_address_can_be_reacquired_dynamically():
    before = resolved_model(
        desired(
            [physical()],
            [interface(ipv4={"mode": "static", "address": "192.0.2.1/24"})],
        )
    )
    after = resolved_model(desired([physical()], [interface(ipv4={"mode": "dhcp"})]))
    link = replace(
        snapshot().links[0],
        addresses=frozenset({"192.0.2.1/24"}),
        dynamic_addresses=frozenset({"192.0.2.1/24"}),
    )
    assert not convergence_issues(
        after, NetworkRuntimeSnapshot((link,)), DIRECTORY, before
    )


def test_kernel_dynamic_address_flag_is_preserved_without_claiming_a_lease():
    documents = json_documents()
    documents[1][0]["addr_info"][0]["dynamic"] = True
    observed = parse_snapshot(*documents)
    assert observed.links[2].dynamic_addresses == {"192.0.2.1/24"}
