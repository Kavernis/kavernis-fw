# Network architecture

Kavernis implements a local desired-state controller. The single network layout
contains `network/devices.yaml`, `network/interfaces.yaml`, and optional
`network/routing.yaml`, all at strict integer version 2.

## Boundaries

`config/network.py` defines frozen Pydantic resource schemas and parses YAML.
`config/addressing.py` retains independent family validation and DHCPv4 defaults.
Routing value schemas validate static gateway and route fields. `config/resolver.py`
creates domain values without Git, SQLite or backend knowledge.

`models/device.py` defines `NetworkDevice`, `DeviceKind`, and distinct immutable
physical, VLAN and bridge settings. The settings determine the kind, so there
cannot be conflicting kind/settings values. Bridge member collections are tuples.
`models/interface.py` defines `NetworkInterface`, `InterfaceAddressing`, and typed
IPv4/IPv6 settings. `models/routing.py` defines `Gateway`, `Route` and
`RoutingModel`. `models/network.py` aggregates tuples of these frozen values.
No domain object contains a mutable collection, rendering context or persistence
handle.

Core resolves topology before backend generation. The backend consumes this
validated aggregate and joins device identities, logical interfaces and gateways
into typed rendering contexts. Jinja2 only writes native syntax and runs with
`StrictUndefined` from packaged `templates/network/` files.

## Dependency and reference validation

Device dependencies form an explicit directed graph. A VLAN depends on its
physical parent; a bridge depends on its physical or VLAN members. Missing
references and cycles fail before generation. Each ready graph layer is sorted
by ID to produce deterministic dependency order.

Validation rejects duplicate device IDs, kernel names, L3 IDs, L3 assignments,
gateway IDs and route IDs. VLAN parent/tag pairs must be unique. Bridge members
must be unique and have only one owner. A physical VLAN parent cannot also be a
bridge member; bridge its VLAN devices instead. Neither nested VLANs nor nested
bridges nor VLANs on a bridge are supported. Any L3 assignment on a bridge member
is rejected, including an assignment with disabled families.

Gateways reference logical interfaces, not devices. Addressed gateways require
a matching enabled family and static subnet reachability, or an explicit onlink
assertion. Direct gateways have no address. Routes reference gateway IDs, have
canonical network destinations and must match the gateway/interface family.
No gateway, static route or default route is mandatory.

## Native generation and ownership

Each managed device receives a `.network` file named by device ID. Virtual VLAN
and bridge devices also receive `.netdev` files. Physical devices are adopted
and never created. L3 assignment contributes addresses, DHCP policy and routes
to its device file. VLAN attachments and bridge attachments belong to L2 and
are rendered even when the device has no logical interface.

Unassigned devices use `DHCP=no`, `IPv6AcceptRA=no`, and
`LinkLocalAddressing=no`. Disabled IPv6 also suppresses link-local addressing;
enabled IPv6 permits IPv6 link-local addressing. DHCPv4 policy always renders
all five explicit boolean defaults; the metric is rendered only if configured.
Basic DHCPv6 and SLAAC preserve their existing behavior.

Device files, child attachments and routes have stable ordering. Display names,
source document order and timestamps do not affect artifacts. Validation checks
the owned filename pattern and the constrained generated native syntax and IP
values. This is an in-process validator for the supported subset, not a full
systemd parser or a live connectivity test.

Ownership is the exact filename pattern
`10-kavernis-[a-z][a-z0-9-]*\.(network|netdev)`. The same rule drives apply,
recovery and drift inspection. Non-regular owned paths are rejected. Unrelated
files are neither removed nor treated as desired state. Obsolete owned files
are removed on apply. Recovery restores the previous artifact bytes with atomic
per-file replacement and removes newly introduced files. Native files have
explicit mode `0644`; writes and directory changes are flushed before reload.

## Desired editing and transactions

One advisory network lock covers editing, apply and rollback. Editors receive a
mode `0600` temporary copy. The correction loop keeps the same candidate and lock.
Resource validation runs before atomic replacement, preserving existing file
permissions and ownership. Fingerprints include every fixed layout path, exact
bytes and absent-file markers; document version fields are therefore covered.
There is no alternate runtime layout to select or fingerprint.

The editor deliberately checks only the edited resource. Device topology and
relationships to other documents are checked during plan/apply. This lets an
operator repair documents and update related objects across multiple edits.
Configuration-management writers must honor the same advisory lock.

## State transitions

```text
load and validate desired resources
  → resolve graph and routing
  → generate and validate native candidate
  → SQLite preflight and applied Git topology reconstruction
  → classify transition and check observed topology
  → identify/snapshot desired Git revision
  → install native candidate and reload
  → bounded verification of static Linux state and networkd association
  → record successful revision and artifact hashes in SQLite
```

A failed native transaction restores prior managed artifacts where possible,
attempts a reload and verifies the prior required static state when a matching
applied baseline exists. Failure information is recorded separately. Cancellation
is re-raised with recovery details. Runtime verification precedes SQLite success
recording; a later database failure still triggers recovery and is reported.
Git, SQLite and Linux are not one atomic transaction. The exact checks and
remaining commit/crash windows are documented in
[network application safety](network-application-safety.md).

History contains only the three permitted desired resource paths. Snapshots
preserve optional routing absence and exact bytes, including line endings.
Revision identifiers are validated before Git execution; Git uses explicit
argument arrays. Matching content reuses the current revision. No normal
operation rewrites history.

Rollback reads and validates the requested desired snapshot, performs SQLite and
structural-transition preflight before replacing live YAML, restores its resource
set, records a new changed desired revision and regenerates/reapplies.
A pre-activation YAML write or history failure attempts to restore the prior
desired resource set. It does not restore historical native files. Native failure
leaves the attempted
rollback desired state current and retains the previous successful applied
revision. Optional routing removal is reflected in both disk and history.

Status compares desired file contents with the applied revision, then compares
observed owned artifact hashes with applied hashes. Pending changes take
precedence over artifact drift. Failure details are recorded separately in
SQLite; the CLI's status categories remain the existing four categories.

## Operational limits

`SystemdNetworkdBackend` is the Debian 13/systemd 257 production activation
boundary. `networkctl reload` alone is insufficient: for planned VLAN/bridge
changes it loads target definitions without broad reconfiguration, coordinates
scoped detachment/deletion and explicit virtual-device creation, waits for link
state transitions, then reconfigures only existing affected links. The backend
observes Linux and networkd through a typed runtime inspector and core requires
static verification before success recording. This sequence is unit-tested but
still requires the gated Debian 13 VM suite for native validation.
Dynamic acquisition, reachability and continued management connectivity are not
verified. Physical device recreation, ambiguous ownership and watchdog recovery
remain unsupported and fail closed.

Replacement is atomic per file, not a crash-atomic transaction spanning all YAML
files, native files, Git and SQLite. Advisory locking cannot protect against
writers that ignore it or guarantee recovery from power loss across all stores.

The model intentionally excludes network servers, firewall/NAT, Prefix Delegation,
bonds, tunnels, advanced routing, bridge VLAN filtering and other advanced device
settings. There is no legacy parser, automatic migration or cross-version history
support. Existing development state must be archived/reset as documented in
[development cleanup](development-cleanup.md).
