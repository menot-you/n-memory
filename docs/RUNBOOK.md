# nMEMORY runbook

Capture a decision with its source. Recover it in a later session with the same project ID.
Read [installation](INSTALLATION.md) before you change an existing account.

## Setup

Use Python from `.python-version` and the root `uv.lock`.
Run from the public repository root:

```bash
uv sync --group dev
uv run nmemory --help
uv run nmemory database create
uv run nmemory mcp serve --project example
```

The data file is `nmemory.sqlite3` in the account-owned database directory.
Read [storage isolation](PRIVACY.md#files-and-retention) before a recovery test.
Create the database only for a new account. The command refuses an existing destination.
The stdio server waits for an MCP client.
Register the absolute `.venv/bin/nmemory` executable with arguments `mcp serve --project example` in that client.
Restart the client. Compare its `tools/list` result with the [reference](REFERENCE.md).

## Configuration

The [loader](../nmemory/config.py) reads `~/.nott/memory.toml`.
`NOTT_NMEMORY_CONFIG` can select another file.
The loader checks `[system1]` and `[curator]`.
It ignores the retired `[store]` and `[github]` tables.
Do not configure a persistent database override.

| Choice | Configuration | Effect |
| --- | --- | --- |
| Lexical setup | `nmemory project bootstrap --root /absolute/project --project example --system1 lexical` | Configure a project without a System1 model |
| Local model | The same command with `--system1 local --endpoint URL --model M` | Check the explicit compatible endpoint |
| System1 | `[system1]` protocol, endpoint, model, timeout_seconds, curator | File values precede the base environment route |
| Question route | `NOTT_SYSTEM1_<QUESTION>_{ENDPOINT,MODEL,API_KEY}` | Set all three values together |
| Curator | `nmemory curator set --provider P [--account A]` | Select a supported inference route |
| Embeddings | `nmemory embedding show --json` | Read the active space and coverage |

Never place an API key in TOML or process arguments.
The [privacy guide](PRIVACY.md) separates local storage, embedding, classification, and curator processing.
A local embedding service does not make the agent client local.

## First useful result

Use synthetic data in project `example`.
Call `nmemory_capsule_ingest` in the MCP client:

```json
{
  "content": "Decision: the example project uses SQLite.",
  "source": "user-request",
  "anchor": "The example project uses SQLite.",
  "kind": "decision",
  "authority_class": "user-stated",
  "project_id": "example"
}
```

Keep the returned capsule ID.
Call `nmemory_capsule_pin` with that ID, `pinned: true`, and a reason.
Start a fresh session with project `example`.
Call `nmemory_context_show` with `{"query":"example decision","project_id":"example"}`.
Check the pinned decision and its source.
Call `nmemory_capsule_retrieve` with `{"terms":["SQLite"],"project_id":"example","lane":"term"}`.
Read the returned evidence and receipt.
Capture a changed decision with `supersedes` set to the first capsule ID.
Repeat recall. Check that the first decision no longer appears as current.
Use `nmemory_capsule_show` to inspect its retained provenance.

## Operation and failure diagnosis

Use `nmemory digest show --project-prefix example` for a summary.
Use `nmemory capsule show --id REF` for a known record.
Check project fences, terms, validity, and supersession when recall omits a record.
Use `nmemory embedding show --json` when the vector lane is unavailable.
A model route refusal needs the named route corrected before capture can proceed.
A successful System1 response does not establish native account authentication.
Read [troubleshooting](TROUBLESHOOTING.md) before reporting a failure.

## Backup and restoration

Create a new private destination:

```bash
nmemory database snapshot /absolute/private-backup/nmemory.sqlite3
```

Keep the configuration and exact runtime version with the snapshot.
Stop all writers before restoration.
Restore the snapshot into a disposable account with that runtime first.
Read known records and repeat the first-result recall.
Replace the active store only after this check passes.
Preserve the previous store until the restored account passes the same check.
Do not reuse v0.3.0 data without the [compatibility procedure](UPGRADE.md).
The [rulebook](../RULEBOOK.md) defines storage and recovery constraints.
