# nSPAWN runbook

Start an admitted native session and read its recorded result.
A configured provider and an authenticated native client are separate prerequisites.

## Setup

Read [installation](../../INSTALLATION.md).
Run from the public repository root:

```bash
uv sync --group dev
uv run nspawn --help
uv run nspawn database create
uv run nspawn provider list
uv run nspawn mcp serve
```

The registry file is `nspawn.sqlite3` in the account-owned database directory.
Read [storage isolation](../../PRIVACY.md#files-and-retention) before a recovery test.
Create the registry only for a new account.
Register the absolute `.venv/bin/nspawn` executable with arguments `mcp serve` in the MCP client.
The stdio server waits for that client.

## Developer test prerequisite

The Linux SDK tests need Node.js and npm.
Use the existing Python workspace and a disposable Node prefix.
Run from the nSPAWN member directory:

```bash
sdk_bin=$(uv run --no-sync python tests/sdk_setup.py "$PWD/.nott/disposable/test-sdk")
export PATH="$sdk_bin:$PATH"
mkdir -p .nott/disposable/test-sdk/tmp
TMPDIR="$PWD/.nott/disposable/test-sdk/tmp" uv run --no-sync --group dev pytest tests/test_thread_list.py -q
```

The setup script declares and checks the exact adapter and SDK versions.
It keeps Node modules and its cache under the disposable prefix.
Public CI uses this same script.
Use `--check` with an existing prepared prefix to check it without installation.
These tests use synthetic native homes. They need no native login.
The native custody tests still need the host facilities that they measure.

## Configuration and native authentication

The [configuration loader](../../../packages/nspawn/nspawn/config.py) selects `~/.nott/spawn.toml`.
`NOTT_NSPAWN_CONFIG` selects another file.
The [roster loader](../../../packages/nspawn/nspawn/owner_roster.py) reads the same file.
`NOTT_NSPAWN_ROSTER` is a compatibility path only when that file is absent.
Keep the roster private. Declare existing accounts instead of copying credentials.
The [public roster template](../../../templates/nott/spawn.toml) has Claude and Codex examples.
Keep the provider you use. Set its absolute client path and existing account home.
Copy the edited template to `~/.nott/spawn.toml` only when that file is absent.
Merge into an existing roster instead of replacing it.
The template adds no plugin arguments and contains no credentials.

| Section | Fields and boundary |
| --- | --- |
| `[bins]` | Absolute executable paths keyed by native kind |
| `[defaults]` | plugins, system_prompt, model, effort, and arguments |
| `[[provider]]` | Unique name, kind, declared models, efforts, and routing ranks |
| `[provider.accounts.<alias>]` | Existing native home, optional Codex sqlite_home |
| API account | auth_env names a variable, model_provider names an existing Responses profile |
| `excluded_accounts` | Refuse excluded aliases, including explicit selections |
| `[store]` | Optional run_root, no database URL |
| `[system1]` | Classification configuration, no API key literal |

Use `nspawn provider list` to read the declared models and accounts.
Use `nspawn quota show` to read observed quota and absence reasons.
Do not treat an unavailable observation as zero usage.
A question-specific System1 route requires endpoint, model, and API key together.
Otherwise nSPAWN uses the base environment route before roster values.
Native account login remains necessary when System1 uses a local endpoint.

Linux dispatch needs its user service manager and the required sandbox facilities.
macOS dispatch uses its `launchd` path.
Read [driver.py](../../../packages/nspawn/nspawn/driver.py) and [native.py](../../../packages/nspawn/nspawn/native.py) for the current host boundary.
The current admission policy refuses new and resumed AGY sessions.
Existing admitted AGY runs remain readable and closeable.

## First useful result

Use an owned disposable project and an authenticated admitted provider.
Call `nspawn_provider_list` first. Select a provider from its result.
Call `nspawn_run_start` with that provider, the project `cwd`, a bounded prompt, and an admitted execution class.
For example, ask the session to describe one synthetic file without changing it.
Use `read_only: true` and explicit `read_paths` for that project.
Read [the schema](REFERENCE.md#mcp-tools) for native admission constraints.
Keep the returned `run_id`.
Read `nspawn_run_show` until terminal state, or use its bounded `timeout_ms`.
Read `nspawn_result_show` for the final text and any `failure_reason`.
Use `nspawn_transcript_show` for the bounded redacted transcript.
Check the recorded provider, account alias, and vendor thread ID.
A zero process exit alone does not prove a successful vendor result.

## Operation and failure diagnosis

Use `nspawn run list` to read the fleet.
Use `nspawn run show RUN_ID` and `nspawn result show RUN_ID` for a known run.
A start acknowledgement timeout can return `AdmissionPending` with a `launch_uuid`.
Resolve that identifier with `nspawn_run_show` before any repeat dispatch.
Reuse the schema's idempotency key and request when applicable.
A changed request is not an idempotent retry.

| Failure | Action |
| --- | --- |
| Provider or account absent | Correct the declared roster or native login |
| All accounts cooling | Read the named reset or account refusal |
| Sandbox prerequisite absent | Install the required host facility, preserve the refusal |
| Registry missing | Create only a new registry, or restore its backup |
| Native policy conflict | Resolve the named plugin or MCP conflict |
| Unknown process identity | Preserve custody and diagnose privately |

Close through `nspawn_run_close`. Read its checked identities and survivors.
Do not signal a bare PID or release a claim without death proof.
See [troubleshooting](../../TROUBLESHOOTING.md).

## Backup and restoration

Use `nspawn database snapshot /absolute/private-backup/nspawn.sqlite3` with a new private destination.
Preserve the run root, roster, and exact runtime version with the snapshot.
Native account homes remain vendor-owned inputs. Protect them separately.
Stop admission and finish or close owned runs before restoration.
Stop clients and drivers before replacing files.
Restore into a disposable account first. Do not restart old process identities from the backup.
Read the fleet and known terminal results with the matching runtime.
Keep the previous files until the restored account passes these checks.
`nspawn database rollback` changes schema 6 to 5 only under its terminal-run conditions.
It does not restore native sessions or replace a backup.
Read the [rulebook](../../../packages/nspawn/RULEBOOK.md) before any schema rollback.
