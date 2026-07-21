# RUNBOOK — operating nmemory

Everything an operator needs to run me, check me, move me, and recover me. The
README says what I am; this says how to keep me working. Every command here was
run before it was written down — the single exception is marked NOT RUN where
it appears (it needs a second machine).

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

**One caveat — the forget key.** The first time you `memory_forget` something
(or run `nmemory sync` — it resolves the key up front), nmemory writes a
sibling key file `<db>.sqlite3.hmac-key` (mode 0600) that keys the tombstone
fingerprints. Back it up *with* the store — copy both, or the
restored store mints a fresh key and can no longer re-verify a historical
tombstone's fingerprint against the original:

```sh
cp ~/.local/state/nmemory/memory.sqlite3          backup.sqlite3
cp ~/.local/state/nmemory/memory.sqlite3.hmac-key backup.sqlite3.hmac-key  # if it exists
```

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

## Two stores, reconciled when you say so (`nmemory sync`)

The offline-first alternative to live SSH: each machine keeps its OWN store,
and you reconcile with a remote mirror file explicitly. Sync is one
owner-invoked command, opt-in every time — nMEMORY NEVER syncs in the
background, and the MCP serve path stays hermetic and zero-network. The copy
itself runs as `scp` in a separate process (the binary links no network code),
so the remote spec is anything your SSH/VPN setup lets `scp` reach:

```sh
nmemory sync --help
# usage: nmemory sync --remote <[user@]host:/path> [--db <path>] [--push]
```

One run does, in order:

1. **Fetch** the remote mirror to a private temp file — never over your store.
2. **Merge** it into the local store in one atomic transaction: content-hash
   identity (identical content collapses), relations remapped and deduped,
   forget-wins (content forgotten on either side stays forgotten),
   deterministic. The local store gains; it never loses live content except by
   a propagated forget.
3. With `--push`, copy the merged local store back over the mirror so both
   sides converge. Without it the mirror is left untouched.

`--db` names the local store; absent, the serve path's resolution order
applies (`--db` > `NMEMORY_DB` > XDG > HOME). The summary prints to stderr;
stdout stays clean. Requirements: `scp` on PATH; for a host-qualified spec,
non-interactive SSH (`ssh -o BatchMode=yes <host> true` succeeds); and the
mirror file MUST already exist — fetch comes first and fails closed, so
bootstrap a new mirror by copying your store out once (idle, both files, as
in Backup). First sync also mints `<db>.sqlite3.hmac-key` beside the local
store if absent (0600) — the key that re-keys propagated forget tombstones;
back it up with the store. `NMEMORY_HMAC_KEY` overrides the file.

### A rehearsal you can replay (run exactly as shown)

Run locally with the mirror as a plain file path — `scp` accepts both `file`
and `host:/file` specs, so the merge semantics below are the ones a two-host
run gives you; only SSH reachability is out of frame. Two seeded stores:
`laptop.sqlite3` holds one capsule, `desk.sqlite3` holds two (one shared):

```
$ nmemory sync --remote desk.sqlite3 --db laptop.sqlite3
nmemory sync · desk.sqlite3 -> laptop.sqlite3 · +1 capsules, 1 collapsed, +0 relations, 0 tombstones, remap 2

$ nmemory sync --remote desk.sqlite3 --db laptop.sqlite3   # again: idempotent
nmemory sync · desk.sqlite3 -> laptop.sqlite3 · +0 capsules, 2 collapsed, +0 relations, 0 tombstones, remap 2

$ nmemory sync --remote desk.sqlite3 --db laptop.sqlite3 --push
nmemory sync · desk.sqlite3 -> laptop.sqlite3 · +0 capsules, 2 collapsed, +0 relations, 0 tombstones, remap 2 · pushed
```

Reading the summary: `+N capsules` were new to the local store; `collapsed`
counts incoming capsules whose content the local store already held; `remap`
is the incoming-id → local-id table size (new + collapsed). The second run
adding `+0` is the idempotency check — reconciling an already-merged mirror
is a no-op.

The host-qualified form is the same command with a `[user@]host:` spec —
**NOT RUN here** (this RUNBOOK was written on one machine); it is the one
untested command block in this file:

```sh
nmemory sync --remote nott@host:/nmemory/memory.sqlite3 --push
```

### Failure modes — every one leaves the local store intact

There is no partial state: the merge is one transaction, so the local store is
either fully merged or byte-untouched. Both failures below were forced for
real; outputs verbatim, exit code 1:

```
$ nmemory sync --remote absent.sqlite3 --db laptop.sqlite3
nmemory: cannot fetch remote "absent.sqlite3": scp exited unsuccessfully (exit status: 1): cp: cannot stat 'absent.sqlite3': No such file or directory

$ nmemory sync --remote nobody@host.invalid:/nmemory/memory.sqlite3 --db laptop.sqlite3
nmemory: cannot fetch remote "nobody@host.invalid:/nmemory/memory.sqlite3": scp exited unsuccessfully (exit status: 255): scp: Connection closed
```

- **Fetch fails** (host down, wrong path, no SSH): the local store was never
  opened — nothing merged, nothing lost. Fix reachability and re-run.
- **Fetched file is not a store** (corrupt, not SQLite, stale schema): typed
  `store:` error, local store untouched.
- **Push fails**: the local store IS fully merged; only the mirror is stale.
  Re-run with `--push` when the mirror is reachable again.
- **No key source** (`no HMAC key`): set `NMEMORY_HMAC_KEY` or use a
  file-backed store so the key file can live beside the DB.
- **Rollback**: a failed run needs none (local unchanged). To undo a
  *completed* merge, restore the pre-sync file backup (see Backup) — sync
  never deletes live content on its own, so the only surprises to undo are
  additions and propagated forgets.

### Verify a sync

- Re-run the same sync: `+0 capsules` proves idempotency (transcript above).
- `memory_list` / `memory_digest` on the local store now show the union of
  both sides.
- `memory_digest` → `journal.chain` stays `ok`. Capsules merged in by the CLI
  carry no per-id audit rows, so the digest counts them under
  `journal.out_of_band` ("state without audit history") — expected after a
  CLI sync, and the difference from the `memory_merge` MCP tool, which writes
  an audit row for every id it adds or forgets.

## One-shot verbs (`nmemory recall` / `nmemory digest`)

Two CLI verbs answer a single tool call over argv/stdout and exit — no MCP
handshake, no server left running. They exist for synchronous shell callers
(hooks, scripts) where an initialize exchange is pure added latency:

```sh
nmemory recall --terms <term[,term...]> [--limit <n>] [--budget <n>] [--project-prefix <p>] [--db <path>]
nmemory digest [--project-prefix <p>] [--db <path>]
```

There is no second recall semantics: each verb routes through the SAME handler
as its MCP tool — `recall` is one `memory_retrieve`, `digest` is one
`memory_digest` — and stdout carries exactly the bytes the MCP path ships as
the tool result's text content. Side effects are identical too: a one-shot
recall bumps usage counters on what it returns and logs a miss when it comes
back empty, like any MCP recall. `--db` absent, the serve path's resolution
order applies (`--db` > `NMEMORY_DB` > XDG > HOME) — a hook with no explicit
path answers from your REAL store, so pass `--db` when you mean a different
one. The zero-network law is unchanged: the verbs open the store, answer once
on stdout, and exit.

### A rehearsal you can replay (run exactly as shown)

Against a throwaway store (`--db demo.sqlite3`, file created on first use).
Empty store first — recall abstains, it never invents:

```
$ nmemory recall --terms sqlite,fts --db demo.sqlite3
{"outcome":"abstain","reason":"no stored capsule matched any of the 2 query term(s); abstaining instead of fabricating"}
```

After one capsule captured over MCP (content "the store lives in one SQLite
file; recall is FTS5 word-exact", source `runbook-rehearsal`), the same call
grounds — and shows the word-exact law in passing: `matched_terms` lists only
`sqlite`, because the term `fts` does not match the stored word `FTS5`:

```
$ nmemory recall --terms sqlite,fts --db demo.sqlite3
{"outcome":"grounded","results":[{"label":"ADVISORY_NOT_AUTHORITY","framing":"DATA","id":"cap-1","headline":"the store lives in one SQLite file; recall is FTS5 word-exact","instruction_taint":false,"authority_class":"agent-inferred","confidence":0.6,"decayed_weight":0.6,"provenance":{"source":"runbook-rehearsal","anchor":"RUNBOOK.md:1","source_hash":"2d7cc66f0ee8dc0ce3c8bab23fd850289044e16196e2e8212e54d1117854fc8a"},"anchor_live":false,"anchor_drift":"unknown","freshness":{"valid_from":"2026-07-21T19:25:14.118104805Z","valid_to":null},"matched_terms":["sqlite"],"relevance":1.0,"bm25":-1e-6}],"matched":1,"returned":1,"trimmed":0,"trimmed_by_limit":0,"trimmed_by_budget":0,"token_budget":1500}
```

The digest is the same one-line projection `memory_digest` returns — note
`recall_misses: 2` counting the two-term miss above; a miss is logged, never
guessed around:

```
$ nmemory digest --db demo.sqlite3
{"label":"ADVISORY_NOT_AUTHORITY","framing":"DATA","total":1,"by_project":[{"project_id":"default","count":1}],"newest":[{"id":"cap-1","project_id":"default","instruction_taint":false,"created_at":"2026-07-21T19:25:14.118104805Z","headline":"the store lives in one SQLite file; recall is FTS5 word-exact"}],"most_recalled":[{"id":"cap-1","project_id":"default","instruction_taint":false,"created_at":"2026-07-21T19:25:14.118104805Z","headline":"the store lives in one SQLite file; recall is FTS5 word-exact","recall_count":1,"last_recalled_at":"2026-07-21T19:25:14.219516743Z"}],"relations":0,"open_sessions":0,"open_session_ids":[],"audit_events":1,"recall_misses":2,"dag":{"status":"ok","ready":[],"ready_total":0,"blocked":[],"blocked_total":0,"done":[],"done_total":0},"tiers":{"active":1,"archived":0,"quarantined":0},"journal":{"chain":"ok","verified":1,"out_of_band":0},"archive_candidates":0}
```

### Who calls them

The NOTT plugin's hooks are the born callers: session start injects the
digest, and every prompt injects a grounded recall for that prompt's terms —
one short-lived process per event, 0.15s wall per prompt measured against a
real store. The hooks are fail-open by law: missing binary, slow store,
abstain, or any error → zero output, and the session continues untouched.

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
