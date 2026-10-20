# nMEMORY evaluation methods

Measure a specific retrieval behavior with exact inputs and an inspectable transcript.
This harness includes synthetic recall, LongMemEval, extraction, and comparison runners.
The [command catalog](../docs/REFERENCE.md#evaluation-cli) lists the current interface.

## Evidence status

This guide publishes methods, not a measured numerical ranking.
This guide includes no original benchmark receipt bundle.
Historical result files do not establish a score for the current Python engine.
In particular, earlier LongMemEval notes can identify the retired implementation as their binary.
Do not attribute those numbers to the current release.

Private session evaluations are **non-public evidence**.
Their source transcripts, store snapshots, and receipts can contain personal data.
A private result cannot support a publicly replayable performance claim.
Keep those records private rather than publishing redacted scores without reproducible inputs.
The source catalogs include private-audit commands. Their presence does not make private datasets public.

## Public synthetic replay

The n1 world uses a deterministic generator, fixed input sizes, and programmatic answers.
Its world starts at `2026-01-01T00:00:00Z`.
Use a clean checkout of the source revision under test and its root lockfile.
Run from the workspace root in a shell with no provider credentials or private configuration:

## Safety and Governance Fences

- **Disposable store**: The benchmark store is `nmemory.sqlite3` under `/var/tmp`, outside the account's live store. On Linux, children inherit an open directory descriptor through `NOTT_DATABASE_DIR`. The shared database library keeps SQLite writes and sidecars bound to that descriptor. Other systems refuse these store operations. Children keep the parent's disposable home, temporary paths, caches, and test markers.
- **Output Confinement**: Benchmark output paths must stay under `/var/tmp/` before and after path resolution. Directory descriptors bind report writes to the selected directory. The writer refuses symlinked parents and files. It replaces regular report files without changing other hard links.
- **Shadow Fixtures**: Fixture directories and shadow stores require unused paths under `/var/tmp/`. Fixture directories are private. Open directory descriptors bind fixture copies, deletions, and replacements to the reserved tree. Source anchors must resolve inside the source tree. The copy reads each admitted source through a directory descriptor without following new symlinks. Existing database files, journals, and lock sidecars cause refusal.

The internal `open_shadow` helper is a context manager. Its context keeps the store directory open through creation, queries, writes, and cleanup.
- **Read-Only SUT**: The SHA-256 digest of the SUT binary is captured at startup and re-verified at shutdown. Any binary mutation during execution aborts with `BinaryChanged`.
- **XDG Isolation**: Per-run temporary state directories are configured via `XDG_STATE_HOME`.

## CLI Usage

The command table in `nmemory_evals/cli.py` is the grammar; `nmemory-evals --help`
is generated from it, and a spelling outside it is refused as unknown.

### Run n1 Benchmark
```bash
uv sync --group dev
run_root=$(mktemp -d /var/tmp/nmemory-public-n1.XXXXXX)
mkdir "$run_root/home" "$run_root/store"
sut="$PWD/.venv/bin/nmemory"
HOME="$run_root/home" NOTT_DATABASE_DIR="$run_root/store" \
  NOTT_NMEMORY_CURATOR=0 "$sut" database create
HOME="$run_root/home" NOTT_DATABASE_DIR="$run_root/store" \
  NOTT_NMEMORY_CURATOR=0 TMPDIR=/var/tmp \
  .venv/bin/nmemory-evals scenario run --binary "$sut" \
  --seed 42 --services 8 --engineers 5 --events 120 --questions 40 -k 3 \
  --out "$run_root/result"
.venv/bin/nmemory-evals artifact verify "$run_root/result"
.venv/bin/nmemory-evals scenario show --seed 42 --services 8 \
  --engineers 5 --events 120 --seq 17
```

The explicit store variable belongs only to this disposable benchmark process.
Never set it persistently or point it at an account's live store.
Keep `run_root` until the results and receipts pass inspection.
This replay specifies the lexical baseline. Do not silently add vector or cloud processing.
For a vector result, also record the provider, endpoint class, model, dimension, and readiness.

## Metric definitions

The implementation is [score.py](nmemory_evals/score.py).
All rates are fractions. An empty denominator produces zero.

| Metric | Numerator | Denominator |
| --- | --- | --- |
| `grounded_hit_at_k.rate` | Answerable questions with a grounded result and a golden ID in the first `k` results | Answerable questions |
| `true_abstain.rate` | Out-of-world questions with outcome `abstain` | Out-of-world questions |
| `false_abstain.rate` | Answerable questions whose outcome is not grounded | Answerable questions |

The n1 headline is `true_abstain.rate`.
The arena abstention helper has a separate definition that can accept an empty result.
Do not combine those definitions into one reported metric.

The six n1 question families cover deploy actors, causal deploys, resolvers,
current owners, paraphrased owners, and out-of-world entities.
The replay requests 40 questions with `k=3`.
Read the generated question file for the actual allocation and golden IDs.
A synthetic world does not measure all conversational memory behavior.

## Required receipt bundle

| File | What it establishes |
| --- | --- |
| `config.json` | Seed, sample sizes, `k`, harness version, executable path, and executable digest |
| `world.json` | Exact generated world events |
| `qa.json` | Questions and golden record IDs |
| `transcript.jsonl` | Recorded protocol requests and results |
| `scores.json` | Metric counts, denominators, and rates |

The producer is [artifacts.py](nmemory_evals/artifacts.py).
The n1 writer does not emit a checksum manifest for every file.
Without `bundle.json`, `artifact verify` checks canonical structure rather than all file digests.
Record hashes of the complete bundle and source revision before publishing a number.
An executable digest alone does not identify all imported source and dependency bytes.
A receipt must name the source commit, root lockfile, Python version, platform,
processing configuration, and command in addition to these generated files.

## LongMemEval and comparisons

LongMemEval needs the exact dataset and the configuration's pinned digest.
The repository does not grant redistribution rights to that external dataset.
Its loader checks timestamps, evidence links, and the haystack arrays.
Dataset seeding uses the capture function, rather than an English-only MCP capture of every original session.
State this boundary when you report a result.

A public comparison needs the same corpus, sample, budget, reader, judge, and processing policy for each system.
Name each system's version and account prerequisites.
Report errors, refusals, unavailable lanes, and collapsed equivalent budgets.
Do not present an unconfigured provider as a measured loss.
Use the `arena` and `synthesis` commands only with their exact preregistration inputs.
This guide alone supports no comparative score.

## Focused verification

From the workspace root:

```bash
TMPDIR=/var/tmp uv run --group dev pytest evals/nmemory/tests/test_metrics.py -q
uv run ruff check evals/nmemory
uv run ruff format --check evals/nmemory
```

Use the affected test path for a different change.
Read [privacy](../docs/PRIVACY.md) before sharing artifacts.
