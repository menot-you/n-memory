# CLAUDE.md — nMEMORY

Repository contract and contributor guide for `nMEMORY`, the local-first grounded memory server and CLI for AI coding agents.
Read this file before changing code, schemas, migrations, or documentation.

## What this repository builds

`nMEMORY` stores project decisions, facts, constraints, plans, tasks, and Definitions of Done (`DoD`) in an account-owned SQLite store with mandatory provenance and tamper-evident audit trails.

| Part | Path | Responsibility |
| --- | --- | --- |
| Core package | [server.py](nmemory/server.py), [store.py](nmemory/store.py), [cli.py](nmemory/cli.py) | MCP stdio and HTTP doors, synchronous SQLite transactions, and the `nmemory <noun> <verb>` CLI |
| Retrieval and embeddings | [embedding.py](nmemory/queries/embedding.py), [retrieve.py](nmemory/tools/retrieve.py) | Hybrid FTS5 BM25 lexical search, taught synonyms, `sqlite-vec` cosine vector search, and RRF fusion |
| System1 and curator | [system1.py](nmemory/system1.py), [curator.py](nmemory/commands/curator.py) | Typed probabilistic System1 questions (`Laya` / `Jev` or local SLM), order-ensemble staging, and anchor curation |
| Shared libraries | [database](packages/database/nott_database/__init__.py), [system1](packages/system1/nott_system1/__init__.py), [netdoor](packages/netdoor/nott_netdoor/__init__.py), [socket](packages/socket/nott_socket/__init__.py) | Single SQLite opener, System1 HTTP transport, authenticated loopback HTTP door, and peer-credential Unix sockets |
| Companion runtime | [nspawn](packages/nspawn/nspawn/supervisor.py), [evals](evals/nmemory_evals/__init__.py) | Supervised native agent execution for deep curator passes and deterministic recall evaluation harnesses |

## Local checks and commands

Run these commands from the repository root with the pinned Python version and `uv.lock`:

| Need | Command |
| --- | --- |
| Sync workspace dependencies | `uv sync --group dev` |
| Lint and format check | `uv run ruff check .` and `uv run ruff format --check .` |
| Strict type check | `uv run mypy --strict nmemory` |
| Unit and contract suite | `uv run --group dev pytest tests -q` |
| Single test file | `uv run --group dev pytest tests/test_config.py -q` |
| Public document and link check | `uv run --group dev pytest tests/test_public_package_layout.py -q` |
| Lockfile consistency | `uv lock --check` |

Every test provisions its own disposable SQLite store directory through [conftest.py](tests/conftest.py).
Never point a test or check at a live user store under `~/.nott/state/database`.

## Architecture and code boundaries

1. **Synchronous domain core:** keep storage, ranking, and graph logic synchronous inside `nmemory/`. Keep concurrency and I/O at the MCP, HTTP, and CLI doors.
2. **One SQLite opener:** open every database through `nott_database.connect` ([database](packages/database/nott_database/__init__.py)). Never import raw `sqlite3` in application modules or add a second persistence store.
3. **Mandatory provenance and audit chain:** every ingested capsule requires a `kind`, `source`, and `anchor`. Every canonical mutation appends a record to the SHA-256 hash-chained audit journal.
4. **Taint inheritance on extraction:** when an agent or extractor derives a capsule from conversation turns, pass the raw turn bytes in `derived_from`. Scan those source bytes so paraphrased prompt injections keep `instruction_taint=true` and remain fenced as `ADVISORY_NOT_AUTHORITY` data.
5. **Three honest retrieval outcomes:** `nmemory_capsule_retrieve` returns `grounded`, `missing_evidence` (matched in store but excluded by project, time, standing, or cosine margin fences), or `abstain` (zero matches). Never fabricate a fallback answer.
6. **System1 probabilistic contract:** [system1.py](nmemory/system1.py) uses `nott_system1` (`noul`, `choice`, and `score` questions) over `/v1/systemone` (`laya-nott`) or a local loopback `/v1/chat/completions` endpoint with strict JSON Schema. Missing or invalid model answers raise `System1Unavailable` (operational exit `1`) and never become a negative verdict.
7. **Two-axis Definition of Done (`DoD`):** keep the authority axis (`status: proposed -> accepted -> waived`) separate from the observation axis (`verdict: unmet -> met | partial | failed`). Closing a DoD requires `evidence_state="observed"` and a reproducible `proof_hint`.
8. **Typed failures and resource cleanup:** raise typed domain exceptions derived from `NMemoryError` for failures and use `T | None` for absence. Close all connections, cursors, and sockets through `with` or `finally`.
9. **Grammar canon:** CLI commands and MCP tools follow singular `<noun> <verb>` order (`nmemory capsule ingest`, `nmemory_capsule_ingest`) with no aliases.

## Working rules for agents and contributors

- Write all code comments, docstrings, commit messages, pull requests, and documentation in Simplified Technical English (ASD-STE100). Keep sentences under 25 words and use active voice.
- Write the regression test first for any behavior fix. Run the test and observe the intended failure (`RED`) before editing product code.
- Never place API keys, tokens, or personal paths in code, TOML files, tests, or documentation. Read keys only from the process environment (`NOTT_SYSTEM1_API_KEY`).
- Preserve existing schema migrations and hash-chained audit compatibility. Read the [rulebook](RULEBOOK.md) before touching storage or recall invariants.

## Canonical documentation map

| Need | Canonical document |
| --- | --- |
| Product overview and quickstart | [README](README.md) |
| Storage, provenance, and recall invariants | [Rulebook](RULEBOOK.md) |
| Documentation index and first contribution | [Docs index](docs/README.md) |
| Service and trust boundaries | [Architecture](docs/ARCHITECTURE.md) |
| Setup, operation, backup, and restoration | [Runbook](docs/RUNBOOK.md) |
| Complete MCP tool and CLI command reference | [Reference](docs/REFERENCE.md) |
| Developer toolkit and Context7/RTK setup | [Setup](docs/SETUP.md) |
| Quality gates and security review | [Quality](docs/QUALITY.md) |
| Source installation and prerequisites | [Installation](docs/INSTALLATION.md) |
| Data boundaries and external processing choices | [Privacy](docs/PRIVACY.md) |
| Schema compatibility and upgrades | [Upgrade](docs/UPGRADE.md) |
| Failure diagnosis | [Troubleshooting](docs/TROUBLESHOOTING.md) |
| Working agreements and pull requests | [Contributing](CONTRIBUTING.md) and [Security](SECURITY.md) |
