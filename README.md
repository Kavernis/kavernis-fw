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
├── firewall.yaml
├── dhcp.yaml
├── dns.yaml
└── system.yaml
```

For example:

```yaml
version: 1

interfaces:
  - uid: "8f3a7c22-1c7d-4d6b-a901-000000000001"
    id: wan
    name: WAN
    device: eth0

    ipv4:
      mode: dhcp

    ipv6:
      mode: dhcp6

  - uid: "8f3a7c22-1c7d-4d6b-a901-000000000002"
    id: lan
    name: LAN
    device: eth1

    ipv4:
      mode: static
      address: 192.168.10.254/24

    ipv6:
      mode: slaac
```

The YAML configuration represents the desired state.

Generated files such as nftables rulesets or systemd-networkd configuration are implementation artifacts and are not the source of truth.

### VLAN interfaces

An interface can declare an optional `vlan` block. `parent` references the `id`
of a physical interface in the same document, and `tag` is an integer from 1 to
4094. The `(parent, tag)` pair must be unique. Nested VLANs are not supported.
Existing interface declarations without a `vlan` block remain unchanged.

```yaml
  - uid: "8f3a7c22-1c7d-4d6b-a901-000000000003"
    id: guests
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

See [the complete VLAN example](configs/examples/interfaces-vlan.yml).
The backend generates a `.netdev` file per VLAN, a `.network` file per interface,
and `VLAN=` attachments in the parent's `.network` file, using the native
[systemd VLAN configuration](https://www.freedesktop.org/software/systemd/man/systemd.netdev.html).
Candidates are validated in memory; this milestone does not apply configuration
to the host or perform native service validation.

### Bridge interfaces

A bridge joins physical or VLAN interfaces into one layer-2 network. Declare a
separate interface with a `bridge` block, referencing member interface `id`s:

```yaml
  - uid: "8f3a7c22-1c7d-4d6b-a901-000000000004"
    id: lan-bridge
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
See [the complete bridge example](configs/examples/interfaces-bridge.yml).
Generation and validation stay in memory without modifying the host network.

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
Backend generation
 │
 ▼
Native configuration validation
 │
 ▼
Safe application
```

A backend never parses Kavernis YAML directly.

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
