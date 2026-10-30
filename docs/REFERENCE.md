# nMEMORY reference

The source declarations own argument types, bounds, defaults, and refusal rules.
Use the installed executable's `--help` and MCP `tools/list` for its effective version.
A declared operation can refuse a missing prerequisite or denied grant.

## CLI commands

Source: [cli.py](../nmemory/cli.py), `COMMANDS`.
All commands use the executable from the public repository's root workspace.

| Command | Arguments |
| --- | --- |
| `nmemory release export` | `--source-root ROOT --output DIR [--component nmemory\|nspawn\|nbrowser\|napple]` |
| `nmemory project bootstrap` | `--root DIR --project P [--client claude\|codex] [--system1 lexical\|native\|local] [--endpoint URL --model M] [--hooks-dir DIR] [--restore]` |
| `nmemory project scan` | `--root DIR --project P` |
| `nmemory curator set` | `--provider P [--account A] \| --reset` |
| `nmemory mcp serve` | `[--project P] [--read-only \| --viewer] [--http [--listen H:P] [--key-file F]]` |
| `nmemory database create` | `None` |
| `nmemory database snapshot` | `DEST` |
| `nmemory capsule retrieve` | `[QUERY] [--terms T] [--limit N] [--budget N] [--project-prefix P] [--known-at T]` |
| `nmemory capsule show` | `--id REF` |
| `nmemory capsule list` | `--pinned [--json \| --full]` |
| `nmemory capsule export` | `[--no-stamp]` |
| `nmemory capsule relate` | `--kind K --from REF --to REF [--revoke --actor A --reason R]` |
| `nmemory capsule audit` | `[--json] [--apply]` |
| `nmemory proposal export` | `--since DATE --until DATE` |
| `nmemory digest show` | `[--project-prefix P]` |
| `nmemory rule write` | `None` |
| `nmemory context search` | `[QUERY] [--source S] [--limit N] [--budget N]` |
| `nmemory index build` | `[--root DIR] [--source S]` |
| `nmemory portal export` | `--project P [--digest]` |
| `nmemory anchor list` | `None` |
| `nmemory anchor check` | `[--drain \| --all] [--root DIR] [--spool DIR] [--json]` |
| `nmemory quality measure` | `[--json] [--dry-run]` |
| `nmemory review run` | `--daily` |
| `nmemory embedding show` | `[--json]` |

## MCP tools

Source: [tools_schema.json](../nmemory/tools_schema.json).

| Tool | Operation |
| --- | --- |
| `nmemory_board_show` | Show a project's live tasks as a board of five columns. |
| `nmemory_capsule_ingest` | Capture checked records. |
| `nmemory_capsule_list` | List filtered records with optional paging. |
| `nmemory_capsule_pin` | Pin or unpin a record with a reason. |
| `nmemory_capsule_purge` | Redact or purge under the selected mode. |
| `nmemory_capsule_retrieve` | Recall eligible records with a receipt. |
| `nmemory_capsule_show` | Read one record and its provenance. |
| `nmemory_context_show` | Read context from selected sources. |
| `nmemory_digest_show` | Read the store summary. |
| `nmemory_dod_set` | Set a result contract lifecycle. |
| `nmemory_kind_classify` | Classify kind, scope, and taint. |
| `nmemory_outcome_list` | List observed outcomes. |
| `nmemory_outcome_record` | Record an observed outcome. |
| `nmemory_proposal_review` | Review staged proposals. |
| `nmemory_relation_add` | Add or revoke a relation. |
| `nmemory_session_finish` | Close a session with its handoff. |
| `nmemory_session_start` | Start a session. |
| `nmemory_work_list` | Page through canonical work records. |

## Implementation and storage

[server.py](../nmemory/server.py) owns MCP dispatch and client identity checks.
[config.py](../nmemory/config.py) owns configuration validation.
[store.py](../nmemory/store.py) composes the synchronous repositories.
The [rulebook](../RULEBOOK.md) defines transaction, recall, and vector readiness invariants.
Viewer and read-only modes restrict the full tool catalog.
Schema discovery does not create a store. The first operation opens it lazily.

## Evaluation CLI

The optional evaluation workspace has a separate executable.
Source: [cli.py](../evals/nmemory_evals/cli.py), `COMMANDS`.
The [evaluation guide](../evals/README.md) defines public synthetic replay and its evidence limits.
Private audit commands do not authorize publication of their inputs.

| Command | Arguments |
| --- | --- |
| `nmemory-evals scenario run` | `[--binary B] [--seed N] [--services N] [--engineers N] [--events N] [--questions N] [-k N] [--out DIR]` |
| `nmemory-evals scenario show` | `--seq N [--seed N] [--services N] [--engineers N] [--events N]` |
| `nmemory-evals session show` | `--data F --sha256 H --question Q --position N --session S` |
| `nmemory-evals suite run` | `--config F --data F [--binary B] [--out DIR]` |
| `nmemory-evals capsule score` | `[--binary B] [--lane term\|vector\|fused] [-k N] [--out DIR]` |
| `nmemory-evals synthesis judge` | `--preregistration F --results F [--expected-sha256 H]` |
| `nmemory-evals proposal score` | `[--dataset F] [--proposer deterministic\|llm] [--timeout-s N] [--json] [--out DIR]` |
| `nmemory-evals transcript audit` | `[--dataset F] [--session ID] [--json] [--out DIR]` |
| `nmemory-evals review measure` | `--shadow-url U --project P --dest-root DIR [--source-root DIR] [--per-cell N] [--budget N]` |
| `nmemory-evals artifact verify` | `<BUNDLE>` |
| `nmemory-evals artifact diff` | `--left DIR --right DIR` |
