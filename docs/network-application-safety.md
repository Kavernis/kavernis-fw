# Network application safety (Lot A)

## Success means verified static activation

The network-domain lock covers planning, preflight, native mutation, runtime
verification, recovery and operation-outcome recording. `plan` stays side-effect
free and does not promise that a live transition is supported.

Apply and rollback now follow these boundaries:

1. Load/validate YAML, resolve the v2 topology, generate/validate native syntax.
2. Preflight SQLite, reconstruct the last successfully applied model from its
   immutable Git revision, and reproduce its recorded artifact hashes. Missing,
   incompatible or unreproducible history fails safely. SQLite holds metadata,
   not a duplicate desired topology.
3. Classify transitions by device ID/settings/dependencies; check observed Linux
   topology before assuming reload is safe. No kernel device is deleted.
4. Snapshot desired state, atomically replace individual managed artifacts,
   remove supported obsolete artifacts, fsync, then reload networkd.
5. Inspect actual Linux/networkd state with bounded polling. Only after the
   required checks pass, record success and artifact hashes in SQLite.

`NetworkRuntimeInspector` returns immutable typed snapshots. The Linux adapter
uses `ip -j -d link show`, `ip -j address show`, IPv4/IPv6 main-table route JSON,
and `networkctl --json=short status --all`. JSON fields follow the systemd 257
baseline; unavailable tools, unsupported output or permission errors fail
explicitly. No localized human-readable CLI output is parsed. The field formats
were checked against upstream [systemd JSON construction](https://github.com/systemd/systemd/blob/v257/src/network/networkd-json.c)
and [iproute2 link output](https://github.com/iproute2/iproute2/blob/main/lib/utils.c).

| Required property | Verification |
| --- | --- |
| Physical devices | Present; not a VLAN/bridge substituted for an adopted device |
| VLANs | Present, VLAN kind, tag, local parent |
| Bridges | Present, bridge kind, STP setting; each expected member attached |
| Managed device association | Exact generated `.network` path selected by networkd; no unverified network-file drop-ins |
| Setup state | `configured` for static-only devices; `configured` or `configuring` for devices delegating dynamic addressing |
| Static addresses | Expected IPv4/IPv6 address and prefix present; tentative or DAD-failed addresses do not count |
| Static routes | Canonical destination, gateway or direct route, output device and onlink flag; main table, static protocol, unicast |
| Retired static configuration | Addresses and complete route identities in the previous model but absent from the candidate must disappear |

Additional kernel, link-local, dynamic and unrelated routes/addresses are allowed.
Route destination alone is never used as ownership. Route protocol alone also
cannot prove exclusive ownership; if an independently configured route exactly
matches a retired managed route, apply conservatively fails instead of deleting
it. The same ambiguity exists for an identical static address independently
maintained by another agent. Kernel addresses flagged dynamic are excluded from
retired-static checks, so reacquisition of the same address does not itself fail
a static-to-dynamic transition. This flag does not identify a DHCP server or
prove SLAAC acquisition. Use exclusive management of declared static values.

DHCPv4/DHCPv6 lease acquisition, SLAAC address acquisition, DNS availability,
carrier, gateway reachability and management connectivity are **not verified**.
A dynamic device can remain `configuring` with no lease/address while activation
passes, provided the selected network file and required static properties match.
This does not certify every DHCP policy option inside networkd or prove that a
server answered. The CLI makes no dynamic-acquisition claim. Static requirements
still have to be installed; if lack of carrier prevents installation, apply times
out and reports the missing property rather than declaring success.

The application layer exposes `verification_timeout` and `reload_timeout` (15 s
each by default). Verification uses a monotonic deadline, 100 ms polling and a
shared remaining budget for all subprocesses in one inspection. Recovery has its
own bounded reload and verification budget. Filesystem I/O and the entire
multi-store operation are not guaranteed to finish within those subprocess
budgets. `status` retains its existing artifact-drift semantics; it is not a
continuous runtime monitor.

## Supported and rejected transitions

Supported: initial physical adoption, initial VLAN/bridge creation when no
unowned name collision exists, initial attachments, unchanged topology, static
address/route additions/changes/removals, and existing DHCPv4/DHCPv6/SLAAC intent.
All supported transitions still have to pass runtime verification.

Rejected before native mutation: changing device type or kernel name, changing
VLAN tag/parent, removing a VLAN or bridge, changing existing bridge membership
or STP, and retiring a physical device declaration. The last two are deliberately
conservative: reload-only detachment/reconfiguration is not demonstrated safe.
A new bridge may attach an existing unmastered physical/VLAN member; an existing
bridge's membership cannot be edited live by this backend.

New virtual names already present in Linux, missing expected physical devices,
conflicting initial bridge membership, applied topology drift, managed artifacts
without an applied baseline, and unexpected `.netdev` artifacts are also rejected.
This is fail-closed detection, not a device reconciliation engine. Errors identify
the transition and require controlled maintenance; they do not suggest arbitrary
kernel cleanup. Rollback uses the same checks **before replacing live YAML**.

## Interruption and recovery

The backend's narrow `BaseException` boundary begins at persistent native
mutation and includes reload, verification and state recording. On a catchable
failure, including `KeyboardInterrupt` and `SystemExit`, it attempts to restore
the exact prior artifact bytes, reload them and verify the prior static model.
The original cancellation is re-raised. Recovery failure is attached to that
exception and printed by the CLI; secondary outcome-recording failures do not
hide the cause.

File restoration and successful recovery reload are reported separately from
runtime recovery. Without a prior applied baseline matching the restored files,
runtime restoration is explicitly unverified. A newly created virtual device may
survive restoration; it is never automatically deleted, and recovery reports the
remaining device instead of claiming restoration succeeded. Even verified prior
static properties do not prove restored connectivity or dynamic leases.

Before native mutation, rollback interruption during YAML replacement/history
creation attempts restoration of the outgoing desired resource set. Once the
rollback revision has been created and native activation is attempted, rollback
YAML remains current and auditable even on failure. Messages identify this state;
there is no blanket “desired state unchanged” interruption message. Prior history
is never rewritten. Operation failures after successful state preflight are
recorded separately, with a nullable revision if no revision was yet identified.
Errors before a usable state store exists cannot reliably be persisted.

SIGKILL, default unhandled SIGTERM, power loss, kernel failure and repeated
interruption of cleanup cannot be recovered by this exception boundary. There is
no watchdog, remote-confirmation timer or crash-atomic network transaction.
Recovery does not guarantee preservation of an SSH session.

## SQLite preflight and remaining windows

Preflight checks the exact schema version, required tables/columns, SQLite
`quick_check`, write-reservation availability (`BEGIN IMMEDIATE`, rolled back),
and the supported applied-state/latest-outcome reads. A new store is initialized
before native mutation. An existing database is not repaired or migrated by
preflight; unsupported schemas must be handled separately. The earlier store's
v1 metadata migration remains available through its existing read API, not as an
implicit part of apply preflight.

Preflight releases its SQLite transaction before touching Linux. It cannot
reserve future disk space or prevent later disk, permission, lock or database
failures. Failure during later recording checks the supported state reads to
distinguish a pre-commit failure from a signal delivered after durable success.
A known pre-commit failure triggers native recovery. A confirmed new success
record keeps the verified native configuration and reports that interruption
arrived after commit. If the commit outcome cannot be read, recovery is withheld
rather than knowingly risking a contradiction with committed metadata; the error
requires inspection of SQLite and Linux. A separate operation failure outcome is
attempted in each case. A SQLite transaction that fails before commit retains
the old successful revision. There is no atomic transaction spanning Git,
artifacts, Linux and SQLite: termination or an exception around the final commit
can leave its durable outcome uncertain, and recovery can itself fail. A commit
that has already become durable cannot be undone merely by catching a signal.
Inspect applied metadata, actual artifacts and Linux state after such an event.

External writers must honor the advisory network lock. Observations are several
bounded tool queries, not one atomic kernel snapshot, and unrelated agents can
change the network immediately after verification. A queued networkd operation
may also continue after its client times out. These remain operational limits.

## Validation and integration boundaries

Unit tests cover JSON normalization, static requirements, dynamic pending state,
bounded waits, stale static values, topology transitions, applied-state ordering,
recovery failures, cancellations, lock lifetime, SQLite preflight and rollback
history. Simulated transaction tests are explicitly labelled; they are not proof
of kernel or networkd behavior.

Run the kernel parser/topology smoke test in a disposable user/network namespace:

```sh
KAVERNIS_RUN_NETNS_TESTS=1 .venv/bin/pytest -q tests/integration/test_runtime_namespace.py -rs
```

It creates veth/VLAN/bridge objects only after entering a new namespace and refuses
direct host-namespace execution. Namespace teardown removes them. It tests actual
kernel observations, **not** networkd activation. This Codex environment rejected
namespace creation (`Operation not permitted`), so this test was skipped.

For full native activation, use a fresh disposable Debian VM with systemd 257+
and systemd-networkd running, no existing Kavernis artifacts, and no earlier
networkd files matching the test devices. Inside that VM only, designate it:

```sh
sudo touch /run/kavernis-disposable-networkd-test-vm
sudo env KAVERNIS_DISPOSABLE_NETWORKD_VM=1 .venv/bin/pytest -q tests/integration/test_networkd_vm.py -rs
```

The test also requires root and VM detection. It creates only named test devices,
uses temporary YAML/Git/SQLite paths and `/run/systemd/network` artifacts, and
checks initial activation, static address update, desired-state rollback and
structural rejection. Cleanup is limited to fixture-created objects. It has not
been executed in this environment. Normal `pytest` skips both opt-in integrations.
Real networkd reconfiguration, timing under load, dynamic-policy acceptance and
remote connectivity remain unvalidated operational assumptions until these tests
and broader disposable-host scenarios run successfully.

Recovery has separate real-networkd scenarios. Run them only on that same kind
of disposable VM:

```sh
sudo env KAVERNIS_DISPOSABLE_NETWORKD_VM=1 .venv/bin/pytest -q \
  tests/integration/test_networkd_recovery_vm.py -rs
```

Each case creates one uniquely named veth pair (`kv-rec-a`, `kv-rec-b`, or
`kv-rec-c`) and one matching, test-owned artifact in `/run/systemd/network`.
It uses temporary desired YAML, Git history, SQLite and lock paths. Setup refuses
existing names/artifacts; cleanup removes only those exact names and reloads
networkd. The marker, opt-in variable, root, VM detection and active
`systemd-networkd` gates all remain mandatory.

The recovery cases deliberately keep networkd real. They use only narrow
in-process seams after native mutation:

* Scenario A raises from the core runtime-verification callback after candidate
  artifacts have been installed and reloaded. It proves exact artifact-byte/hash
  restoration, a second real reload, restoration of the prior static address and
  per-device routes/setup state, unchanged successful SQLite revision, and a
  separately recorded failure outcome.
* Scenario B raises from SQLite's `_before_success_commit` hook. The hook first
  observes that the new static address is active, then fails before commit. It
  proves that native convergence alone does not advance applied state and that
  normal recovery restores the former artifacts and kernel state while recording
  failure separately.
* Scenario C raises `KeyboardInterrupt` from `_after_success_commit`, after the
  durable successful record. The interruption remains visible to the caller, but
  recovery is correctly not run: the new artifacts/address and successful SQLite
  revision remain, and no failure row is added.

These tests do not prove DHCP lease acquisition, external reachability,
crash/power-loss recovery, or recovery from an indeterminate SQLite commit.
