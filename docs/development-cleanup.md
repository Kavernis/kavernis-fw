# One-time cleanup for existing development installations

The device/interface separation is a deliberate breaking change. Only
`/etc/kavernis/network/{devices,interfaces,routing}.yaml` at `version: 2` is
supported. There is no conversion command or cross-version rollback. Follow this
procedure on the development installation being updated, never on an unrelated
host as part of running the test suite.

1. Arrange console/out-of-band access and a maintenance window. Stop automated
   Kavernis invocations and configuration-management writers; ensure no apply or
   editor operation remains active. Keep a tested host recovery procedure.
2. Make an offline backup of `/etc/kavernis`, the internal history repository,
   `/var/lib/kavernis/state.db` and any SQLite `-wal`/`-shm` sidecars, and the current
   networkd configuration. Preserve permissions. Stop writers before copying
   SQLite files so the backup is consistent. Keep backups outside both the active
   configuration tree and the internal history repository.
3. Archive the old top-level network desired files: `/etc/kavernis/interfaces.yaml`,
   `/etc/kavernis/gateways.yaml`, and `/etc/kavernis/routes.yaml`, if present.
   Move the old internal history and state database, including sidecars, into
   that backup directory. This resets development-only network history and
   applied metadata. Do not delete a user's Git repository under `/etc/kavernis`.
   The current implementation stores only network operational state; if a local
   extension stores other domains, preserve that data separately before resetting.
4. Provision the new mandatory documents, using one complete example from
   `configs/examples/*/network/` and the host's actual kernel names. Recreate
   static routes in `network/routing.yaml` only if needed. Run
   `kavernis plan network` and review every generated file before application.
5. Inventory existing native artifacts with this **read-only** command:

   ```bash
   sudo find /etc/systemd/network -maxdepth 1 -regextype posix-extended \
     -type f -regex '.*/10-kavernis-[a-z][a-z0-9-]*\.(network|netdev)' -print
   ```

   Inspect each listed file and compare it with your previous installation's
   ownership records. Never use `rm /etc/systemd/network/*` or a broad
   `*kavernis*` deletion. Files outside the exact managed pattern are unrelated
   to this cleanup and must remain untouched. Resolve any matching symlink or
   non-regular path manually; application deliberately refuses such paths.
6. Apply the reviewed new configuration using `kavernis apply network`. The normal
   owned-artifact reconciliation replaces files named by device ID and removes
   obsolete owned files that are absent from the new candidate. This is ordinary
   artifact cleanup, not filename compatibility or configuration migration. On
   native apply failure, Kavernis attempts to restore the complete prior owned
   artifact bytes and reload them; the archived old software/configuration and
   console recovery procedure remain necessary if connectivity is lost.
7. Inspect `kavernis status network`, native networkd status, addresses and routes
   from the console. Retain the backup until the new installation is verified.
   Old kernel virtual devices may require separate manual retirement or a
   controlled reboot; file cleanup and `networkctl reload` alone do not prove
   runtime convergence.

If you need to remove old native artifacts before provisioning the new model,
move only individually reviewed, confirmed Kavernis-owned files to your backup
using their explicit full paths. Do not remove them before arranging recovery:
this can disrupt the host network. Keeping them until the first validated apply
allows the built-in failure recovery to snapshot them.

New desired history and SQLite applied state are created by normal operations.
Do not copy old revisions into the new history or attempt rollback to them.
