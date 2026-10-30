# nSPAWN reference

The source declarations own argument types, bounds, defaults, and refusal rules.
Use the installed executable's `--help` and MCP `tools/list` for its effective version.
A declared operation can refuse a missing prerequisite or denied grant.

## CLI commands

Source: [cli.py](../../../packages/nspawn/nspawn/cli.py), `COMMANDS`.
All commands use the executable from the public repository's root workspace.

| Command | Arguments |
| --- | --- |
| `nspawn mcp serve` | `[--host-control]` |
| `nspawn dispatch run` | `[--check \| --host-control]` |
| `nspawn review admit` | `--stdin` |
| `nspawn notice drain` | `--project <directory>` |
| `nspawn notice take` | `--owner-prefix node: [--wait-ms N] [--lease-ms L] [--limit K]` |
| `nspawn notice renew` | `--run <run-id> --generation G (--from-file F \| --stdin) --lease-ms L` |
| `nspawn notice ack` | `--run <run-id> --generation G (--from-file F \| --stdin)` |
| `nspawn notice release` | `--owner-prefix node:` |
| `nspawn database create` | `None` |
| `nspawn database snapshot` | `<dest> [--group-read]` |
| `nspawn database rollback` | `None` |
| `nspawn admission stop` | `--node node:<session> --generation N` |
| `nspawn admission resume` | `--group W --node node:<session> --project <path> --decision D --generation N` |
| `nspawn admission bind` | `--group W --node node:<session> --project <path> --decision D --generation N --key K --fingerprint <sha256>` |
| `nspawn learning export` | `None` |
| `nspawn run start` | `< <arguments.json>` |
| `nspawn review start` | `< <arguments.json>` |
| `nspawn run list` | `[--live \| --since <ISO> \| --owner node:<session> [--include activations] \| --group <group> \| --caller <socket> --json]` |
| `nspawn run close` | `--owner node:<session> [--before <epoch-ms>] \| --group W` |
| `nspawn run show` | `<run-id>` |
| `nspawn run wait` | `<run-id>` |
| `nspawn slice reconcile` | `[--dry-run]` |
| `nspawn result show` | `<run-id>` |
| `nspawn transcript show` | `<run-id>` |
| `nspawn quota show` | `[provider]` |
| `nspawn quota record` | `None` |
| `nspawn provider list` | `[provider]` |
| `nspawn route show` | `<provider> [--route <route> \| --account <alias> \| --move <providers>] [--codex-adapter <path>] [--consumer sdk]` |
| `nspawn route start` | `<provider> [--route <route>] [--codex-adapter <path>] [--cwd <dir>] [--profile <path> --profile-digest <digest> --grant-root <dir>] [--check]` |
| `nspawn route list` | `claude --model <model> --effort <effort> [--exclude <JSON array>]` |
| `nspawn route switch` | `claude --route <route> --model <model> --effort <effort>` |
| `nspawn route wall` | `<claude\|codex\|agy> --route <route>` |
| `nspawn thread list` | `[--limit N] [--exclude -]` |
| `nspawn thread show` | `<provider>:<id> [--messages --claude-home DIR]` |
| `nspawn session serve` | `None` |
| `nspawn session list` | `[<vendor>] [--id <id>]... [--since <ISO>] [--count]` |
| `nspawn session show` | `<vendor> <id>` |
| `nspawn skill install` | `<claude\|codex> <source> [--check] [--skill <name>]...` |
| `nspawn plugin install` | `claude <marketplace-source> <plugin> [--check]` |

`nspawn run start` and `nspawn review start` read the corresponding MCP tool's arguments unchanged from stdin.
Each command opens a new stdio door of the installed runtime in the caller's working directory.
The door validates the arguments. Use `"notice": false` to start without a notice.
Each command prints the tool's answer as one JSON line on stdout.
Exit 0 means that the door answered. Exit 2 means invalid CLI input or a refusal by the door.
Exit 1 means a door start, transport or deadline failure.
A refusal prints its message and structured data on stderr.
If the data names a `launch_uuid`, give it to the `nspawn_run_show` tool as `launch_uuid` before another start.
The `nspawn run show` command takes a run id only.
A failed call does not prove that no run started.

## MCP tools

Source: [mcp.py](../../../packages/nspawn/nspawn/mcp.py), `NSPAWN_TOOL_CONTRACTS`.

| Tool | Operation |
| --- | --- |
| `nspawn_run_start` | Start an admitted native session asynchronously. |
| `nspawn_review_start` | Start a bounded review session. |
| `nspawn_batch_start` | Start a batch under admission limits. |
| `nspawn_run_list` | Read nonterminal runs and terminal runs with undelivered notices. Use `include_terminal=true` for the full fleet page. |
| `nspawn_run_show` | Read run status with optional bounded waiting. |
| `nspawn_result_show` | Read final text and recorded failure reason. |
| `nspawn_transcript_show` | Read bounded redacted transcript events. |
| `nspawn_run_close` | Close through process identity checks and read custody back. |
| `nspawn_route_ping` | Check route readiness. |
| `nspawn_quota_show` | Read quota observations or absence reasons. |
| `nspawn_usage_show` | Read recorded tokens for 30 local days by node identity. |
| `nspawn_judgment_estimate` | Estimate a numerical answer with uncertainty. |
| `nspawn_judgment_choose` | Choose against declared criteria. |
| `nspawn_judgment_score` | Score on an ordered spectrum. |
| `nspawn_provider_list` | Read roster declarations without requiring the registry. |

### Input fields

The linked schema gives the complete constraints for these fields.
Conditional requirements still apply when the required column is empty.

| Tool | Required fields | All input fields |
| --- | --- | --- |
| `nspawn_run_start` | cwd | prompt, cwd, provider, model, effort, class, session_id, original_run, activation, session_operation, timeout_ms, read_only, native_write, red_record, read_paths, system_prompt, agent, inference_profile, idempotency_key, group, operations, notice |
| `nspawn_review_start` | cwd | branch, cwd, base, provider, model, effort, prompt, read_paths, system_prompt, agent, idempotency_key, group, operations, notice |
| `nspawn_batch_start` | prompt, cwd, providers | prompt, cwd, providers, read_paths, system_prompt, idempotency_key, group, operations |
| `nspawn_run_list` |  | `include_terminal` (boolean, default false) |
| `nspawn_run_show` |  | run_id, timeout_ms, launch_uuid, idempotency_key, request |
| `nspawn_result_show` | run_id | run_id, timeout_ms |
| `nspawn_transcript_show` | run_id | run_id, since_seq, max_bytes, frame_types |
| `nspawn_run_close` |  | run_id, idempotency_key, request, only_unadmitted |
| `nspawn_route_ping` | cwd | cwd, providers, deadline_ms |
| `nspawn_quota_show` |  | provider, refresh |
| `nspawn_usage_show` | timeZone | timeZone |
| `nspawn_judgment_estimate` | state, questions | state, questions, model |
| `nspawn_judgment_choose` | state, questions | state, questions, model |
| `nspawn_judgment_score` | state, questions | state, questions, model |
| `nspawn_provider_list` |  | provider |

### Read paths

`read_paths` requires `read_only=true`. Reviews and batches are always read-only.
nSPAWN refuses `native_write` with `read_paths` before it creates a run, claim or child.
Drop `read_paths` for a native writer. It uses ordinary native access without this field.
Or use `read_only=true` without `native_write` for a reader.
The same rule applies to `session_id` resumes and `original_run` continuations.

### Writer admission evidence

`nspawn_run_start.red_record` is the clone-relative producer path
`.nott/plans/<plan>/red/<unit>.json`. Implementation native writers require it;
verification native writers do not. Every other native-writer class is refused.
Resolved continuation/session scope controls the rule, not a caller relabel.
The locator participates in idempotency identity only when supplied. Reattachment
returns the original admission without reopening its files; a new attempt checks
current HEAD and requires the reference explicitly.

Generate evidence with `nott plan test` at the exact full clone HEAD on a known
containing origin branch. Missing-reference refusals name the recovery locations
`.nott/plans/writer-red/tests/unit.md` and `.nott/plans/writer-red/red/unit.json`.
No admission handler fetches or publishes. Initial workflow drift requires the
printed revise-and-readmit procedure; refreshing JSON cannot resume that workflow.
Cure/repair/base-update waits retain the same builder and need new reviewed-head
RED before continuation.

Only `run list --since` adds these four fields to its existing rows:

| Field | Meaning |
| --- | --- |
| `execution_class` | Recorded profile class, or null for an unknown profile. |
| `red_admission` | `valid`, `missing`, `invalid`, `unknown`, or `not_required`. |
| `red_admission_reason` | Bounded cause for missing/invalid/unknown; otherwise null. |
| `red_receipt` | For valid evidence only: `{record, commit, unit, tests, admitted_at}`; full commit, exact integer test count and admission-event epoch milliseconds. Otherwise null. |

The route includes all retained authorized terminal and verification rows without
the live fleet page cap. A verification writer remains visible as
`native_write=true`, `execution_class="verification"`, `not_required`. Historical
other-class writers also remain visible but cannot be newly admitted. Missing or
unreadable profiles are `unknown`, not an inferred read-only exemption. Failed or
partial reads return a typed nonzero error, never successful partial JSON.

### Optional operations

`nspawn_run_start`, `nspawn_review_start` and `nspawn_batch_start` accept
`operations`. A batch sends the same explicit declaration to every member.
The public schema is `operations.SCHEMA`; direct supervisor and detached-driver
requests validate the same declaration. Command, activation and session-operation
contracts do not accept it.

| Version-1 field | Required shape |
| --- | --- |
| `version` | Integer `1`, not a boolean |
| `tools` | Unique exact facility names; `shell` names a general shell facility |
| `paths` | Unique `{base, path, access, scope}` objects |
| `network` | Unique `{host, port}` objects; literal hostname/IP and integer port 1–65535 |
| `hosts` | Unique `{host, transport}` objects; transport is `ssh` |
| `actions` | Unique action kinds, or the targeted forms below |

Every collection is required and bounded to 128 entries. Unknown fields and
variants are invalid. The request obeys the existing wire byte ceiling; the safe
durable witness obeys the existing 16 KiB observation ceiling.
`base` is `cwd`, `temp` or `absolute`; `access` is `read` or `write`; `scope` is
`path` or `subtree`. Logical bases resolve against the selected tool's real
locations, not the caller's home. Paths must be literal: no wildcards,
interpolation, tilde expansion, backslash ambiguity or parent traversal.
The declaration does not establish access to a path or its descendants.

Actions are `file.read`, `file.write`, `process.exec`, `git.commit`, `git.push`,
`host.install` and `service.change`. A string declares its action ceiling; declare
required paths/destinations separately. Targeted forms have exactly `kind` and
`target`:

- File access, installation and process execution take a path object. Its access
  must match the action (`read` for file reads/execution, `write` otherwise).
- `git.commit` takes `{"branch": "topic"}`; `git.push` takes
  `{"branch": "topic", "remote": "origin"}`.
- `service.change` takes a literal service-name string.

Targets add requirements; they never grant authority. The writer cage matcher
is removed. New worker runs report unchecked admission for every declaration:

```json
{"operation_admission": {"checked": false, "reason": "operations_unchecked: declared-operation compatibility was not checked"}}
```

Nonempty declarations do not select or prove a compatible launch policy.
Request fingerprints still distinguish omitted, empty and nonempty declarations.
Node cages retain their existing enforcement independently.

Historical checked observations remain readable from the admission journal.
A checked observation records an old admission-time answer, not current access,
continuous enforcement, a serving tool or task success. Duplicate, malformed
or broken-chain witnesses still produce a typed read failure.
Keyed reattachment returns recorded evidence without checking today's policy.

## Runtime observations

`nspawn_run_show` reports the recorded backend and vendor thread ID.
Resume with `session_id` or `original_run` only when provider, account, home, and profile still match.
nSPAWN automatically closes only `completed` or `failed` origins through the same checked custody path as `nspawn_run_close`.
An `orphaned` origin still refuses. Read the run, then close it explicitly after its process tree is gone.
A live origin, surviving process tree, or undrained writer slice prevents continuation.
A node can resume only its own sessions. nSPAWN checks ownership before it reads the origin profile.
An ownership refusal gives constant text. It does not repeat the supplied `session_id`.
A changed profile refusal names each changed field, but gives no recorded or requested values.
To continue, repeat the recorded profile or start a new run.
A read-only session cannot acquire write access on resume.
`effort.requested` and `effort.sent` come from admission.
`effort.executed` remains unknown. A sent value is not a vendor echo.

The picker excludes declared exclusions and active account walls.
It ranks admitted accounts from observed quota windows and occupancy.
An unknown or failed quota source carries an absence reason.
A provider list can succeed while the registry is unavailable.

`nspawn thread list` discovers native Claude and Codex sessions that nSPAWN did not start.
A failed home appears in `unlisted` with its reason.
On a host with the journal writer user unit, Claude rows and Claude history come from the shared journal reader.
Codex rows keep the app-server reader on every host.
A host without that unit keeps the vendor readers and their current limits.
External discovery preserves recorded origins before run pruning.
Pruned runs must not return as unrelated conversations.

The [rulebook](../../../packages/nspawn/RULEBOOK.md) owns layer interfaces, process custody, output bounds, and admission control.

## Source modules

| Module | Responsibility |
| --- | --- |
| [operations.py](../../../packages/nspawn/nspawn/operations.py) | Optional declarations and historical admission evidence readers |
| [limits.py](../../../packages/nspawn/nspawn/limits.py) | the shared byte and cardinality ceilings |
| [errors.py](../../../packages/nspawn/nspawn/errors.py) | the `nERR-NNNN` catalog and `NSpawnError`, this package's one error root |
| [config.py](../../../packages/nspawn/nspawn/config.py) | Configuration path and store options |
| [process_identity.py](../../../packages/nspawn/nspawn/process_identity.py) | the pid triple a freed pid cannot impersonate |
| [wire.py](../../../packages/nspawn/nspawn/wire.py) | client identity, sandbox and approval policy, JSON-RPC frames |
| [rows.py](../../../packages/nspawn/nspawn/rows.py) | the durable run vocabulary and its write-column sets |
| [schema.py](../../../packages/nspawn/nspawn/schema.py) | one generation, one SQLite file (`SCHEMA_DDL`) |
| [redact.py](../../../packages/nspawn/nspawn/redact.py) | exact-secret redaction for executor output |
| [mcp.py](../../../packages/nspawn/nspawn/mcp.py) | Schema validation, MCP tools, bounded responses, and typed refusals |
| [cli.py](../../../packages/nspawn/nspawn/cli.py) | the one executable `nspawn <noun> <verb>`: `mcp serve` serves stdio (a bare `nspawn` is a usage error) |
| [claude_restricted.py](../../../packages/nspawn/nspawn/claude_restricted.py) | Claude node launch composer: confined bootstrap, runtime inventory and private endpoint check |
| [main.py](../../../packages/nspawn/nspawn/main.py) | the stdio door on the official MCP Python SDK (`mcp`), one session per process, reached through `cli.py` |
| [driver.py](../../../packages/nspawn/nspawn/driver.py) | the per-call driver: every dispatch runs in its own process and transient user scope, so a run outlives the session that started it |
| [notify.py](../../../packages/nspawn/nspawn/notify.py) | Terminal notices, delivery claims, and acknowledgements bound to a run, generation, and token |
| [worktree.py](../../../packages/nspawn/nspawn/worktree.py) | repository-root discovery and the observed-effect read |
| [registry.py](../../../packages/nspawn/nspawn/registry.py) | the instance row, the one-writer claim, the run lifecycle, and the reaper |
| [owner_roster.py](../../../packages/nspawn/nspawn/owner_roster.py) | the provider roster from `~/.nott/spawn.toml`, expansion |
| [install.py](../../../packages/nspawn/nspawn/install.py) | skill and plugin install for the node host, its staged check, and the admission of installed Claude roots (see below) |

## Skills, plugins, and native children

[install.py](../../../packages/nspawn/nspawn/install.py) stages skill or plugin checks in disposable homes.
Admission refuses unsafe paths, symbolic links, and unsupported manifest fields.
An install preserves existing installations. A failed new install restores its prior records.
Codex accepts skills only. AGY has no installation target through these verbs.
Native registration and account authentication remain separate from file admission.

[claude_child.py](../../../packages/nspawn/nspawn/claude_child.py) checks the declared binary and child MCP configuration.
It resolves the explicit binary before the environment, provider binary, and roster default.
It never substitutes an ambient PATH lookup for an absent binary.
It checks binary identity around the version probe. It reports the observed version without a fixed approved-version list.
MCP environment entries must contain variable references rather than credential values.
[child.py](../../../packages/nspawn/nspawn/child.py) and [native.py](../../../packages/nspawn/nspawn/native.py) consume those checks.

## Workflow controls

Coordinator CLI commands bind a workflow to a node, project, decision, generation, and request fingerprint.
A node-environment caller cannot use coordinator mutations.
A Stop holds admissions until an exact-generation resume releases that workflow.
The final admission transition checks the reservation and hold in the same transaction.
A schema rollback refuses live reserved runs and preserves additive data for re-upgrade.
Read the rulebook before using these host coordinator commands.


## Claude child reference

The Claude child helpers do not compose Codex arguments.
They report the installed version and preserve the binary identity boundary.

| Surface | Result and boundary |
| --- | --- |
| `Version`, `parse_version` | Parse the observed native version string |
| `ClaudeChildError` | Root for binary identity, absent binary, and MCP configuration failures |
| `BinaryIdentity`, `binary_identity` | Device, inode, size, and modification time |
| `mint_session_id` | Generate a UUID before a vendor effect |
| `resolve_claude_binary` | Explicit path, environment override, provider binary, then roster default |
| `inspect_claude_installation` | Compare file identity before and after the version probe |
| `inspect_child_mcp_config` | Check bounded regular-file MCP configuration and required servers |
| `FORBIDDEN_CLAUDE_FLAGS`, `SETTING_SOURCES` | Restrict native flags and settings sources |
| `check_claude_version` | Return the injected probe result, refuse an unwired probe |

An absent or relative binary winner refuses instead of searching ambient PATH.
The MCP check refuses credential literals. It accepts environment variable references.
The custody implementation uses process identity and process-group signals, without a separate process guardian.

## Coordinator readbacks

These operations remain host coordinator CLI commands.
They refuse before opening the store when `NOTT_NSPAWN_NODE_SESSION` or `NOTT_NSPAWN_GRANT` exists, including an empty value.

| Command | Readback |
| --- | --- |
| `nspawn admission stop` | node, generation, and still-held groups |
| `nspawn admission bind` | group, node, project, decision, releasedGeneration, nodeGeneration, held |
| `nspawn admission resume` | Binding fields after exact-generation release of one workflow |
| `nspawn run close --group W` | group, held or released status, and closed runs |
| `nspawn slice reconcile` | `dry_run`, `counts` by action, and each slice by action (`stopped`, `would_stop`, `kept`, `refused`) with its run, phase, detail and `claim_released` |
| `nspawn database rollback` | version 5 after the terminal-run check |

Repeated binding never releases a hold.
Admission preserves the exact node, project, decision, key, and request fingerprint.
`nspawn_run_show.admission` reports group, node, releasedGeneration, and nodeGeneration, or null.
`terminal_receipt` reports phase, reason, and time, or null when unknown.
A null receipt never means success. A terminal receipt survives close and pruning.
`nspawn_run_show.agent` and `skills` report the agent file and skills a Claude launch composed, `none` and [] for a Claude or AGY run with no agent, or null when the launch recorded none.
Schema rollback retains additive tables, triggers, bindings, reservations, and receipts for idempotent re-upgrade.
It proves no process custody or runtime restoration.

## Session journal

`nspawn session list [<vendor>] [--id <id>]... [--since <ISO>] [--count]`
prints session totals. A list entry has no `native_activity`. Only the footer
of `nspawn session show` carries it, so the list reads no import activity.
Each entry carries `source_home`: the `source.home` of the session's one
`session` row, the value that `nspawn session show` prints, or null when that row
has none. Each entry also carries `source_home_refusal`, null or one of two values:

- `session row is not unique`: the session has no `session` row or more than one.
- `session identity does not match`: the row's `vendor` or `id` is not the
  session's.

With a refusal, `source_home` is null. The reader gets both from the query that
checks the row's format. That query reads at most two `session` rows through the
partial index `journal_session_row`, so a list entry reads no other journal row.
The writer adds this index when it opens the store. Until then, a list reads all
the rows of each session to find a second `session` row.
`--since` includes sessions whose recorded activity time is at or after the bound.
Use the journal's UTC ISO timestamp form. The reader applies the time and vendor
filters before it reads usage totals, formats or titles.
`--id <id>` needs `<vendor>`, and `<id>` is a journal id. It keeps the session
`<id>` and each session whose id starts with `<id>.`: the copies of that
session in other homes, a copy without metadata included, and its subagents.
Each `--id` adds one session family. A session that two values match appears
once. The filter searches the primary key of `journal_session`, so its cost
grows with the matched sessions, not with the journal history. It composes
with `--since` by AND.
`--count` ends the reply with one more line, `{"count":N}`, where N is the
number of session lines before it. A complete empty list is the single line
`{"count":0}`. A reader that gets no count line, or a count that does not
match, has an incomplete reply. Without `--count`, the reply has only session
lines. Any other form prints the usage text and exits 2.

The [rulebook](../../../packages/nspawn/RULEBOOK.md#lj--the-session-journal) owns the writer contract.
The journal translates Claude Code sessions only. GPT runs through the Claude client, so its sessions are Claude sessions.

`journal.show.active_rows(conn, vendor, id)` returns a session's active conversation.
It pages the stored rows and passes them, with the stored `journal_link` entries, to `journal.show.active_chain(rows, links=())`.
A link has `uuid`, `src`, and, when recorded, `parentUuid` and `logicalParentUuid`.
A null parent marks a root. A missing parent field marks legacy data.
The projection follows the latest native leaf through logical compaction parents.
It keeps tool and reasoning rows of the selected messages.
It raises `ChainIncomplete` if a required link is absent.
Stored rows keep file order. An abandoned branch is only not returned.

`thread show --messages --claude-home` reads only a journaled home when the writer is installed.
It keeps the existing message response and tail limits. It imports no source on demand.
It finds the session by its vendor id, so its cost does not grow with the journal.
`thread list` and `thread show` leave out a session whose transcript file is gone, as the SDK reader did; its stored history stays readable.


## Counted review heads

Give `nspawn_review_start` both `subject` and `candidate` to count a review.
The candidate is a full 40- or 64-hex value and must occur whole in `prompt`.
Use a repository-qualified artifact lineage, never main or master. A present
`.nott-review-sha` must be a regular file with the candidate's value.

`nspawn review admit --stdin` accepts exactly one line of the form
`Candidate: <subject>@<40 or 64 hex>` and at most one line of the form
`Spec revision: <64 hex>`. Indent quoted copies. It returns JSON with
`subject`, `candidate`, `budget`, `heads` and `counted`. `budget` is always 1.
It checks no snapshot marker and starts no model. A refusal exits 2 and names
`ReviewCandidateInvalid`.

Review rounds have no limit. `heads` counts the distinct heads of the subject.
Repeated readers and retries of one head count once. A revised plan uses a new
version subject. A compile round uses `<plan subject>/plan.py` and names its
PLAN.md revision. A call without subject and candidate opts out for older
consumers.
