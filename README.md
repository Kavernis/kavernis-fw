# Kavernis FW

Kavernis is an early-stage, declarative Linux network and firewall management
project for **Kavernis OS**. The official platform is Debian 13 (Trixie), using
its systemd 257 `systemd-networkd` package family and Debian-maintained kernel.
Kavernis is authoritative for appliance system networking; NetworkManager,
ifupdown and arbitrary-distribution coexistence are not supported. YAML
describes desired state; generated native configuration is an implementation
artifact.

**Declare the desired state. Validate it. Understand the change. Apply it safely.**

## Network architecture

The network model separates four responsibilities:

| Model | Responsibility |
| --- | --- |
| `NetworkDevice` | Stable device ID, kernel name, physical/VLAN/bridge topology |
| `NetworkInterface` | Logical L3 ID and display name, referencing one device ID |
| `InterfaceAddressing` | Independent IPv4 and IPv6 client/address settings |
| `RoutingModel` | Static gateways referencing L3 IDs; routes referencing gateways |
| `NetworkModel` | Immutable aggregate of devices, interfaces and routing |

Device IDs and logical interface IDs occupy separate namespaces. Device `name`
is the Linux kernel name; interface `name` is a display label. References always
use IDs. A device may have zero or one L3 assignment. A bridge member may have
none. A trunk needs no artificial L3 interface.

```text
YAML → strict Pydantic resources → immutable network model
     → topology/reference validation and dependency order
     → networkd rendering context → packaged Jinja2 templates
     → native candidate validation → safe apply → applied-state recording
```

Domain models do not depend on YAML, Pydantic, systemd, Jinja2, Git or SQLite.
See [the architecture documentation](docs/network-architecture.md) for topology
rules, state transitions and failure semantics.

## Desired configuration

Only this layout is supported; every document must declare integer `version: 2`:

```text
/etc/kavernis/network/
├── devices.yaml       # mandatory; at least one device
├── interfaces.yaml    # mandatory; interfaces: [] is valid
└── routing.yaml       # optional; absent means no static gateways or routes
```

Planning never creates an optional resource. DHCP-learned gateways and routes
are runtime state and require no static gateway declarations.

This complete example has a DHCP WAN, an unaddressed LAN trunk and two addressed
VLANs. It requires **no `routing.yaml`**.

`/etc/kavernis/network/devices.yaml`:

```yaml
version: 2

devices:
  - id: wan-nic
    type: physical
    name: enp1s0
  - id: lan-nic
    type: physical
    name: enp2s0
  - id: users-vlan
    type: vlan
    name: vlan10
    vlan:
      parent: lan-nic
      tag: 10
  - id: iot-vlan
    type: vlan
    name: vlan30
    vlan:
      parent: lan-nic
      tag: 30
```

`/etc/kavernis/network/interfaces.yaml`:

```yaml
version: 2

interfaces:
  - id: wan
    name: WAN
    device: wan-nic
    ipv4:
      mode: dhcp
  - id: users
    name: USERS
    device: users-vlan
    ipv4:
      mode: static
      address: 192.168.10.1/24
  - id: iot
    name: IOT
    device: iot-vlan
    ipv4:
      mode: static
      address: 192.168.30.1/24
```

Physical devices are adopted by kernel name. VLAN parents must be physical
devices; tags are integers from 1 to 4094, unique per parent. Bridges reference
physical or VLAN member IDs and default to STP enabled. Bridge nesting, VLANs on
bridges, and bridge membership of physical VLAN parents are unsupported.

IPv4 modes are `static`, `dhcp`, and `disabled`. IPv6 modes are `static`, `dhcp6`,
`slaac`, and `disabled`. Omitted family blocks mean disabled. Static mode requires
one address with prefix; other modes reject a non-null address. Devices without
an L3 interface explicitly disable DHCP, router advertisements and link-local
addressing. Enabled IPv6 retains IPv6 link-local addressing.

DHCPv4 policy is optional and renders explicit values:

```yaml
ipv4:
  mode: dhcp
  dhcp:
    use_hostname: false
    send_hostname: true
    use_dns: true
    use_routes: true
    use_ntp: true
    # route_metric: 200  # optional integer, 0 through 4294967295
```

Static routing can be added independently:

```yaml
# /etc/kavernis/network/routing.yaml
version: 2
gateways:
  - id: main-router
    interface: users
    address: 192.168.10.254
routes:
  - id: other-network
    network: 10.100.0.0/16
    gateway: main-router
```

An addressed gateway must belong to a static subnet on its logical interface,
unless `onlink: true` explicitly asserts reachability. Family validation still
applies. Omit the gateway address for direct-interface routes; `onlink` then must
be false. Static default routes use `0.0.0.0/0` or `::/0`. `use_routes: false`
affects DHCP-learned routes, not explicitly declared static routes.

Complete independent examples:

- [DHCP WAN](configs/examples/dhcp/network)
- [DHCP policy overrides](configs/examples/dhcp-options/network)
- [WAN and two VLANs](configs/examples/vlans/network)
- [Bridge](configs/examples/bridge/network)
- [Static routing](configs/examples/routing/network)
- [DHCPv6 and SLAAC](configs/examples/dual-stack/network)

Schemas live in [schemas/network](schemas/network). Pydantic validates resource
fields; core validates topology and cross-resource relationships.

## CLI workflow

Install with Python 3.11 or newer:

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

For a new installation, provision both mandatory YAML documents under
`/etc/kavernis/network/`, using an example and the correct host device names.
Then use the resource editor for interactive changes:

```bash
kavernis edit devices
kavernis edit interfaces
kavernis edit routing
kavernis plan network
kavernis apply network
```

`edit` selects `$VISUAL`, then `$EDITOR`, then `nano`. It edits a secure temporary
copy, validates that resource, offers correction or abort, and replaces the live
file atomically. References to other resources are checked by plan/apply, allowing
related resources to be edited in sequence. An absent routing resource is created
only if a valid change is saved. Editing never applies native configuration.

All state-changing network operations share `/run/kavernis/network.lock`.
Fingerprints cover bytes and presence of all three paths, including their version
fields. External changes during editing cause a conflict instead of an overwrite.
Configuration management tools can provision YAML, but must coordinate writes
with this same lock; arbitrary unlocked writes cannot be made transactional.

`plan` validates and prints the candidate plus a structural transition preview
without changing the host. `apply` validates before writing, atomically replaces
each owned artifact with mode `0644`, removes obsolete owned artifacts, and
runs scoped `networkctl`/`ip` lifecycle actions when a managed VLAN or bridge
must be recreated. Normal supported VLAN and bridge transitions do not require
a reboot, but may interrupt traffic on the affected interfaces.
Artifact names are `10-kavernis-<device-id>.network` and, for VLANs and bridges,
`10-kavernis-<device-id>.netdev`. Unrelated networkd files are left untouched.
Apply checks SQLite readiness and the last applied Git topology before native
mutation, then verifies required static Linux state after native convergence
before recording success. DHCP/SLAAC acquisition and external connectivity are
separate from this activation guarantee. Physical device removal/recreation and
ambiguous ownership are rejected before mutation, including during rollback. A
failed or interrupted native transaction restores artifacts, removes only
affected managed virtual identities, reloads the previous topology and performs
bounded runtime verification where a known baseline exists. This does not
guarantee live connectivity recovery. See
[network application safety](docs/network-application-safety.md) for exact checks,
recovery limits and integration-test instructions.

## State and recovery

| State | Storage |
| --- | --- |
| Current desired state | `/etc/kavernis/network/*.yaml` |
| Desired revision history | Internal Git repository `/var/lib/kavernis/history/` |
| Successful applied revision and artifact hashes | `/var/lib/kavernis/state.db` |
| Observed configuration | Linux and native service artifacts |

```bash
kavernis status network
kavernis history network
kavernis diff network
kavernis diff network <revision>
kavernis rollback network <revision>
```

History captures the actual resource bytes and optional file presence. The public
configuration directory is not made into a Kavernis-owned Git repository.
SQLite stores operational metadata, never a second desired-state configuration.
`status` reports not yet applied, pending changes, in sync, or artifact drift.
Drift covers missing, modified and unexpected owned artifacts, not live runtime
address or route changes.

Rollback restores a desired revision, records the new desired transition and
regenerates native configuration through the normal validation/apply pipeline.
It preserves IDs and history. Native apply failure keeps the previous successful
SQLite revision; the attempted rollback YAML remains current and auditable.
State-store errors are reported and never treated as successful recording.

This is a breaking architecture change for development installations. There is
no compatibility adapter, migration command, version detection or cross-version
rollback. Follow the [one-time development cleanup procedure](docs/development-cleanup.md)
before using an existing installation with this layout.

## Development and scope

```bash
.venv/bin/pytest
.venv/bin/ruff check .
.venv/bin/mypy
PYTHONPATH=backend .venv/bin/python scripts/generate_network_schemas.py
```

Unit tests use temporary configuration, native, history and database paths,
mocked reloads and explicit runtime observations. Opt-in integration tests require
an isolated network namespace or an explicitly designated disposable VM; they
are skipped in the normal test suite.

The implementation covers physical/VLAN/bridge devices, logical L3 interfaces,
existing IP client/addressing modes, static routing, editing, history, state and
rollback. DHCP/RA/DNS servers, Prefix Delegation, firewall/NAT, VPN, bonding,
advanced routing, multiple static addresses and high availability are future
work. No service placeholders are part of this model.

Development rules are in [AGENTS.md](AGENTS.md) and [CLAUDE.md](CLAUDE.md).
Use feature branches and reviewed pull requests; `main` is protected. Commits
follow Conventional Commits, with `!` or a `BREAKING CHANGE` footer for breaking
changes. The project is licensed under [AGPL-3.0](LICENSE).
