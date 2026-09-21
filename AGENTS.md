# AGENTS.md — Kavernis FW Development Guidelines

This file defines the rules that AI-assisted development MUST follow when modifying Kavernis FW.

For project goals, architecture overview, technologies and roadmap, see [README.md](README.md).

## 1. Core Architecture

Kavernis uses a declarative desired-state architecture.

The fundamental processing pipeline is:

```text id="sn58wu"
YAML configuration
        ↓
Schema validation
        ↓
Typed configuration objects
        ↓
Internal domain model
        ↓
Business logic / planning
        ↓
Backend generators
        ↓
Native configuration validation
        ↓
Safe application
```

This separation MUST be preserved.

### Source of truth

Kavernis YAML configuration represents the desired state and is the source of truth.

Generated native configuration files are implementation artifacts and MUST NOT become the source of truth.

## 2. Layer Responsibilities

### Configuration layer

`backend/kavernis/config/`

Responsible for:

* loading YAML
* schema validation
* normalization
* conversion into typed configuration objects

It MUST NOT generate native service configuration.

### Domain models

`backend/kavernis/models/`

Models represent Kavernis concepts and network intent.

They MUST remain independent of backend implementation details.

Do not introduce nftables, systemd-networkd, Kea or Bind9 syntax into generic domain models.

Avoid passing arbitrary dictionaries between application layers. Convert external data into typed objects near the system boundary.

### Core

`backend/kavernis/core/`

Contains business logic including:

* dependency resolution
* desired-state processing
* planning
* validation
* backend orchestration
* apply and rollback coordination

Business logic MUST NOT be implemented in API handlers or frontend components.

### Backends

`backend/kavernis/backends/`

Backends translate the internal domain model into native configuration.

A backend MUST consume the internal model.

A backend MUST NOT parse Kavernis YAML directly.

Correct:

```text id="rpkpvs"
YAML → config → model → backend
```

Incorrect:

```text id="91ihb7"
YAML → backend
```

Prefer separating backend operations:

```python id="1b24ru"
candidate = backend.generate(model)
backend.validate(candidate)
backend.apply(candidate)
```

Generation should remain deterministic and testable without modifying the host system.

### API

The API is an interface to the Kavernis core.

API handlers MUST NOT contain networking, firewall, DHCP or DNS implementation logic.

### Frontend

The frontend uses Vue.js and communicates exclusively through the API.

The frontend MUST NOT:

* modify YAML files directly
* modify native configuration
* execute system commands
* reproduce backend business logic

## 3. Primary Platform

The primary target platform is Debian Stable.

Preferred native components are:

* systemd-networkd — network configuration
* nftables — firewall and NAT
* Kea — DHCP
* Bind9 — DNS
* FRRouting — advanced routing when required
* WireGuard — VPN

Platform-specific behavior belongs in backends or platform adapters.

Do not unnecessarily couple the internal domain model to Debian or any specific backend.

Do not introduce Ubuntu-specific dependencies such as Netplan into the primary Debian implementation.

## 4. Configuration

Public Kavernis configuration is declarative YAML.

Configuration SHOULD remain separated by domain, for example:

```text id="0d44kp"
interfaces.yaml
firewall.yaml
dhcp.yaml
dns.yaml
system.yaml
```

Public configuration structures MUST have associated schemas under `schemas/`.

Validation MUST occur before data reaches the internal domain model.

Invalid configuration MUST NOT reach backend generators.

Validation errors should be actionable and identify the relevant object and field whenever possible.

## 5. Object Identity

Configuration objects use two distinct identifiers:

* `uid` — immutable technical identifier, normally UUID-based
* `id` — immutable human-readable identifier used for configuration references

`name` is a mutable display attribute.

Example:

```yaml id="aj2b5c"
- uid: "8f3a7c22-1c7d-4d6b-a901-000000000002"
  id: lan
  name: Users LAN
  device: eth1
```

References between configuration objects SHOULD use `id`.

Neither `uid` nor `id` should be silently changed after object creation.

A future `id` rename operation must be explicit and transactional so references can be updated safely.

## 6. Python Guidelines

Use modern Python with type annotations.

Prefer explicit typed interfaces:

```python id="75t6is"
def generate(model: FirewallModel) -> str: ...
```

instead of:

```python id="bq3a9c"
def generate(config: dict): ...
```

Code should favor:

* small focused functions
* explicit exceptions
* clear domain models
* minimal global state
* deterministic behavior
* minimal dependencies
* readability over clever abstractions

Avoid premature abstraction.

Do not implement functionality for hypothetical future requirements unless it directly improves the current architecture.

Expected development tooling:

```text id="i6c6v9"
pytest
ruff
mypy
```

## 7. Safety

Kavernis is security infrastructure.

Treat changes affecting networking, routing, firewalling, authentication or system configuration as security-sensitive.

Always follow:

```text id="zkbsxk"
Generate
   ↓
Validate
   ↓
Apply
```

Never apply generated configuration without validation when the native service provides a validation mechanism.

A validation failure SHOULD leave the currently working configuration untouched.

Design network changes with future transactional application and rollback in mind.

Never:

* concatenate untrusted values into shell commands
* silently ignore invalid security configuration
* log secrets
* expose management services broadly by default
* weaken firewall behavior merely to make a failing test pass

Prefer safe APIs over shell execution. When subprocess execution is necessary, use explicit argument arrays.

## 8. Testing

Behavioral changes MUST include appropriate tests.

Unit tests should cover:

* models
* validation
* configuration parsing
* resolution
* planners
* generators

Backend generation tests MUST NOT modify the developer's host networking or firewall.

Integration tests may invoke native validation tools in controlled/disposable environments.

Bug fixes SHOULD include a regression test whenever practical.

## 9. Scope Discipline

Keep changes focused.

Do not introduce unrelated refactoring while implementing a feature or fixing a bug.

Do not prematurely implement:

* clustering
* high availability
* multi-node orchestration
* cloud management
* plugin systems
* multiple Linux distributions
* enterprise functionality

unless explicitly requested.

Prefer completing a small vertical slice before broadening the architecture.

## 10. Git Workflow

The `main` branch is protected.

Normal development follows:

```text id="kqhgpd"
feature/fix branch
       ↓
Pull Request
       ↓
Review + tests
       ↓
main
```

Do not commit directly to `main`.

## 11. Conventional Commits

All commits MUST follow the Conventional Commits specification:

```text id="34e59c"
<type>[optional scope]: <description>
```

Preferred types:

* `feat`
* `fix`
* `docs`
* `test`
* `refactor`
* `perf`
* `build`
* `ci`
* `chore`
* `style`
* `revert`

Scopes are optional but SHOULD be used when they improve clarity.

Examples:

```text id="zcy0z8"
feat(network): add interface domain model
feat(networkd): generate static IPv4 configuration
fix(config): reject duplicate interface ids
test(networkd): add DHCP generation tests
docs(architecture): document configuration pipeline
refactor(core): separate validation from apply logic
ci: run backend tests on pull requests
```

Descriptions MUST:

* be written in English
* use imperative form
* start with lowercase
* not end with a period

Breaking changes MUST use `!` or a `BREAKING CHANGE:` footer.

Example:

```text id="cb1n7z"
feat(config)!: change interface reference format
```

Pull Request titles SHOULD also follow Conventional Commits.

If squash merging is used, the resulting squash commit MUST comply with Conventional Commits.

## 12. AI-Assisted Development Rules

When modifying Kavernis:

1. Preserve the YAML → validation → model → backend architecture.
2. Never make a backend parse YAML directly.
3. Never place business logic in API handlers or Vue components.
4. Keep domain models independent from backend syntax.
5. Validate generated configuration before application.
6. Prefer reversible operations for system changes.
7. Add or update tests for behavioral changes.
8. Do not silently broaden the scope of a requested change.
9. Avoid unnecessary dependencies and abstractions.
10. Follow Conventional Commits.
11. Explain significant architectural deviations before implementing them.
12. Prefer simple, explicit and maintainable code.

When uncertain between a generic abstraction and a simple implementation required by the current milestone, prefer the simple implementation unless the abstraction is necessary to preserve an architectural boundary.

## 13. Guiding Principle

The fundamental Kavernis principle is:

> **Declare the desired state. Validate it. Understand the change. Apply it safely.**

Architecture should remain simple enough to understand, deterministic enough to reproduce, and strict enough to trust.
