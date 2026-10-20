# Privacy and processing choices

Read this guide before the first capture.
Local storage and local processing are separate choices.
A capsule can contain source text, paths, project names, and personal information.
Capture only the data that the project needs. Never capture secrets.

| Boundary | Stored or transmitted data | Choice |
| --- | --- | --- |
| Memory SQLite store | Capsule text, provenance, relations, outcomes, and audit data | Account-owned storage |
| Lexical recall | Query terms and matching local records | No embedding request |
| Embedding service | Capsule text and recall queries | Local compatible service or external provider |
| System1 | Text needed for classification | Inspect the configured route before capture |
| Curator | Selected records and supporting context | Inspect the inference route before activation |
| nSPAWN registry and run files | Account aliases, lifecycle, bounded redacted output | Private local storage, native accounts remain separate |
| Browser vault | Encrypted passwords and a separate local key | Back up both files together |
| Browser requests | URLs, headers, page data, and authorized form data | Explicit origin and network boundaries |
| Browser captures | Page pixels under the selected retention mode | Host grant required for managed retention |
| Native agent client | Prompts, tool results, and recalled context | Client authentication and provider policy apply |
| Web search | Search terms | Every configured keyed provider can receive the query |
| GitHub sync | Selected graph records | Explicit repository and effort fence |

Gemini and OpenAI embeddings use external services when their credentials enable them.
An OpenAI-compatible endpoint can use a local embedding server.
Its endpoint, model, and native dimension must match the selected model.
A compatible protocol alone does not prove that the endpoint is local.
Without a usable embedding route, recall can use its lexical lane.
Check `nmemory embedding show --json` for the actual state.

System1 classification and curator inference are independent of embedding selection.
A local embedding endpoint does not make those routes local.
The public setup must require an explicit external-processing choice before activation.
Native clients still need authentication, even when System1 is local.
Provider charges and terms apply to external processing.

## Files and retention

The database opener (`nott_database.connect`) selects the effective account's passwd home.
Its default data directory is `.nott/state/database` under that home.
Changing `HOME` alone does not isolate a store.
Use a disposable account or the test harness's explicit process-local store override for recovery tests.
The directory must be owner-only. Store files must be owner-readable and owner-writable only.


User configuration belongs under the existing `~/.nott/` root.
The memory loader reads `~/.nott/memory.toml` unless an explicit configuration path replaces it.
Native clients keep credentials in their own account homes.
Do not place keys in commands, source files, examples, reports, or screenshots.

The database contains more than the current recall result.
Supersession excludes a prior decision from default recall. It does not erase its history.
Purge and redaction do not erase an existing backup or a provider's retained copy.
Keep snapshots and rollback receipts private. Protect them as you protect the store.
Use the tool schema to inspect purge modes before a destructive operation.

## Share a report safely

Use synthetic records in a disposable home.
Omit tokens, account identifiers, private paths, capsule bodies, and query text from logs.
A benchmark needs public synthetic inputs or a dataset with compatible sharing terms.
Private session records are not public benchmark evidence.
