# nSPAWN rulebook: the layer interfaces

[Architecture](../../docs/components/nspawn/ARCHITECTURE.md) shows service and trust boundaries.
The [reference](../../docs/components/nspawn/REFERENCE.md) lists source declarations.

The interface contract of nSPAWN's layers, L1–L8 plus L1q (the quota reading that
hangs off L1). Every layer below is built; a row states
what the layer owns, consumes, produces and refuses, and a change to one of those is a
change to this contract. The live door lists exactly the contracts in
`NSPAWN_TOOL_CONTRACTS`, each with a real non-empty `inputSchema`; no count is written
out here.

## Shared contract

- A provider's `kind` selects its executor: `claude` uses Claude Code, `codex` uses
  Codex app-server (FUGU Responses profiles included), `agy` uses AGY stream-json.
  `rows.Backend` is the closed vocabulary the registry admits.
- `requested_provider` records caller intent. `executed_provider` records the route
  actually used. `executed_account` records only a roster alias, never credential
  material. All three may be `None` when no observation exists.
- The core is synchronous. Threads are admitted only at the child I/O boundary;
  `asyncio` is not admitted.
- Every subprocess receives an argv sequence without shell interpretation. Every
  process action is authorized by `ProcessIdentity`, never by a bare PID.
- Credential values must not enter rows, MCP results, transcripts, run metadata, or
  this configuration surface. Child output is redacted before persistence.
- A missing dependency, invalid input, exhausted capacity, output limit, unknown
  process identity, or unclassifiable terminal stream is a typed refusal. There is
  no internal failure sentinel.
- L6 calls L7; L7 composes L1, L2, L3, L4, L5, and L8. This is dependency direction:
  a lower layer never imports a higher one.

## L1 — the registry

| Contract field | Locked value |
| --- | --- |
| Owns | `nspawn/schema.py` (`SCHEMA_DDL`), the row fields in `nspawn/rows.py`, and the wall methods in `nspawn/registry.py` |
| Consumes | One SQLite file opened only through `nott_database.connect`; `RunRow`; `ProviderWallRecord` and `AccountWallRecord` observations from L5 |
| Produces | `REGISTRY_SCHEMA_VERSION`, carried by `PRAGMA user_version`; live-wall reads and idempotent wall writes |
| Generations | The current generation is 6. `Registry.open` applies the additive steps in `nspawn/schema.py` once under the write lock. Version 6 adds reversible node holds, workflow bindings, key reservations and first-terminal receipts; unknown generations are refused (`UnrecognizedSchema`). The host-only `nspawn database rollback` can restore generation 5 only after every reserved run is terminal, preserving the additive rows and triggers for idempotent re-upgrade. The door never creates the file: `nspawn database create` does |
| Run columns | Nullable `requested_provider`, `executed_provider`, `executed_account` and `owner` (`node:<session>`, immutable once admitted); `system_prompt` retained |
| Wall tables | Inline DDL only: `provider_walls(provider PK, cooling_until, source, observed_at)` and `account_walls(account_alias PK, cooling_until, source, limit_type, observed_at)` |
| Source vocabulary | `vendor_reset`, `default_1h`, matching `terminal.WallSource` |
| Signatures | `Registry.get_provider_walls`; `get_account_walls`; `record_provider_wall`; `record_account_wall` |
| Wall rules | A stored wall row, of any cause, ends only at its deadline; nothing lifts it early. `record_account_wall` keeps ONE row per account and merges atomically: the later deadline wins, and a live credential or default cause outranks `vendor_reset` (a live `credential_rejected` keeps its label in either order). `get_account_walls` is `get_enforced_account_wall_rows`: per account, the latest of its live row and the latest future reset of its stored windows at utilization >= 1, however stale the reading. A reset at or before now walls nothing |
| Refuses | Unknown generation, invalid/bare credential account value, invalid wall source/time; the enclosing transaction rolls back |
| Consumers | L2 reads unexpired wall maps; L5 supplies wall records; L7 persists lifecycle and route observations |
| Columns | Every declared column must retain a row/registry writer. |
| Effort record | `admit_run(effort_profile={requested, sent})` checks the record before any write and appends it as the run's `lifecycle` event `{"effort_profile": {...}}` inside the admission's own transaction: no new table, column, event kind or schema generation. Each value is null or non-empty text; any other key (an `executed` above all) is refused. `Registry.effort_profile(run_id)` reads only the journal, never the roster: no record answers every value `unknown`, and a broken chain, a malformed record, one on another event kind, or a second record is refused (`RegistryError`), never picked from or repaired. A run admitted before the record existed stays unknown; nothing is backfilled. An admission whose own transaction failed leaves no record. |

## L1q — the quota reading

| Contract field | Locked value |
| --- | --- |
| Owns | `nspawn/quota_sources.py` |
| Consumes | Native Codex app-server rate-limit snapshots (`account/rateLimits/read` responses and notifications) |
| Produces | `SourceReading(windows, fetched_at, source, absence, stale)` — one shape whatever the vendor speaks |
| Invariant | `windows` is empty **if and only if** `absence` names a reason; the constructor refuses both other quadrants. A source that failed is NEVER an account at zero. `stale` is a typed field, never a spelling inside `source` |
| Refresh | `nspawn_quota_show` with `refresh > 0` pulls native Codex account metadata without a model turn; every other account answers from what its runs' wire recorded. A provider with `quota_source = "colab"` is never pulled by `quota()`, whatever `refresh` is: `quota()` reads the `compute_balance` row and answers at once with `compute` (balance, rate per hour, active assignments, `fetched_at`), its age in `compute_updated` and `compute_stale` past 600 s, or `compute_absence`. The last failed pull rides in `compute_failure` and `compute_failed_at`. `nspawn quota record`, which the `nspawn-quota-record` user timer runs every 60 s under `flock -n`, runs `colab usage` once (stdin closed, only HOME and PATH, 5 s timeout) and is the only writer of that row. It refuses a reading older than the stored one, and a failure never erases the last good reading. A refresh updates USAGE only, through `record_account_usage`, the one writer a run's completion also uses: each observed bucket replaces its stored row unless that row was observed later, and an unobserved bucket is kept. A same-second tie keeps the higher utilization. It reads past a `vendor_reset` wall, never past a credential or default wall, and the reported `cooling_until` is the wall the picker enforces |
| Refuses | The Codex parsers NEVER raise: an unparseable snapshot is an absence naming its own reason. The Colab reader raises `QuotaSourceError`, and `nspawn quota record` stores it as the last failure. No number is reported that was not read |
| Consumers | L7 persists observations into L1 and answers the quota verb |

## L2 — ranking and four-step picker

| Contract field | Locked value |
| --- | --- |
| Owns | `nspawn/ranks.py` and `nspawn/picker.py` |
| Consumes | `Roster`, Claude `Provider` rows, caller provider/model/effort/class, producer name, current epoch seconds, and unexpired provider/account wall maps from L1 |
| Produces | `PickerResult(requested_provider, executed_provider, executed_account, model, effort, warning)` or `AllProvidersWalled(nearest_reset)` |
| Ranking | Judgment/planning/review/implementation use descending `Provider.strength`; mechanical fan-out uses descending `thrift`; design uses descending `design`; verification excludes the producer then uses descending `strength`; deterministic ordering is stable by provider name; the fail-closed `unclassified` ranks by descending `thrift` |
| Four steps | (1) validate an explicit model — and the provider's `model_fast` — against the selected provider's declared models (the CLI's `[1m]` suffix is ignored there); (2) otherwise use provider model, then roster default model; (3) with no explicit provider and a class, rank eligible unwalled providers; (4) with neither, use the declared default provider and roster default, falling through only to eligible ranked providers when the chosen route is walled. Steps 3 and 4 and `serviceable_routes` never choose a `named_only` provider: only a caller that names it, or one of its accounts, gets it |
| Account rule | Select only a roster alias that is not walled. The result carries the alias, not a token path or token bytes. Provider and account walls are independent. A `fill_first` provider takes the first declared account below its capacity, and the score decides only when every account is full. An account with `auth_env = "ANTHROPIC_API_KEY"` and no base url sends an Anthropic API key: it is eligible for a Haiku model only, and a canvas node never takes it. |
| Signatures | `rank_providers(...) -> list[Provider]`; `pick_execution(...) -> PickerResult` |
| Refuses | Unknown provider/model/effort, no declared model, no eligible Claude provider/account, or all eligible routes walled; the last case reports the nearest future reset when one exists. A pin of an Anthropic API-key account with a model that is not Haiku, and a pool that the key rule alone empties, are refused with a message that names the key rule and no reset |
| Consumers | L7 admission and status persistence; L3 environment/argv construction |

Native API accounts (including Fugu) use the same `provider.secrets` directory
and `Provider.resolve_token_file(selected_alias)` as Claude. The selected
`<alias>.token` is read through the native owner/permission/size/symlink fence;
only its contents are injected under that account's `auth_env` name. That name
is the Codex profile's input contract, not an ambient credential source.
Missing or unsafe selected files refuse, with no ambient or other-account
fallback. The alternate `defaults.credential_env_file` loader is retired.
OAuth accounts continue to use their CLI's existing authenticated home.

## L3 — detached child custody

| Contract field | Locked value |
| --- | --- |
| Owns | `nspawn/child.py` |
| Consumes | L2's selected provider/account/model/effort, L8's binary and MCP plugin directory, caller prompt/cwd/session, one private run directory, and `SecretNeedles` |
| Produces | A `SpawnedChild` containing `subprocess.Popen[bytes]`, captured `ProcessIdentity`, separate stdout/stderr `OutputPump`s, and run directory |
| Spawn boundary | `subprocess.Popen(..., start_new_session=True)` with an argv sequence and closed environment; no shell |
| Termination | `SpawnedChild.stop_then_kill(expected)` revalidates the exact identity, sends `SIGSTOP` to its process group, then `SIGKILL`; no bare-PID fallback is admitted. The pair (and the reap after an observed exit) runs on its own thread under a reap fence: an interrupt never splits `SIGSTOP` from `SIGKILL`, and the leader is never reaped while a signal is owed. Once the signaler has started, the caller waits for it at most `SIGNALER_BOUND_SECONDS`, the reap included; a start that raises waits at most `SIGNALER_START_SECONDS` for the thread to enter; past either, `ChildIdentityError`, the kill not guaranteed sent. A reap that finds the leader already reaped by an earlier attempt's signaler under the fence returns that exit code; one whose exit observation meets ECHILD waits only while a reaper holds Popen's wait lock (at most `SIGNALER_BOUND_SECONDS`), then returns the code it stored or refuses at once. Interrupts are re-raised on a best-effort basis: Python cannot mask every bytecode, so one may escape unrecorded |
| Signatures | `build_child_env`; `compose_child_argv`; `spawn_child`; `SpawnedChild.identity/poll/wait/stop_then_kill` |
| Refuses | Environment/credential construction failure, binary/argv failure, identity capture or recheck failure, any group operation that cannot prove it still targets the captured process, a signaler thread that cannot start (`ChildIdentityError`, nothing signaled, any interrupt a note on it), a signaler that does not finish within `SIGNALER_BOUND_SECONDS` (`ChildIdentityError`; it still signals under the fence when it can), and an exit observation that meets ECHILD with no reaper storing a code (`ChildIdentityError`, at once, or past `SIGNALER_BOUND_SECONDS` while a reaper holds Popen's wait lock) |
| Consumers | L7 owns the child and calls L5 after both L4 pumps finish; CLOSE uses the same identity-bound custody path |

## L4 — bounded redacted stream pump

| Contract field | Locked value |
| --- | --- |
| Owns | `nspawn/pump.py` |
| Consumes | One binary stdout or stderr reader, run directory, `OutputStream`, `SecretNeedles`, and one shared `RunOutputBudget` |
| Produces | Separate `<run>/stdout` and `<run>/stderr` files, each mode `0600`, plus `path` and `bytes_written` readback |
| Ordering | Bound one input line; pass that line through `SecretNeedles`; reserve redacted bytes in the shared run budget; only then append. Secret-bearing bytes must never reach the file. |
| Limits | `MAX_OUTPUT_LINE_BYTES = 1 MiB`; `MAX_RUN_OUTPUT_BYTES = 64 MiB` shared across both streams, not one budget per stream |
| Signatures | `RunOutputBudget.consume`; `OutputPump.__init__/path/bytes_written/start/join` |
| Refuses | `PumpLimitExceeded` before append for a line or aggregate overflow; I/O and worker completion failures remain typed and surface from `join`. A line refusal names the stream, the line ordinal, the measured bytes read and the ceiling in force — assembled from this package's own constants and counts, never from a child byte. The size is a floor: the reader stops at the ceiling and never measures the whole line. |
| Consumers | L3 starts two pumps with the same budget; L5 reads their persisted redacted files; L7 transcript reads only that persisted output |

## L5 — terminal evidence and cooldown walls

| Contract field | Locked value |
| --- | --- |
| Owns | `nspawn/terminal.py` |
| Consumes | Redacted stdout/stderr paths, process exit code, executed provider, optional executed account alias, and current epoch seconds |
| Produces | `TerminalClassification(phase, final_text, warning, marker, provider_wall, account_wall, tokens)` |
| Wire authority | Parse the measured wire corpora (`tests/fixtures/claude-stream-wire-2.1.241.ndjson`, `2.1.250`, `2.1.251`). Rate limits live under `rate_limit_info`; fields remain camelCase; `resetsAt` is epoch seconds. Do not accept an invented flattened event as parity. |
| Success rule | A terminal stream-json observation must establish success. Exit code zero alone, empty stdout, or an error body only on stderr is failure. Signal termination is failure. |
| Walls | Vendor account limits produce `AccountWallRecord`; provider unavailability produces `ProviderWallRecord`. Both carry `observed_at` and a closed `WallSource`; no credential value is stored. |
| Signature | `classify_terminal_run(...) -> TerminalClassification` |
| Refuses | Unreadable/oversized/malformed evidence that cannot be safely classified, invalid wall data, or contradictory terminal evidence, through `TerminalClassificationError` |
| Consumers | L7 persists phase/status; L1 stores walls; L2 excludes live walls |
| Fixtures | The wire corpora are data, not an implementation shortcut. |

## L6 — the MCP replacement surface

Tool results up to 65,536 compact JSON bytes remain inline. Larger results return
JSON containing `preview` (the first 65,536 serialized bytes), `path` (an absolute
path to the complete JSON file, mode `0600`), and `bytes` (the full byte count).
Run-specific answers spill into that run's existing directory; fleet/roster
answers use a fresh private directory under the configured run root (default
`~/.nott/state/nspawn/runs`). Repeated reads create distinct files. A missing run
directory or failed write returns a typed error and publishes no artifact path.
Artifact creation and cleanup use a no-follow directory descriptor, so replacing
the run directory with a symlink cannot redirect the write. Before publishing a
path, the door checks directory/file identity and resolved containment; a changed
directory or artifact causes a typed refusal and preserves the session.
JSON serialization escapes non-ASCII text, so the preview boundary preserves
bytes without splitting a UTF-8 character; the preview need not itself be valid JSON.

`McpServer.handle` rejects tools/call params above 1,048,576 compact JSON bytes
(`MAX_MCP_PARAMS_BYTES`) with `-32602` naming the ceiling and observed size and
preserving the request ID, before invoking a tool. The stdio reader has a
separate 2,097,152-byte (2 MiB) memory bound (`MAX_MCP_FRAME_BYTES`) for its
trusted spawner. Frames above the params ceiling but within that reader bound
receive the typed refusal and the same session can answer its next request.
Above the reader bound, the session closes before parsing;
the reader is not drained or reused afterwards.

| Contract field | Locked value |
| --- | --- |
| Owns | Wiring in `nspawn/mcp.py`; the declarations are `NSPAWN_TOOL_CONTRACTS` and the inert `McpServer.nspawn_*` methods, one per contract |
| Consumes | L7 `Supervisor` methods and result types; JSON-RPC 2.0 over newline-delimited stdio through `listen.serve_one` |
| Produces | The verbs `NSPAWN_TOOL_CONTRACTS` declares, each with its non-empty input schema. Nothing may restate the set as a count: `SERVER_INSTRUCTIONS`, the listen tests and the live judges all DERIVE from it. |
| Start/review | `nspawn_run_start` and `nspawn_review_start` validate wire arguments, map wire key `class` to Python `execution_class`, call L7, and return `run_id` without waiting for terminal state. Whatever fields `RunAccepted` carries are rendered; L6 names none of them. |
| List/show | `nspawn_run_list` lists the fleet through `Supervisor.fleet()` and never long-polls; `nspawn_run_show` reads one run, and only it can long-poll. |
| Long poll | `timeout_ms == 0` is immediate; `timeout_ms > 0` blocks until a terminal phase or the deadline. The door answers one call at a time (`main.build`). |
| Quota | `nspawn_quota_show` declares no required argument: `provider` narrows the report and `refresh` bounds staleness. It calls `Supervisor.quota(provider=..., refresh=...)` and renders the whole `QuotaReport`; L1q owns the reading. |
| Providers | `nspawn_provider_list` declares one optional `provider` filter and no required argument, and renders a `ProviderReport` — per provider: models, declared `default_model` (never `defaults.model`), efforts, discovered accounts, and the `strength`/`thrift`/`design` ranks routing decides on. It is the ONE verb that never resolves a live `Supervisor`: it uses an injected one when the session holds it and calls `supervisor.provider_report` directly otherwise, because building one connects the registry. So it answers the whole roster even while the registry cannot be opened. |
| Accounts iff | A listing's `accounts` is empty **if and only if** `absence` names a reason, on the same `_refuse_half_an_answer` gate the quota windows use. An env-route provider is not a defect and never an empty tuple alone; a declared secrets directory that answered nothing is a DIFFERENT named reason. |
| Result | `nspawn_result_show` returns `run_id`, `final_text`, and observed `stderr_tail`. Whenever a run has a recorded failure, it additionally exposes `failure_reason`, equal to the status value copied from `blocked_reason`, regardless of current phase. The reason survives `nspawn_run_close`; closing does not erase a recorded failure. Runs with no recorded failure retain the byte-identical three-field shape, without a reason key. Newly recorded `_fail_run` reasons contain only the exception class and its domain nERR code when present, never raw exception details, credential values or private paths. `PluginSkillsRefused` may additionally retain its closed host-selected validation reason, never arbitrary exception detail. The other exception is `PumpLimitExceeded`, whose message this package assembles itself from its own ceilings and measured counts — stream name, line ordinal, byte totals — and which is appended after the class name so that its five distinct ceilings stay distinguishable. Child text and stderr retain their existing meaning. |
| Transcript/close | `nspawn_transcript_show` and `nspawn_run_close` return bounded redacted events from L7; close returns terminal phase and exact checked/surviving process identities, releasing the claim only after readback proves no survivor. Any supervisor can close a run after the live process-triple check proves death, without waiting for an orphan sweep or lease expiry. FAILED and CLOSED registry updates release ordinary claims in the same transaction; live custody and poisoned claims stay fenced. Closing by `idempotency_key` with its `request` releases a keyed dispatch's claim under the key lock: the key's run is closed, or, naming none, the key is written to `key_fences` and every later admission of it is refused as `KeyFenced`. With `only_unadmitted: true` (key selector only), a matching run is returned unchanged without close or claim release; a missing key is still fenced atomically. Guarded found, conflict and fenced answers echo `only_unadmitted: true`. Omitted or false keeps ordinary keyed close. Busy/store failures provide no fence proof. A fence proves no run is retained under that repository key and no later admission is possible; retention pruning means it does not prove the key never ran. |
| Rendering | `_to_jsonable` descends dataclasses, `Enum`s, tuples and lists — it does NOT descend dict VALUES. A dict-bearing L7 result field would reach `json.dumps` raw inside `_bounded_result`, which never truncates and would not catch it: L7 result types are tuples of dataclasses all the way down. |
| Envelope | Every `tools/call` answer is the spec's `CallToolResult`: `{"content": [{"type": "text", "text": <the rendered JSON>}], "isError": false}`. The payload is NEVER returned at the top level of `result` — it was until 2026-08-29, and a conforming client rendered every verb as EMPTY while every unit test read the internal dictionary happily. `test_mcp_contract.py` asserts this per declared verb, deriving arguments from each contract's own `input_schema`. A refusal is unaffected: it travels in the JSON-RPC `error` member, which is why only success went silent. |
| Cap | `_bounded_result` serializes the complete result once with compact separators and measures those bytes against `MAX_RESPONSE_BYTES` (65,536). Larger answers spill intact to a private run file; the inline JSON then carries `preview`, `path`, and `bytes`. |
| Declared, not yet built | Nothing. `Supervisor.fleet()`, `FleetPage`, `Supervisor.quota()` and `Supervisor.providers()` all landed in L7, and the `type: ignore[attr-defined]` markers this row used to describe are gone — `mypy --strict` warns on an unused ignore, which is what removed them. |
| Refuses | Invalid schema/arguments, params over `MAX_MCP_PARAMS_BYTES`, unknown tool/run, a long-poll with no run to poll, failed artifact writes, call-capacity exhaustion, long-poll-capacity exhaustion, or a typed L7 refusal. Wire frames above `MAX_MCP_FRAME_BYTES` fail the session closed at the reader. |
| Consumers | Real MCP clients and the live contract scenarios in `tests/live/` |

## L7 — supervisor and run lifecycle

| Contract field | Locked value |
| --- | --- |
| Owns | `nspawn/supervisor.py` |
| Consumes | Registry L1, picker L2, child L3, pumps L4, classifier L5, and installation/config L8 |
| Produces | `RunDirectory`, `RunAccepted`, `RunStatus`, `TranscriptPage`, `ProcessTreeReadback`, `CloseReadback`, `FleetPage`, `QuotaReport`, and `ProviderReport` |
| Admission | `execute` and `review` create a private run directory and durable run, then return `RunAccepted(run_id)` asynchronously. Review preserves the target worktree read-only. A swarm member (group `swarm:<name>`) with no `timeout_ms` gets `SWARM_TIMEOUT_MS` (2 hours). Every fresh run (no `session_id`, no orchestrator binding, no structured inference) has a walk (owner), and a failed run walks on before its terminal write and before its deadline. An unpinned review or verification walks the owner's preference order; any other unpinned run walks the other routes of its class, then the other accounts of its route; pinned, the other accounts of the pinned route, none when the pin names an account. A preference entry that names an account adds no other accounts (a `named_only` route serves only the accounts the order names). An account entry is resolved as provider plus account, in declared order. A route refusal (an account, balance, quota or rate-limit failure included: HTTP 401, 402, 403, 429 or a wall) takes the next entry each time; one other failure takes it one time per swarm member. A writer walks on only while no file or directory of its tree outside `.git` and `.nott/disposable` changed (by change time) since its first child, only while no close ended it, and only from its own unpoisoned claim that the store gives up. `result` follows the walk to the run that served and returns `alert`, which names each route that could not serve and the route that served. |
| Kind dispatch | `execute` resolves native accounts before the worker starts. `_drive_native_run` uses Codex app-server for `codex` (including FUGU Responses profiles) and AGY stream-json for `agy`; Claude uses its own installation/MCP checks. Native preparation refuses unsupported request forms before launch; correlated vendor terminal results enter the common lifecycle. Canonical plugin parity requires separate real-vendor observations; launcher availability does not establish it. |
| Status/transcript | `status` supports immediate and bounded long-poll reads; `transcript` is sequence-based and byte-bounded over already redacted stream-json lines |
| Effort | Admission records `requested` (the caller's own `effort`, null when it named none) and `sent` (`picked.effort`, the value handed to `child.build_child_env`, `claude_restricted` and `native.prepare_native`; AGY may spell it as its model's tier). `RunStatus.effort` is the `EffortProfile` `{requested, requested_source, sent, sent_source, executed, executed_source}` read back from that record, appended after every earlier field; `nspawn_run_show` and `nspawn run show` render it as JSON. Sources are `admission` (a null there is a recorded absence) or `unknown` (nothing recorded). `executed` is null and `unknown` for every backend: Claude echoes no effort, and Codex 0.159.3's `turn/started` `Turn` carries none, so no reply, roster, sent value or prose fills it. A refused record makes the status read a typed refusal. The idempotency fingerprint is unchanged. |
| Close | Discover descendants, act only through captured/revalidated `ProcessIdentity` values, apply L3 termination, read every identity back, and return checked and surviving identities. `claim_released` may be true only when `survivors` is empty; a successful transition to closed emits one terminal notice; a swarm member's notice joins the one notice of its swarm. |
| Node caller | A door whose environment sets `NOTT_NSPAWN_NODE_SESSION` (a canonical uuid, else refused by name) stamps `owner = node:<session>` on every run it admits, and reattaches by idempotency key only a run of that owner; that node is also the notice address, ahead of any messaging socket. It reaches only its own runs, by id or by idempotency key: it shows, reads, transcribes, closes, resumes and lists them (the list filters in SQL before the page cap), and its fleet holds only its own notices. Its notices are never pushed or drained, outlive pruning while undelivered and keep their run and key with them, and are the host's through `nspawn notice take/renew/ack/release`, fenced by `(run, generation, token)`; `nspawn run list/close --owner` answer for one node. `nspawn run list --group <group>` answers every run of one group, whoever admitted it, with its owner and sandbox; a node's own door refuses it. With `NOTT_NSPAWN_GRANT=read`, `execute` admits only a caged dispatch (`read_only`, no `native_write`, no `orchestrator`); review and batch starts are caged and pass. A door without either variable behaves as before |
| Signatures | `create_run_directory`; `provider_report`; `Supervisor.__init__/execute/review/status/transcript/close/fleet/quota/providers` |
| Refuses | Run-directory custody failure, unknown/terminal run transition, no executable route, child/pump/classifier failure, unprovable identity/tree state, or attempted claim release before death proof |
| Consumers | L6 is the only MCP adapter; live LD-1 through CLOSE observe this path |

## L8 — declared Claude installation and child MCP config

| Contract field | Locked value |
| --- | --- |
| Owns | `ClaudeInstallation`, `ChildMcpConfig`, and the three L8 functions in `nspawn/claude_child.py` |
| Consumes | Explicit binary path, environment override, provider binary, roster default binary, current binary identity/version probe, and one plugin directory |
| Produces | `ClaudeInstallation(binary, identity, version)` and `ChildMcpConfig(plugin_dir, server_names)` |
| Binary resolution | Precedence is explicit → environment override → provider binary → roster default. If all are absent, refuse; never resolve `claude` from ambient `PATH`. Bind the selected path to `BinaryIdentity` and the observed-compatible `Version` before use. nSPAWN verifies an existing declared installation; it does not invoke a package manager. |
| MCP config | Inspect the plugin-owned `.mcp.json`, require `nmemory`, and report server names without copying environment values or credential bytes. L3 passes only the verified plugin directory to its argv composer. |
| Signatures | `resolve_claude_binary`; `inspect_claude_installation`; `inspect_child_mcp_config` |
| Refuses | No declared binary, changed/unidentifiable binary, unsupported version, absent/malformed plugin config, missing required `nmemory`, or config requiring credential persistence |
| Consumers | L3 child construction; L7 admission; live LD-4 drives a real memory operation through the child |
| Lower rung | The version and identity helpers of `claude_child.py` (README, `nspawn.claude_child`). |

## L0 RED judge

Each scenario in `tests/live/test_contract_red.py` starts the real stdio process,
reaches its named consumer boundary, and ends in `EXPECTED RED RECOGNIZED` only for
its declared reason. Import, connection, fixture, or protocol setup failures are wrong
REDs.
