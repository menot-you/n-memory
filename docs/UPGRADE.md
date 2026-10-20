# Backup, upgrade, and rollback

## Existing v0.3.0 data

Do not open a v0.3.0 store with the new runtime before a compatibility check.
The public version number alone does not prove that its storage schema matches this Python implementation.
Do not rename an old store into the new account directory.
Do not copy database tables between versions.

1. Stop clients and background writers that use the old installation.
2. Record the old version and executable location.
3. Use the old release's supported backup or export procedure.
4. Preserve its database, configuration, and required sidecar files together.
5. Keep the backup in a private directory. Leave the original untouched.
6. Restore the backup with the old runtime in a disposable account.
7. Check its record counts and several known records.

If the old release has no supported backup or export procedure, stop the upgrade.
Request a documented migration for that exact storage format.
The new release must either prove a migration on a copy or refuse data reuse.
A new empty installation is separate from a successful data migration.
Keep the old runtime until the restored records and recall match the expected result.

## Current Python stores

The current CLI supports a compact SQLite snapshot:

```bash
nmemory database snapshot /absolute/private-backup/nmemory.sqlite3
```

Replace the path with a new destination in your private backup directory.
The command refuses an existing destination.
This command does not establish compatibility with v0.3.0.
Keep configuration and the prior executable version with the snapshot.
Check recovery in a disposable account before replacing the active runtime.
Do not configure test stores through a persistent `NOTT_DATABASE_DIR` setting.

## nSPAWN and nBROWSER

nSPAWN supplies `nspawn database snapshot DEST` for a new private destination.
Preserve its run root, roster, and matching runtime separately.
Stop admission and owned processes before restoring registry files.
A restored registry does not restore a running native process.

nBROWSER has no snapshot command.
Stop vault users before copying `nbrowser.sqlite3` and `nbrowser.key` together.
Protect both files and preserve their original permissions.
A vault without its key cannot restore login profiles.
Temporary profiles and session references are not durable browser history.

Follow the selected component's docs/RUNBOOK.md for the exact restoration checks.
Test restoration in a disposable account before replacing an active account.
Keep the previous data until those checks pass.

## Plugin restoration

Keep the previous bundle, runtime, and private activation receipt.
The plugin runbook contains the receipt-based rollback command.
A public Codex receipt also restores native registration.
Other installation paths can need a separate native operation.
Preserve unrelated client settings.
Rollback refuses when later owned changes make restoration ambiguous.
After restoration, check a fresh session's loaded version and MCP catalog.
Complete one known memory read before accepting restoration.
