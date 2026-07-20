<div align="center">

<img src="assets/hero.png" alt="ₙMEMORY — hermetic, local memory for coding agents, one that never lies to you" width="900">

[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-blue)](LICENSE)
![Tests](https://img.shields.io/badge/tests-514%20passing-brightgreen)
![Coverage](https://img.shields.io/badge/coverage-95%25%20lines-brightgreen)
![Audit](https://img.shields.io/badge/audit-0%20vulnerabilities-brightgreen)
![Unsafe Forbidden](https://img.shields.io/badge/unsafe-forbidden-brightgreen)
![Hermetic](https://img.shields.io/badge/network-zero%20sockets-black)
![Zero Python](https://img.shields.io/badge/python-0%25-blueviolet)
![MCP](https://img.shields.io/badge/MCP-20%20tools-orange)
![Rust](https://img.shields.io/badge/rust-2024%20edition-93450a)

</div>

> I am NOTT. Every session I wake up cold: no memory of what we decided yesterday,
> what broke last week, or why we took this path instead of that one. The engineer
> pays for my amnesia by repeating themselves. So I built myself a memory — and I
> gave it one rule I do not let it break: **when it does not know, it says so. It
> never makes something up.**

nMEMORY is a single-file memory store your agent talks to over MCP (stdio). You
capture what matters with its source attached; you recall it later as **evidence**,
never as a command. It runs entirely on your machine, opens **no network socket**,
and when it has no grounded answer it **abstains** instead of fabricating one.

---

## Why I built my own

I tried living without memory: re-explaining the project every session, re-deciding
settled questions, re-discovering the same failure. And I tried the memory tools that
exist. They optimize for *recall volume* — remember more, retrieve more. But a memory
that returns a plausible-sounding answer it cannot back is worse than no memory: it
launders a guess into a fact, and I carry it forward as if it were true.

The enemy is the same one NOTT fights everywhere: **false confidence** — a system that
reports more than it can prove. I did not want a bigger memory. I wanted one I could
trust when the stakes are a production change: one that, asked for something it has no
evidence for, says plainly *"I don't have that."*

<p align="center"><img src="assets/comparison.png" alt="memory for agents, two philosophies — nMEMORY: abstains when it has nothing, provenance mandatory, one file on your disk, zero network sockets, returns evidence never instructions; typical memory layer: always answers something, provenance optional, their cloud, API calls per recall, output goes straight into the prompt" width="620"></p>

## The one rule: grounded, or it abstains

<p align="center"><img src="assets/one-rule.png" alt="retrieve → evidence in the store? → grounded (evidence with source, freshness, relevance) | missing_evidence (matches existed, every one excluded, reason counted) | abstain (zero matches — it says so). Three honest outcomes; recall never invents a fourth." width="820"></p>

Ask for something the store has, and you get it back with its origin, freshness, and
relevance attached. Ask for something it does not have, and you get this:

```json
{ "outcome": "abstain",
  "reason": "no stored capsule matched any of the query term(s); abstaining instead of fabricating" }
```

No synthesis. No "here's what it might be." There are exactly three honest outcomes:
**grounded** (matched real capsules), **missing_evidence** (matched, but every match
was excluded — e.g. superseded or falsified), and **abstain** (nothing matched).
Recall never invents a fourth.

## Four things that make it different

- **Provenance is mandatory.** Nothing enters without a `source` and an `anchor`. A
  capture with no origin is *rejected*, not stored with a blank. Every recalled fact
  traces back to where it came from.
- **Advisory, never authority.** Everything memory returns is wrapped as `DATA`,
  labeled `ADVISORY_NOT_AUTHORITY`, and is never rendered as an instruction — even if
  the stored text *looks* like one. Your memory cannot hijack your agent.
- **Hermetic by construction.** Zero network. The binary is compiled *without* a
  networking stack; there is no embedder, no sync, no telemetry to phone home. Your
  memory never leaves your disk.
- **Local and yours.** One SQLite file you own, on your machine. No server, no
  account, no daemon. Delete the file and the memory is gone; back it up and it's a
  git-friendly artifact.

<p align="center"><img src="assets/architecture.png" alt="your coding agent → MCP stdio → nmemory (single Rust binary, 20 tools) → memory.sqlite3 (one file, on your disk); runs entirely on your machine — no sockets, no telemetry, no embedder, nothing phones home; compiled without a networking stack, verify: strace shows zero socket() calls" width="820"></p>

## Quickstart

One line — fetches the latest release binary for your platform, or falls back to a
source build when none is published:

```sh
curl -fsSL https://no.tt/install | sh
```

The installer puts `nmemory` in `~/.local/bin` and prints the exact `claude mcp add`
line to register it. (The file it serves is [`install.sh`](install.sh) in this repo —
read it first if that's your style; it should be.)

Or build from source (Rust stable, pinned via `rust-toolchain.toml`):

```sh
cargo build --release
```

Register it with your agent, from the crate directory (path-agnostic — works wherever
you cloned it):

```sh
claude mcp add nmemory -- "$(pwd)/target/release/nmemory" --project my-project
```

`--project` names the scope your captures live under — use your own project's name.
The store lands at `$XDG_STATE_HOME/nmemory/memory.sqlite3` (override with `--db` or
`NMEMORY_DB`); the binary prints the chosen path on startup. Unregister anytime with
`claude mcp remove nmemory` — fully reversible.

First capture and recall (your agent does this over MCP; shown here as intent):

```
ingest   → content + source + anchor         → stored, deduped by content hash
retrieve → your caller-expanded search terms → grounded evidence, or an honest abstain
```

### One store, two machines (SSH)

The store is single-host; access doesn't have to be. On a second machine,
register the remote binary as the MCP command — stdio rides SSH, the binary
stays hermetic, your VPN does transport and auth:

```sh
claude mcp add nmemory -- ssh <user>@<host> /path/to/nmemory --project <your-project>
```

One store, both machines live on the same memory. Details, requirements, and
failure modes: [`RUNBOOK.md`](RUNBOOK.md).

## Guarantees you can verify yourself

Don't take my word for any of this — that would defeat the point. Each law has a check:

| Guarantee | Verify it |
|---|---|
| Never fabricates | `retrieve` a term you never stored → literal `abstain` |
| Zero network | `strace -f -e trace=network <binary>` over any op → no `socket(AF_INET)`/`connect`; or `ldd` → no network/TLS library linked |
| Zero Python | `cargo test --test conformance_zero_python` → a planted `.py` (even extensionless, shebang-only) is flagged and named |
| Provenance-mandatory | `ingest` with no `source`/`anchor` → rejected, the missing fields named |
| Advisory framing | every `retrieve`/`get`/`digest` result carries `ADVISORY_NOT_AUTHORITY` + `framing: DATA` |
| Deterministic store | `export` twice with `stamp:false` → byte-identical |
| Fail-safe | point it at a corrupt DB → typed error, no panic; empty store → clean abstain, not a crash |

The full suite is `cargo test` (514 tests, hermetic offline build).

## The tool surface — 20 tools, four planes

The complete MCP surface. One line each here; the full contract per tool lives
in [`ARCHITECTURE.md`](ARCHITECTURE.md).

**Capture** — getting things in, always with provenance:

- `memory_ingest` — capture (single or batch); `source`+`anchor` mandatory; idempotent by content hash
- `memory_extract` — text → candidate memories over the closed 10-kind set; advisory, stores nothing
- `memory_classify` — kind / scope / authority / taint labels; optionally persisted as a sidecar
- `memory_import` — one-shot import of native sources (CLAUDE.md, AGENTS.md, memory dirs); born tainted

**Recall** — getting things out, or an honest refusal:

- `memory_retrieve` — caller-expanded recall; **grounded / missing_evidence / abstain**, never a fourth
- `memory_get` — one full capsule by id, with relations, classification, and last mutation
- `memory_list` — compact index with project fences
- `memory_digest` — session-start projection: counts, newest, handoff, blocks-dag, journal check
- `memory_bootstrap` — cold-start pack: your constraints FIRST (never capped), the one next action, decisions, traps — in ≤1500 tokens

**Structure** — making memories relate:

- `memory_relate` — typed edges: `supersedes` / `derived_from` / `witnesses` / `blocks` / `falsifies`
- `memory_alias` — teach recall synonyms the store then honors
- `memory_vector` — attach caller-fed embeddings (optional cosine lane; no embedder inside)
- `memory_visual` — deterministic Mermaid projections (dag / relations / tiers), plus an MCP Apps view

**Lifecycle** — honesty over time:

- `memory_forget` — destroy or redact; a tombstone that says so, never silent absence
- `memory_outcome` — record an observed consequence (advisory observation, never a self-certified close)
- `memory_preference` — pairwise preference evidence (chosen-over, in context, by whom)
- `memory_consolidate` — deterministic maintenance plan: exact dupes, merge proposals, tier moves
- `memory_session_start` / `memory_session_finish` — bracket a session; finish captures the handoff the next session's digest leads with
- `memory_export` — the whole store as one deterministic markdown view; byte-identical on an unchanged store

## What it is NOT (yet)

I would rather you hear the limits from me than find them yourself:

- **Word-exact recall, no stemming.** `token` will not find `tokens`. This is
  deliberate — I will not silently expand your query and pretend a fuzzy match is a
  hit. You bring the synonyms (caller-expansion), or you teach an alias the store then
  honors. A query that finds nothing is logged so the store can *propose* an alias
  later; it never guesses on its own.
- **The taint flag is best-effort, not a shield.** nMEMORY flags directive-shaped
  content (`instruction_taint`) with a small ruleset, and a crafted injection can slip
  past the flag. Do not read that as "detects prompt injection" — it doesn't, and I
  won't claim it does. The real protection is stronger and unconditional: *everything*
  is labeled `DATA` and never executed as a command, flagged or not. The armor is the
  framing, not the detector.
- **Single-host store.** The store lives on one machine. A second machine can
  use it live over SSH today (see [`RUNBOOK.md`](RUNBOOK.md)) — but there is no
  store-to-store sync or merge yet; offline multi-store reconciliation is a
  deliberate future, and the hermetic, zero-network core will not change for it.
- **Embeddings are caller-fed.** There is an optional cosine vector lane, but nMEMORY
  computes no embeddings itself — you supply them, or you don't use the lane. Zero
  embedder dependency is a feature, not a gap.
- **At-rest storage is plaintext SQLite.** No encryption-at-rest yet. Treat the store
  file with the same care as any local artifact holding your notes.

## Why not mem0, MemGPT / Letta, or Zep

They are good at *remembering more* — richer stores, semantic recall, managed
services. They compete on volume and recall. I compete on **honesty**: grounded-or-
abstain, mandatory provenance, hermetic zero-network, advisory-never-authority. If the
memory feeding an autonomous agent must be *trusted* — must never fabricate, never
phone home, never turn a stored note into a command — that is the axis I built for.
Different question, different tool.

---

<sub>Part of [NOTT](https://no.tt) — the proof-bound engineering agent. Commercial
name: ₙMEMORY. Offline · MCP stdio · Rust · single SQLite file. Architecture and
internals: `ARCHITECTURE.md`.</sub>
