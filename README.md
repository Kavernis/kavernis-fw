# Kavernis FW

**Kavernis FW** is an open-source, Linux-native firewall and network management platform built around declarative configuration and Infrastructure as Code principles.

> **Declare the desired state. Validate it. Understand the change. Apply it safely.**

Kavernis aims to provide a modern and transparent alternative to traditional firewall appliances while relying on proven native Linux networking technologies.

## Project Status

> [!IMPORTANT]
> Kavernis FW is currently in early development and is **not ready for production use**.

The initial development focuses on building the core architecture and configuration model before implementing the complete firewall feature set.

The first milestone is intentionally small:

```text
interfaces.yaml
      │
      ▼
Configuration validation
      │
      ▼
Internal domain model
      │
      ▼
systemd-networkd backend
      │
      ▼
Generated configuration
      │
      ▼
Native validation
```

## Goals

Kavernis FW is designed around several core principles:

* **Declarative configuration** — describe the desired network and security state rather than a sequence of commands.
* **Linux-native** — rely on proven Linux networking technologies rather than reimplementing them.
* **Deterministic** — the same desired state should produce the same system configuration.
* **Validated** — configuration must be validated before it can affect the system.
* **Reproducible** — a firewall should be rebuildable from its configuration.
* **Auditable** — configuration and changes should be understandable and reviewable.
* **Automatable** — everything exposed by the web interface should eventually be manageable through an API or CLI.
* **Recoverable** — configuration changes should be designed with rollback and disaster recovery in mind.
* **Secure by design** — firewall and network changes are treated as security-sensitive operations.

## Architecture

Kavernis separates user intent from the native Linux configuration used to implement it.

```text
                    ┌─────────────────┐
                    │     Web UI      │
                    │     Vue.js      │
                    └────────┬────────┘
                             │
                             │ REST API
                             ▼
                    ┌─────────────────┐
                    │  Kavernis API   │
                    └────────┬────────┘
                             │
                             ▼
                    ┌─────────────────┐
                    │  Kavernis Core  │
                    └────────┬────────┘
                             │
                             ▼
                    ┌─────────────────┐
                    │  Domain Model   │
                    └────────┬────────┘
                             │
              ┌──────────────┼──────────────┐
              │              │              │
              ▼              ▼              ▼
        systemd-networkd   nftables        Kea
           backend         backend        backend
              │              │              │
              └──────────────┼──────────────┘
                             │
                             ▼
                         Linux OS
```

The Kavernis domain model is independent of the underlying Linux services.

Backends translate this model into native configuration.

This separation makes it possible to evolve configuration formats and potentially support alternative backends without coupling the entire project to a specific Linux tool.

## Declarative Configuration

Kavernis configuration is stored as YAML and separated by functional domain.

A future installation may use a structure similar to:

```text
/etc/kavernis/
├── interfaces.yaml
├── gateways.yaml
├── routes.yaml
├── firewall.yaml
├── dhcp.yaml
├── dns.yaml
└── system.yaml
```

For example:

```yaml
version: 1

interfaces:
  - id: wan
    name: WAN
    device: eth0

    ipv4:
      mode: dhcp

    ipv6:
      mode: dhcp6

  - id: lan
    name: LAN
    device: eth1

    ipv4:
      mode: static
      address: 192.168.10.254/24

    ipv6:
      mode: slaac
```

The YAML configuration represents the desired state.

`ipv4` and `ipv6` are optional on an interface. An omitted address-family block
means `mode: disabled`, so an unaddressed interface can be written concisely:

```yaml
- id: trunk
  name: Trunk
  device: eth1
```

### DHCPv4 policy

IPv4 DHCP options are optional desired-state overrides. Kavernis resolves and
renders its own DHCPv4 policy even when the `dhcp` block is omitted:

```yaml
ipv4:
  mode: dhcp
```

The generated systemd-networkd file still contains an explicit `[DHCPv4]`
section:

| Property | Kavernis default | networkd output |
| --- | ---: | --- |
| `use_hostname` | `false` | `UseHostname=no` |
| `send_hostname` | `true` | `SendHostname=yes` |
| `use_dns` | `true` | `UseDNS=yes` |
| `use_routes` | `true` | `UseRoutes=yes` |
| `use_ntp` | `true` | `UseNTP=yes` |
| `route_metric` | unset | omitted |

`use_hostname: false` prevents a DHCP server from changing the firewall or
router hostname. It is deliberately independent from `send_hostname: true`,
which permits the local hostname to be sent to the DHCP server. Individual
defaults can be overridden without repeating the rest:

```yaml
ipv4:
  mode: dhcp
  dhcp:
    use_dns: false
    route_metric: 200
```

The `dhcp` subsection is valid only with `ipv4.mode: dhcp`; it is rejected for
static or disabled IPv4. `use_routes` affects only DHCP-learned routes, never
Kavernis-declared static routes.
See [the DHCPv4 override example](configs/examples/interfaces-dhcp-options.yaml).

Kavernis YAML may omit properties that have Kavernis defaults, but generated
native configuration explicitly encodes Kavernis-defined behavior whenever
practical. Kavernis does not rely on native-service defaults for behavior it
owns.

Generated files such as nftables rulesets or systemd-networkd configuration are implementation artifacts and are not the source of truth.

### Static gateways and routes

`interfaces.yaml` configures devices and addressing. `gateways.yaml` defines
logical static next-hops (or a direct-interface exit), and `routes.yaml` maps a
destination network to a gateway id. Routes are rendered into the resolved
interface's systemd-networkd `.network` file as part of the normal
`kavernis plan network` and `kavernis apply network` transaction.

```yaml
# gateways.yaml
version: 1
gateways:
  - id: internet
    name: Internet router
    interface: wan
    address: 192.168.0.254
  - id: vpn-direct
    name: VPN direct
    interface: vpn0

# routes.yaml
version: 1
routes:
  - id: default-v4
    network: 0.0.0.0/0
    gateway: internet
  - id: vpn-network
    network: 10.50.0.0/16
    gateway: vpn-direct
```

An addressed gateway must belong to a statically configured subnet on its
referenced interface. `onlink: true` is an explicit assertion that it is
directly reachable despite that not being provable from static addressing; it
does not bypass IP-family checks. A direct gateway has no address and renders a
route without `Gateway=`. DHCP-provided addresses, gateways, and routes remain
managed by the existing DHCP interface behavior and are not represented as
gateway objects.

### VLAN interfaces

An interface can declare an optional `vlan` block. `parent` references the `id`
of a physical interface in the same document, and `tag` is an integer from 1 to
4094. The `(parent, tag)` pair must be unique. Nested VLANs are not supported.
Existing interface declarations without a `vlan` block remain unchanged.

```yaml
  - id: guests
    name: Guests VLAN
    device: eth1.20
    vlan:
      parent: lan
      tag: 20
    ipv4:
      mode: static
      address: 192.168.20.1/24
    ipv6:
      mode: disabled
```

`device` explicitly names the VLAN interface (at most 15 characters); it does
not need to follow the `parent.tag` naming convention. VLAN interfaces support
the same IPv4 and IPv6 modes as physical interfaces. A parent can carry multiple
VLANs and retain its own IP configuration, or use disabled IP modes for a trunk.

See [the complete VLAN example](configs/examples/interfaces-vlan.yaml).
The backend generates a `.netdev` file per VLAN, a `.network` file per interface,
and `VLAN=` attachments in the parent's `.network` file, using the native
[systemd VLAN configuration](https://www.freedesktop.org/software/systemd/man/systemd.netdev.html).
Candidates are validated in memory before they can be applied to the host.

### Bridge interfaces

A bridge joins physical or VLAN interfaces into one layer-2 network. Declare a
separate interface with a `bridge` block, referencing member interface `id`s:

```yaml
  - id: lan-bridge
    name: LAN bridge
    device: br0
    bridge:
      members: [lan-one, lan-two]
      stp: true
    ipv4:
      mode: static
      address: 192.168.10.1/24
    ipv6:
      mode: disabled
```

Each member must be declared in the same document with both IP modes set to
`disabled`. Configure addresses, DHCP or SLAAC on the bridge itself. Members
have DHCP, IPv6 router advertisements and link-local addressing explicitly
disabled in the generated files. The bridge device name follows the same rules
as a VLAN device name.

`members` must contain at least one unique interface id. A member can belong to
only one bridge. Bridges cannot be nested or also declare a `vlan` block.
VLAN interfaces can be members, but a physical interface used as a VLAN parent
cannot also be a bridge member; attach its VLAN interfaces instead. VLANs on top
of bridges and bridge VLAN filtering are not supported in this initial slice.

`stp` defaults to `true` and can explicitly be set to `false`. The backend emits
`Kind=bridge` and `STP=` in a `.netdev` file, with `Bridge=` attachments in each
member's `.network` file, following the
[systemd bridge configuration](https://www.freedesktop.org/software/systemd/man/systemd.netdev.html).
See [the complete bridge example](configs/examples/interfaces-bridge.yaml).
Generation and validation stay in memory until an explicit apply operation.

## Command line interface

The `network` transactional domain reads `/etc/kavernis/interfaces.yaml`,
`/etc/kavernis/gateways.yaml`, and `/etc/kavernis/routes.yaml` together.
Interfaces, gateways, and routes are not independently applied because they
contribute to the same systemd-networkd artifacts.

```bash
kavernis plan network
```

`plan` loads, validates and resolves the YAML, then prints the generated
`systemd-networkd` files without modifying the host.

### Safely editing desired state

The three network YAML files remain ordinary, human-readable desired state and
are the authoritative configuration. Direct writes are unsupported: use the
resource-oriented editor command instead.

```bash
kavernis edit interfaces
kavernis edit gateways
kavernis edit routes
```

`edit` opens a secure temporary copy using `$VISUAL`, then `$EDITOR`, then
`nano`. It validates only the document being edited before atomically replacing
the live YAML: interface rules within `interfaces.yaml` (such as VLAN parents
and bridge members) remain validated there, while gateway/interface and
route/gateway relationships are deliberately deferred. If validation fails,
Kavernis keeps the same candidate open through a visudo-like edit-again or
abort prompt; aborting removes the temporary candidate without changing live
desired state.

`kavernis plan network` and `kavernis apply network` load all three documents
and perform complete cross-resource validation and resolution, including
gateway interface references, gateway reachability, and route gateway/IP-family
consistency. All network desired-state writes share the same
`/run/kavernis/network.lock` advisory lock, including the full correction loop.
Kavernis also fingerprints all three YAML files with SHA-256 and refuses to
overwrite an edit if any file was changed externally while the editor was open.
The same Core transaction is available to future API writers.

Editing only changes desired state; it does not apply configuration or advance
SQLite applied state. The normal workflow is:

```bash
kavernis edit interfaces
kavernis edit gateways
kavernis edit routes
kavernis plan network
kavernis apply network
```

```bash
kavernis apply network
```

`apply` runs the same validation and generation step, writes only the
Kavernis-owned `10-kavernis-*` files in `/etc/systemd/network`, and invokes
`networkctl reload`. It therefore requires the privileges needed to modify
system network configuration. If the reload fails, the previous
Kavernis-owned files are restored and Kavernis attempts to reload them.

### Network state and history

The network workflow records desired-state history separately from the
user-managed configuration directory. `/etc/kavernis/interfaces.yaml`,
`gateways.yaml`, and `routes.yaml` remain the source of truth; Kavernis snapshots
all three files as one desired network revision in its internal Git repository
at `/var/lib/kavernis/history` only when applying changed desired state. The
last successful apply revision and SHA-256 hashes of generated
`10-kavernis-*` artifacts are stored in `/var/lib/kavernis/state.db`.

```bash
kavernis status network
kavernis history network
kavernis diff network
kavernis rollback network <revision>
```

`status` distinguishes `in sync`, `pending changes`, `drift detected`, and
`not yet applied`. Artifact drift covers missing, changed, and unexpected
Kavernis-owned systemd-networkd files only. It deliberately does not yet detect
live runtime changes made with tools such as `ip addr`.

Rollback restores the requested historical network desired state atomically,
makes a new history commit, and regenerates native files through the normal validated
apply path. If native application fails, the previous managed native files and
SQLite successful-applied revision remain in effect; the attempted rollback
YAML remains current so the failed desired transition is explicit and auditable.

## Configuration Pipeline

Configuration follows a strict processing pipeline:

```text
YAML
 │
 ▼
Schema validation
 │
 ▼
Typed configuration objects
 │
 ▼
Domain model
 │
 ▼
Dependency resolution / planning
 │
 ▼
Rendering context
 │
 ▼
Jinja2 native configuration rendering
 │
 ▼
Native configuration validation
 │
 ▼
Safe application
```

A backend never parses Kavernis YAML directly.

### Native configuration templates

Native configuration templates are packaged with Kavernis and grouped by
functional domain. The first domain is `interfaces`, whose templates render
systemd-networkd `.network` and `.netdev` files. Future domains such as
firewall, DHCP, DNS, routing, and VPN can use the same organization when they
are implemented.

The responsibility boundary is deliberately narrow:

```text
Python: desired state → validation → resolution → rendering context
Jinja2: rendering context → native configuration syntax
```

Python makes all semantic decisions and validates both the domain model and the
rendered candidate. Templates only present already-resolved values using the
native service syntax.

For example:

```text
firewall.yaml
      │
      ▼
Kavernis configuration layer
      │
      ▼
Firewall domain model
      │
      ▼
nftables backend
      │
      ▼
nftables ruleset
```

This separation is one of the fundamental architectural principles of the project.

## Linux Platform

The primary target platform is **Debian Stable**.

Kavernis intends to rely primarily on standard Linux and Debian components.

Planned technologies include:

| Function              | Technology       |
| --------------------- | ---------------- |
| Operating system      | Debian           |
| Network configuration | systemd-networkd |
| Firewall / NAT        | nftables         |
| DHCP                  | Kea DHCP         |
| DNS                   | Bind9         |
| Advanced routing      | FRRouting        |
| VPN                   | WireGuard        |
| Backend / Core        | Python           |
| Web frontend          | Vue.js           |

Not all of these components are implemented yet.

The internal architecture intentionally avoids unnecessary coupling to a specific backend so alternative implementations may be supported in the future.

## Repository Structure

The project currently follows a monorepo approach.

```text
kavernis-fw/
├── backend/
│   └── kavernis/
│       ├── api/
│       ├── backends/
│       ├── config/
│       ├── core/
│       └── models/
│
├── configs/
│   └── examples/
│
├── schemas/
│
├── tests/
│   ├── unit/
│   └── integration/
│
├── docs/
│
├── CLAUDE.md
├── LICENSE
├── README.md
└── pyproject.toml
```

The repository will grow progressively as features are implemented.

The project deliberately avoids creating unnecessary abstractions or components before they are required.

## Development Roadmap

The initial development sequence is expected to be:

```text
Interfaces
    │
    ▼
Routing
    │
    ▼
Firewall
    │
    ▼
NAT
    │
    ▼
DHCP
    │
    ▼
DNS
    │
    ▼
VPN
```

The priority is correctness and architectural consistency rather than feature count.

### Initial milestone

The first functional vertical slice is:

1. Load `interfaces.yaml`
2. Validate its schema
3. Build typed Python objects
4. Build the internal interface model
5. Generate systemd-networkd configuration
6. Validate generated configuration
7. Add unit and integration tests

The API and web interface will be built on top of the same core engine once this foundation is stable.

## Development

Kavernis FW uses Python for its backend and core components.

The project favors:

* modern Python
* type annotations
* explicit domain models
* deterministic generators
* small focused components
* unit testing
* minimal dependencies

Development tooling will progressively include:

```text
pytest
ruff
mypy
```

Detailed architecture and development rules are documented in [`CLAUDE.md`](CLAUDE.md) and the `docs/` directory.

## Git Workflow

The `main` branch is protected.

Changes should be developed in dedicated branches and submitted through Pull Requests.

```text
main
 │
 └── feature/<name>
          │
          ▼
     Pull Request
          │
          ├── Review
          ├── Automated tests
          └── Merge
```

Direct changes to `main` should not be used for normal development.

## Conventional Commits

Kavernis uses the **Conventional Commits** specification.

Commit messages follow:

```text
<type>[optional scope]: <description>
```

Examples:

```text
feat(network): add interface configuration model
fix(nftables): correct NAT rule generation
docs: add architecture documentation
test(networkd): add static IPv4 generator tests
refactor(core): separate validation from apply logic
ci: add backend test workflow
```

Common types include:

* `feat`
* `fix`
* `docs`
* `test`
* `refactor`
* `perf`
* `build`
* `ci`
* `chore`

Breaking changes must be explicitly marked according to the Conventional Commits specification.

## Contributing

Kavernis FW is in its early development phase, and the contribution process will evolve with the project.

Before contributing:

1. Read this README.
2. Read `CLAUDE.md` for architectural and development rules.
3. Keep changes focused and consistent with the existing architecture.
4. Add or update tests when behavior changes.
5. Use Conventional Commits.
6. Submit changes through a Pull Request.

Large architectural changes should be discussed before implementation.

## Security

Kavernis is security infrastructure.

Security issues should **not** be disclosed through public GitHub issues.

A dedicated security reporting process will be documented in `SECURITY.md`.

Until that process is available, avoid publishing details of exploitable vulnerabilities publicly.

## License

Kavernis FW Community Edition is licensed under the **GNU Affero General Public License v3.0 (AGPL-3.0)**.

See [`LICENSE`](LICENSE) for the complete license text.

## Why Kavernis?

Linux already provides excellent networking technologies.

Kavernis does not aim to replace them.

Instead, Kavernis aims to provide a coherent management layer above them:

```text
               Kavernis
                  │
        Desired State Model
                  │
      Validation / Planning
                  │
    Safe Application / Rollback
                  │
    ┌─────────────┼─────────────┐
    ▼             ▼             ▼
 nftables      networkd        Kea
    │             │             │
    └─────────────┼─────────────┘
                  ▼
                Linux
```

The objective is to combine the transparency and performance of Linux networking with the usability expected from a modern firewall platform.

**Simple enough to understand.
Strict enough to trust.
Declarative enough to reproduce.**
