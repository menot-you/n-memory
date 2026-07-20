# RUNBOOK — operating nmemory

Everything an operator needs to run me, check me, move me, and recover me. The
README says what I am; this says how to keep me working. Every command here was
run before it was written down.

## Install

Release binary (linux x86_64 today; more targets arrive when the repo goes
public and cross-builds arm):

```sh
curl -fsSL https://no.tt/install | sh
```

Or from source (Rust stable, pinned by `rust-toolchain.toml`):

```sh
cargo build --release   # target/release/nmemory, a single ~8MB binary
```

Health check at any moment:

```sh
nmemory --version       # prints: nmemory <semver>
```

## Register with your agent

```sh
claude mcp add nmemory -- /path/to/nmemory --project <your-project>
```

`--project` is the scope your captures live under. Unregister anytime:
`claude mcp remove nmemory`. Any MCP client works — stdio, no daemon, no port.

## Where the store lives

One SQLite file. Resolution order, first hit wins:

1. `--db <path>` flag
2. `NMEMORY_DB` env var
3. `$XDG_STATE_HOME/nmemory/memory.sqlite3`
4. `~/.local/state/nmemory/memory.sqlite3`

I print the chosen path on startup — read it there instead of guessing.

## Backup and restore

The store is one file; the backup is that file. The startup line names it —

```
nmemory 0.1.0 serving stdio · db /home/you/.local/state/nmemory/memory.sqlite3 · default project <id>
```

— so copy exactly that path:

```sh
cp ~/.local/state/nmemory/memory.sqlite3 backup.sqlite3
```

Copy while your agent is idle (SQLite WAL makes a mid-write copy *unlikely* to
corrupt, but idle is free). Restore = put the file back. `memory_export` is a
deterministic *generated view* for reading and diffing — useful alongside a
backup, not a substitute for the file.

## One store, two machines (SSH)

The store is single-host by design. To use the SAME memory from a second
machine, register the remote binary as the MCP command — stdio rides SSH, the
binary stays hermetic, your VPN (e.g. Tailscale) does transport and auth:

```sh
# on the second machine:
claude mcp add nmemory -- ssh <user>@<host> /path/to/nmemory --project <your-project>
```

Requirements: non-interactive SSH to the host (`ssh -o BatchMode=yes <host>
true` succeeds). Properties: one store (the host's), both machines see every
capture live; concurrent sessions behave exactly like concurrent local
sessions (same WAL semantics). Costs: recall latency = your network RTT; host
down or offline = memory unavailable — your agent degrades to no-memory, it
does not corrupt. Verify the path end to end:

```sh
ssh <user>@<host> /path/to/nmemory --version
```

## Upgrade

Replace the binary; re-run `nmemory --version`. The on-disk Capsule v1 schema
is frozen — an upgraded binary opens an existing store; a downgraded one may
refuse features, never silently rewrite.

## When something looks wrong

- **Recall says `abstain` for something you stored** — recall is word-exact by
  design; expand terms in the call or teach an alias (`memory_alias`). A miss
  is logged, never guessed around.
- **`missing_evidence` outcome** — matches existed but every one was excluded
  (superseded, falsified, expired); the response counts the reasons. That is
  the store being honest, not broken.
- **`not_yet_valid` / expired surprises** — check the capsule's freshness
  window via `memory_get <id>`.
- **A capsule you forgot still answers** — it answers as a tombstone marker,
  content gone. That is the contract, not a leak.
- **Store won't open ("file is not a database")** — the DB path points at a
  non-SQLite file or a directory; I fail closed with a typed error, never
  half-open. Fix the path; nothing was written.
- **Disk full / quota (`EDQUOT`)** — writes fail closed; captures are rejected,
  nothing corrupts. Free space and retry; the journal chain stays verifiable
  (`memory_digest` → `journal.chain`).
- **Suspected tampering** — `memory_digest` verifies the audit journal chain on
  every call and reports `chain: ok` or the break; `memory_get` shows each
  capsule's `last_mutation` (who, when, what).

Diagnostics go to stderr; the protocol stream on stdout stays clean JSON-RPC.

## Uninstall

```sh
claude mcp remove nmemory
rm -rf ~/.local/state/nmemory   # deletes the memory itself — no cloud copy exists
```

The second line is the whole story: delete the file and the memory is gone,
because it never lived anywhere else.
