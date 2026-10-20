# nott-database

The one door to every pynott SQLite store (`nott_database`). No executable.

- `connect(path, *, readonly=False, allow_live_store=False, check_same_thread=True)` opens an existing file
  (`mode=rw`, never creates) in Python autocommit with `busy_timeout=10000`,
  `foreign_keys=ON`, `trusted_schema=OFF`, `SQLITE_DBCONFIG_DEFENSIVE`,
  `synchronous=FULL`, WAL and `query_only=ON`.
- `create(path, ddl)` is the only way a store comes into existence: a new 0600 file in a
  0700 directory, then the DDL inside one write.
- `write(conn)` lifts `query_only` and issues `BEGIN IMMEDIATE`; a nested `write()` is a
  SAVEPOINT. `begin(conn)` and `finish(conn, ok)` are its two halves. A second
  connection of the same thread asking for a held write fails at once with `BusyError`.
  Before `BEGIN IMMEDIATE`, an outer write waits in the kernel for an `flock` on the
  store's `<file>-write.lock` sidecar, released in `finish` just before `COMMIT`, so the
  lock goes to a waiter rather than back to the writer that just committed; that wait
  and SQLite's share one `busy_timeout` deadline, past which `BusyError`. A wait that
  runs out stays queued on its connection and the next `begin` resumes it, so a retry
  loop never piles up waiters. A write interrupted anywhere (even by
  `KeyboardInterrupt`) is rolled back, and a connection dropped mid-write lets its
  admission and claim go when it is collected.
- `query(conn, sql, params)` returns every row and closes its cursor, so no read
  transaction lingers.
- `lock(path, *, wait)` holds an exclusive `flock`, waiting up to `wait` seconds.
- `snapshot(conn, dest, *, group_read=False, allow_live_store=False)` copies with
  `VACUUM INTO` from the caller's read-only connection (`query_only` lifted for that one
  statement only) into a new 0600 file in a checked directory: compact, one
  rollback-journal file, no writer lock. It syncs the copy, runs `integrity_check`
  through a read-only `connect`, and only then widens it to 0640 when asked. It refuses
  a connection inside an open transaction.
- `user_version(conn)` and `load_vec(conn)` (sqlite-vec, with the SQL
  `load_extension()` function kept off throughout).
- `directory_bind(descriptor)` checks an open, private directory outside the live store and returns its Linux descriptor path.
  The caller keeps the descriptor open until all store operations finish.
  SQLite uses its native Unix file operations with the descriptor path intact.
  This keeps store files, journals, and locks inside that directory after an ancestor changes.
  Other systems refuse this operation. Ordinary pathname callers keep their existing interface.

Every open refuses a symlink, a hard-linked file, a foreign owner and a mode wider than
private. The live
store is the account's `~/.nott/state/database` from the passwd home. Opening it needs
`allow_live_store=True`. The opener refuses it when `pytest` is imported or either test marker
is nonempty (`PYTEST_CURRENT_TEST` or `PYTEST_XDIST_WORKER`). The distribution's
`pytest11` plugin points `NOTT_DATABASE_DIR` at each test's own temporary directory
and sets `OTEL_SDK_DISABLED=true` before collection. Subprocesses inherit that
setting. Telemetry tests may enable exports against a collector they own.

Errors: `BusyError`, `RefusedError` and `SchemaError`, all `DatabaseError`; `mapped()`
and `translate()` map a raw `sqlite3.Error` once.

`nott_database.captures` keeps the browser captures that the owner can attach. The root is
`captures/`, next to the store directory. It is a 0700 directory that the account owns.
Each capture is a 0600 PNG file named by the SHA-256 of its bytes, from 1 byte to 4 MiB.
`keep(data)` writes the file through a temporary name and returns its id. `read(id, offset,
length)` returns one slice and the size. It refuses a malformed id, a link, a file with
more than one hard link, a foreign owner and a wide mode. A missing capture is `Missing`.
`clear()` deletes only the capture names and returns how many captures it deleted.
The root holds at most 256 captures, so retained bytes cannot exceed 1 GiB.
Concurrent writers share the capacity check. A full root refuses a new capture.
It keeps existing captures until the owner clears them.
