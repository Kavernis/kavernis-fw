# AGENTS.md — Kavernis FW Development Guidelines

This file defines the rules that AI-assisted development MUST follow when modifying Kavernis FW.

For project goals, architecture overview, technologies and roadmap, see [README.md](README.md).

## 1. Core Architecture

Kavernis uses a declarative desired-state architecture.

The fundamental processing pipeline is:

```text
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
Jinja2 native rendering
        ↓
Native configuration validation
        ↓
Safe application
        ↓
Applied-state recording
```

This separation MUST be preserved.

Kavernis distinguishes three different configuration states:

```text
Desired State
     │
     │ /etc/kavernis/*.yaml
     │
     ▼
   Kavernis
     │
     ├──────────────► Applied State
     │                  SQLite
     │
     └──────────────► Observed State
                        Linux / native services
```

These states MUST NOT be conflated.

---

## 2. State Model

### 2.1 Desired State

The desired state describes what the user wants Kavernis to configure.

The canonical current desired state is stored as declarative YAML under:

```text
/etc/kavernis/
```

Examples:

```text
/etc/kavernis/interfaces.yaml
/etc/kavernis/firewall.yaml
/etc/kavernis/dhcp.yaml
/etc/kavernis/dns.yaml
```

The YAML desired state is the primary configuration source of truth.

Generated native configuration files MUST NOT become the source of truth.

SQLite MUST NOT replace the YAML desired state.

The desired-state files MUST remain:

* human-readable
* declarative
* reproducible
* Git-friendly
* suitable for automation
* suitable for configuration management tools such as Ansible

---

### 2.2 Desired-State History

Kavernis SHOULD maintain an internal Git repository containing the history of the desired-state YAML configuration.

The Git repository belongs to Kavernis internal state and SHOULD live under a path such as:

```text
/var/lib/kavernis/history/
```

The public configuration directory:

```text
/etc/kavernis/
```

SHOULD NOT be turned into a Kavernis-owned Git working repository.

This avoids conflicts with users who manage `/etc/kavernis` themselves using Git, Ansible, GitOps or other configuration-management systems.

The history repository MUST contain desired-state configuration only.

It MUST NOT contain:

* generated systemd-networkd files
* generated nftables rulesets
* SQLite databases
* runtime state
* logs
* temporary files
* secrets unless the corresponding secret is intentionally part of the user-managed desired state

Git is used for configuration history and auditability, not as the operational state database.

---

### 2.3 Applied State

The applied state represents what Kavernis knows it successfully applied.

Applied-state metadata SHOULD be stored in SQLite under:

```text
/var/lib/kavernis/
```

For example:

```text
/var/lib/kavernis/state.db
```

SQLite is an operational metadata store.

It MUST NOT become the primary user configuration database.

The state database may contain information such as:

* domain
* desired-state revision
* applied-state revision
* Git commit identifier
* configuration hash
* generated artifact hashes
* apply timestamp
* apply status
* failure information
* schema or state-store version

Example conceptual record:

```text
domain: interfaces
desired_revision: 9f2ab31
applied_revision: 72ad991
applied_at: 2026-09-30T18:30:00Z
status: success
```

The exact database schema SHOULD remain minimal and evolve only when required.

Do not duplicate the complete desired-state configuration into relational tables unless a concrete requirement justifies it.

---

### 2.4 Observed State

The observed state represents what actually exists on the running Linux system.

Examples include:

* systemd-networkd configuration files
* network interfaces
* IP addresses
* routes
* nftables rules
* Kea runtime state
* Bind9 configuration or runtime state

Observed state MUST be obtained from the operating system or the relevant native service.

Observed state MUST NOT be treated as authoritative desired configuration.

The generic relationship is:

```text
Desired State
     ↓
Applied State
     ↓
Observed State
```

Differences between these states may represent pending changes or drift.

---

## 3. State Responsibilities

Kavernis MUST maintain a clear responsibility boundary:

| State | Storage | Responsibility |
|---|---|---|
| Desired | `/etc/kavernis/*.yaml` | current user intent |
| Desired history | internal Git repository | revision history and audit |
| Applied | SQLite | last known successful application state |
| Generated | native files | implementation artifacts |
| Observed | Linux/native services | actual system state |

Never make generated files authoritative.

Never infer the complete desired state solely from generated native files.

Never use SQLite as a second independent source of desired configuration.

---

## 4. Desired-State Revisions

Every meaningful desired-state change SHOULD be representable as an immutable revision.

A revision MAY correspond directly to a Git commit identifier.

Conceptually:

```text
Revision A
    ↓
Revision B
    ↓
Revision C
```

The current YAML files represent the latest desired state.

When Kavernis records a new desired-state revision, the revision SHOULD contain all relevant domain files needed to reconstruct that desired state.

Revision creation MUST be deterministic and auditable.

Avoid commits generated from nondeterministic formatting or irrelevant metadata changes.

---

## 5. Rollback Semantics

Rollback MUST operate on Kavernis desired state.

Rollback MUST NOT restore old generated native files directly.

Correct rollback flow:

```text
Previous desired-state revision
          ↓
Restore desired configuration
          ↓
Create a new desired-state revision
          ↓
Validate
          ↓
Build domain model
          ↓
Plan
          ↓
Generate native candidate
          ↓
Validate candidate
          ↓
Apply
          ↓
Record successful applied revision
```

Rollback SHOULD preserve history.

For example:

```text
A ── B ── C ── D ── E
                 ↑    ↑
               current rollback to B
```

Revision `E` may contain the same desired-state contents as `B`, while preserving the fact that the rollback occurred after `D`.

Do NOT implement user-facing rollback through:

```text
git reset --hard
```

or history rewriting.

Do NOT delete newer revisions as part of a rollback.

Rollback is a new state transition, not a destructive Git operation.

---

## 6. Drift Detection

Kavernis SHOULD detect drift between expected and observed configuration.

There are two different drift classes.

### 6.1 Artifact Drift

Artifact drift compares generated native configuration with files currently present on the host.

Example:

```text
Expected generated artifact
            ↓
          hash
            ↕
Actual /etc/systemd/network/10-kavernis-*.network
```

Artifact drift SHOULD be implemented before more complex runtime drift where practical.

Generated artifact hashes MAY be stored in SQLite.

Do not store generated file contents in SQLite unless a concrete requirement requires them.

---

### 6.2 Runtime Drift

Runtime drift compares the desired or applied model with the actual live operating-system state.

Examples:

```text
Expected interface address
            ↕
Actual Netlink/networkctl state
```

or:

```text
Expected nftables rules
            ↕
Active nftables ruleset
```

Runtime drift is backend-specific.

Do not implement generic runtime comparisons that ignore backend semantics.

Examples:

* DHCP addresses are dynamic and MUST NOT be compared as if they were static desired addresses.
* SLAAC addresses are runtime-derived and require semantic comparison.
* Route ordering may not be significant.
* Native tools may normalize equivalent configuration differently.

Runtime drift detection MUST compare semantics, not blindly compare text.

---

## 7. State Store Architecture

State persistence SHOULD be implemented behind an explicit abstraction.

A possible structure is:

```text
backend/kavernis/
├── config/
├── models/
├── core/
├── backends/
├── templates/
├── state/
│   ├── __init__.py
│   ├── models.py
│   ├── store.py
│   ├── sqlite.py
│   └── history.py
└── cli.py
```

The exact structure may evolve, but the architectural boundaries MUST remain clear.

The core SHOULD depend on a state-store interface rather than directly on SQLite implementation details.

Conceptual example:

```python
class StateStore:
    def get_applied_revision(self, domain: str): ...
    def record_apply(self, ...): ...
    def record_failure(self, ...): ...
    def get_history(self, domain: str): ...
```

Git history access SHOULD likewise be encapsulated behind a dedicated component.

Do not scatter SQLite queries or Git subprocess calls throughout the core.

---

## 8. Git History Implementation

The desired-state history implementation MUST be isolated from the rest of the application.

Prefer a small dedicated abstraction, for example:

```python
class DesiredStateHistory:
    def snapshot(self, ...): ...
    def list_revisions(self, ...): ...
    def read_revision(self, ...): ...
    def diff(self, ...): ...
```

The core SHOULD reason in terms of revisions, not raw Git commands.

If Git subprocesses are used:

* use explicit argument arrays
* never construct shell command strings
* validate revision identifiers before use
* never allow arbitrary user-controlled Git options
* never rewrite history during normal Kavernis operations

A future alternate history implementation SHOULD remain possible without redesigning the core state model.

---

## 9. SQLite Guidelines

Use the Python standard-library `sqlite3` module unless a concrete requirement justifies an ORM or additional dependency.

Prefer a small explicit persistence layer.

SQLite access MUST:

* use parameterized queries
* use transactions for multi-step state changes
* explicitly manage schema versions
* fail loudly on corruption or incompatible schema
* never silently discard state
* avoid unnecessary database abstractions

Schema migrations MUST be explicit and testable.

Do not introduce an ORM merely for convenience.

The state database SHOULD remain small and understandable.

---

## 10. Apply Transaction Semantics

Applying configuration is a security-sensitive state transition.

The desired sequence is:

```text
Read desired state
       ↓
Validate desired state
       ↓
Resolve domain model
       ↓
Generate native candidate
       ↓
Validate candidate
       ↓
Record or identify desired revision
       ↓
Apply native configuration
       ↓
Verify apply success
       ↓
Record applied revision in SQLite
```

A failed apply MUST NOT be recorded as successfully applied.

If native application fails:

* restore the previous native configuration when possible
* keep the previous successful applied revision
* record failure information separately if required
* do not advance the applied revision

State-store updates and native configuration updates cannot generally participate in the same SQLite transaction.

Code MUST therefore explicitly handle partial failures.

Never assume that writing SQLite state and changing Linux configuration are atomically equivalent.

---

## 11. Layer Responsibilities

### Configuration layer

`backend/kavernis/config/`

Responsible for:

* loading YAML
* schema validation
* normalization
* conversion into typed configuration objects

It MUST NOT:

* generate native service configuration
* manage Git history
* access SQLite directly

---

### Domain models

`backend/kavernis/models/`

Models represent Kavernis concepts and network intent.

They MUST remain independent of:

* systemd-networkd
* nftables
* Kea
* Bind9
* SQLite
* Git

State persistence details MUST NOT leak into generic domain models.

---

### Core

`backend/kavernis/core/`

Contains business logic including:

* dependency resolution
* desired-state processing
* planning
* validation
* backend orchestration
* apply coordination
* rollback coordination
* revision coordination
* drift orchestration

The core MAY depend on abstract state/history interfaces.

It MUST NOT contain raw SQLite SQL or raw Git command orchestration when these can be delegated to state components.

Business logic MUST NOT be implemented in API handlers or frontend components.

---

### Backends

`backend/kavernis/backends/`

Backends translate internal domain models into native configuration.

A backend MUST consume the internal model.

A backend MUST NOT parse Kavernis YAML directly.

A backend MUST NOT treat SQLite as configuration input.

Correct:

```text
YAML
  ↓
config
  ↓
model
  ↓
backend
  ↓
rendering context
  ↓
Jinja2
  ↓
native configuration
```

Incorrect:

```text
SQLite → backend
```

or:

```text
YAML → backend
```

Backends MAY expose backend-specific observed-state inspection required for drift detection.

---

## 12. Jinja2 Rendering

Native configuration templates are grouped by functional domain.

Current example:

```text
backend/kavernis/templates/
└── interfaces/
    ├── network.j2
    ├── vlan.netdev.j2
    └── bridge.netdev.j2
```

Python is responsible for:

* validation
* semantic decisions
* resolution
* rendering context construction

Jinja2 is responsible only for native configuration syntax.

Templates MUST NOT contain business logic.

Keep templates generic and deterministic.

Use `StrictUndefined`.

Do not load templates from user-controlled paths.

---

## 13. CLI Semantics

CLI commands SHOULD expose state concepts without leaking implementation details.

Possible future commands include:

```text
kavernis status
kavernis history
kavernis diff
kavernis rollback <revision>
```

The CLI SHOULD speak in Kavernis concepts such as:

* desired
* applied
* observed
* revision
* drift

It SHOULD NOT require users to understand:

* SQLite table names
* internal Git repository layout
* raw Git object types
* backend-specific temporary files

The CLI remains a thin interface to the core.

---

## 14. Status Semantics

Kavernis SHOULD be able to distinguish states such as:

```text
desired == applied == observed
```

Meaning:

```text
in sync
```

```text
desired != applied
```

Meaning:

```text
pending changes
```

```text
desired == applied
observed != applied
```

Meaning:

```text
drift detected
```

```text
last apply failed
```

Meaning:

```text
desired state exists but the previous successful applied state remains active
```

Status evaluation belongs in the core, not in the CLI.

---

## 15. Primary Platform

The primary target platform is Debian Stable.

Preferred native components are:

* systemd-networkd — network configuration
* nftables — firewall and NAT
* Kea — DHCP
* Bind9 — DNS
* FRRouting — advanced routing
* WireGuard — VPN
* SQLite — operational state metadata
* Git — desired-state history

Do not introduce distributed state systems such as etcd unless clustering requirements explicitly justify them.

Kavernis is currently a local system controller, not a distributed control plane.

---

## 16. Object Identity

Configuration objects use two distinct identifiers:

* `uid` — immutable technical identifier, normally UUID-based
* `id` — immutable human-readable identifier used for configuration references

`name` is a mutable display attribute.

References between configuration objects SHOULD use `id`.

Neither `uid` nor `id` should be silently changed after object creation.

Historical revisions MUST preserve these identifiers exactly.

Rollback MUST NOT regenerate object identifiers.

---

## 17. Python Guidelines

Use modern Python with type annotations.

Prefer explicit typed interfaces over arbitrary dictionaries.

Use:

* dataclasses or typed models where appropriate
* immutable structures for resolved state when practical
* explicit exceptions
* deterministic functions
* minimal global state
* small focused modules

Avoid premature abstraction.

Do not introduce dependencies where the standard library is sufficient.

Expected tooling:

```text
pytest
ruff
mypy
```

---

## 18. Safety

Kavernis is security infrastructure.

Always follow:

```text
Generate
   ↓
Validate
   ↓
Apply
   ↓
Record successful state
```

Never mark a configuration as applied before native application succeeds.

Never advance the applied revision after a failed apply.

Never restore historical generated artifacts directly as a rollback mechanism.

Never rewrite desired-state history during normal operations.

Never silently discard state-store failures.

Never:

* concatenate untrusted values into shell commands
* log secrets
* silently ignore validation failures
* expose management services broadly by default
* weaken security behavior to make tests pass

---

## 19. Testing

Behavioral changes MUST include appropriate tests.

Unit tests SHOULD cover:

* configuration parsing
* models
* validation
* resolution
* planners
* Jinja2 rendering
* state-store behavior
* revision logic
* Git history logic
* rollback semantics
* drift classification

SQLite tests SHOULD use temporary databases.

Git history tests SHOULD use temporary local repositories.

Tests MUST NOT modify:

```text
/etc/kavernis
/var/lib/kavernis
/etc/systemd/network
```

on the developer machine.

Backend generation tests MUST remain side-effect free.

Integration tests may modify native system state only inside controlled or disposable environments.

---

## 20. Determinism

Kavernis behavior SHOULD remain deterministic.

The same desired state and software version SHOULD produce the same native candidate.

Generated configuration hashes SHOULD therefore be stable.

Do not include timestamps, random identifiers or host-specific irrelevant metadata in generated native files.

Historical Git commits may contain timestamps as metadata, but the desired-state file contents themselves SHOULD remain deterministic.

---

## 21. Scope Discipline

Keep changes focused.

Do not prematurely implement:

* clustering
* high availability
* distributed consensus
* remote state databases
* multi-node orchestration
* cloud control planes
* plugin systems
* multiple Linux distributions

unless explicitly requested.

The initial state-management implementation SHOULD remain local:

```text
YAML + Git + SQLite + Linux
```

Prefer a complete local vertical slice over a premature distributed architecture.

---

## 22. Git Workflow

The `main` branch is protected.

Normal development follows:

```text
feature/fix branch
       ↓
Pull Request
       ↓
Review + tests
       ↓
main
```

Do not commit directly to `main`.

This repository Git workflow is separate from the internal Git repository that Kavernis may use on installed systems for desired-state history.

Do not confuse the two.

---

## 23. Conventional Commits

All repository commits MUST follow Conventional Commits:

```text
<type>[optional scope]: <description>
```

Examples:

```text
feat(state): add SQLite applied-state store
feat(history): add desired-state Git revisions
feat(status): detect pending desired changes
feat(drift): detect modified networkd artifacts
feat(rollback): restore a previous desired-state revision
test(state): add SQLite transaction tests
docs(state): document desired applied and observed states
```

Descriptions MUST:

* be written in English
* use imperative form
* start with lowercase
* not end with a period

---

## 24. AI-Assisted Development Rules

When modifying Kavernis:

1. Preserve YAML as the desired-state source of truth.
2. Keep desired, applied and observed state distinct.
3. Use Git for desired-state history, not operational state.
4. Use SQLite for applied-state metadata, not primary configuration.
5. Never make generated native files authoritative.
6. Rollback by restoring desired state and re-running the normal pipeline.
7. Never implement rollback by restoring old native files directly.
8. Never rewrite history as part of normal rollback.
9. Keep Git and SQLite behind dedicated abstractions.
10. Do not leak persistence implementation into domain models.
11. Preserve the config → model → backend → Jinja2 pipeline.
12. Validate before applying.
13. Record applied state only after successful native application.
14. Treat drift detection as semantic comparison, not blindly as text comparison.
15. Start with artifact drift before complex runtime drift where practical.
16. Add tests for every state transition.
17. Prefer standard-library SQLite over adding an ORM without justification.
18. Prefer simple local state over distributed systems.
19. Keep changes narrowly scoped.
20. Explain significant architectural deviations before implementing them.

---

## 25. Guiding Principle

The fundamental Kavernis principle remains:

> **Declare the desired state. Validate it. Understand the change. Apply it safely.**

State management extends this principle:

> **Know what was requested. Know what was applied. Know what is running. Be able to explain and recover every transition.**

Kavernis architecture should remain simple enough to understand, deterministic enough to reproduce, auditable enough to trust, and recoverable enough to operate safely.

