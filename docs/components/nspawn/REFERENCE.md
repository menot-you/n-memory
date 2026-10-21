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
| `nspawn run list` | `[--owner node:<session> [--include activations] \| --group <group>]` |
| `nspawn run close` | `--owner node:<session> [--before <epoch-ms>] \| --group W` |
| `nspawn run show` | `<run-id>` |
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
| `nspawn skill install` | `<claude\|codex> <source> [--check] [--skill <name>]...` |
| `nspawn plugin install` | `claude <marketplace-source> <plugin> [--check]` |

## MCP tools

Source: [mcp.py](../../../packages/nspawn/nspawn/mcp.py), `NSPAWN_TOOL_CONTRACTS`.

| Tool | Operation |
| --- | --- |
| `nspawn_run_start` | Start an admitted native session asynchronously. |
| `nspawn_review_start` | Start a bounded review session. |
| `nspawn_batch_start` | Start a batch under admission limits. |
| `nspawn_run_list` | Read the recorded fleet. |
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
| `nspawn_run_start` | cwd | prompt, cwd, provider, model, effort, class, session_id, original_run, activation, session_operation, timeout_ms, read_only, native_write, orchestrator, read_paths, system_prompt, agent, inference_profile, idempotency_key, group |
| `nspawn_review_start` | cwd | branch, cwd, base, provider, model, effort, prompt, read_paths, system_prompt, agent, idempotency_key, group |
| `nspawn_batch_start` | prompt, cwd, providers | prompt, cwd, providers, read_paths, system_prompt, idempotency_key, group |
| `nspawn_run_list` |  | None |
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

## Runtime observations

`nspawn_run_show` reports the recorded backend and vendor thread ID.
Resume with `session_id` only when provider, account, home, and profile still match.
A read-only session cannot acquire write access on resume.
`effort.requested` and `effort.sent` come from admission.
`effort.executed` remains unknown. A sent value is not a vendor echo.

The picker excludes declared exclusions and active account walls.
It ranks admitted accounts from observed quota windows and occupancy.
An unknown or failed quota source carries an absence reason.
A provider list can succeed while the registry is unavailable.

`nspawn thread list` discovers native Claude and Codex sessions that nSPAWN did not start.
A failed home appears in `unlisted` with its reason.
Claude discovery scans a bounded recent set. Older sessions need a direct read.
External discovery preserves recorded origins before run pruning.
Pruned runs must not return as unrelated conversations.

The [rulebook](../../../packages/nspawn/RULEBOOK.md) owns layer interfaces, process custody, output bounds, and admission control.

## Source modules

| Module | Responsibility |
| --- | --- |
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
| [claude_restricted.py](../../../packages/nspawn/nspawn/claude_restricted.py) | Caged Claude writer start with Bash-only arguments, a closed environment, and pinned bubblewrap settings |
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
| `nspawn database rollback` | version 5 after the terminal-run check |

Repeated binding never releases a hold.
Admission preserves the exact node, project, decision, key, and request fingerprint.
`nspawn_run_show.admission` reports group, node, releasedGeneration, and nodeGeneration, or null.
`terminal_receipt` reports phase, reason, and time, or null when unknown.
A null receipt never means success. A terminal receipt survives close and pruning.
Schema rollback retains additive tables, triggers, bindings, reservations, and receipts for idempotent re-upgrade.
It proves no process custody or runtime restoration.
