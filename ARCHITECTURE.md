# nMEMORY — feature map + engine architecture (LLM-first)

```
status:    engine + feature map for the shipped crate (v0.1.0)
axiom:     nMEMORY is FOR LLMs. The primary consumer (~90%) is an agent inside a
           session, not a human. Every design choice optimizes for that caller:
           token economy, structured shapes, caller-side intelligence, injection
           safety. The human is the owner and auditor, not the hot-path reader.
```

## Shipped surface (current — 21 MCP tools)

The feature map in §1 below is the original 2026-07-18 design plan (CORE / NEXT /
DEFERRED). Most of what it filed under NEXT and DEFERRED has since shipped. The
**current live surface is 21 MCP tools**, all tested:

- **capture** — `memory_ingest` (single/batch, provenance-mandatory) · `memory_import`
  (CLAUDE.md / AGENTS.md / memory-dir, taint-fenced)
- **recall** — `memory_retrieve` (FTS5+bm25, grounded-or-abstain, optional caller-fed
  cosine vector lane) · `memory_get` · `memory_list` · `memory_digest` (session-start
  projection) · `memory_bootstrap` (cold-start pack)
- **organize** — `memory_classify` · `memory_extract` · `memory_relate`
  (supersedes / derived_from / witnesses / blocks / falsifies) · `memory_alias` ·
  `memory_consolidate`
- **lifecycle** — `memory_forget` (tombstone / redact / purge) · `memory_outcome` ·
  `memory_preference` · `memory_session_start` · `memory_session_finish`
- **views** — `memory_export` (markdown generated view) · `memory_visual`
  (dag / relations / tiers)
- **vectors** — `memory_vector` (attach caller-fed embeddings; zero embedder dependency)
- **sync** — `memory_merge` (MCP: reconcile a second store file into this one) ·
  `nmemory sync` (CLI subcommand, not an MCP tool: fetch a remote mirror, merge it
  in, optionally `--push` the merged store back — §4). Both ride one core:
  content-hash identity, id-remap, forget-wins, deterministic. Sync is explicit,
  owner-invoked, opt-in — NEVER a background daemon; the serve path stays
  zero-network.
- **one-shot verbs** — `nmemory recall` · `nmemory digest` (CLI verbs, not MCP
  tools): one argv→stdout call through the SAME `memory_retrieve` /
  `memory_digest` handlers — identical envelope bytes and side effects, no MCP
  handshake — the transport the NOTT plugin's session-start and per-prompt
  recall hooks ride (0.15s wall per prompt).
- **app views** — two MCP App resources (`text/html;profile=mcp-app`):
  `ui://nmemory/document` (readable document over `memory_export`) ·
  `ui://nmemory/visual` (Mermaid over `memory_visual`) — §5. Resources, not tools:
  the 21-tool surface above is unchanged by them.

The four laws in §0 and the engine architecture in §2 remain current. Read the §1
tiers for the design *why*, not the present tool count.

## 0. What "LLM-first" changes (the four laws of this design)

1. **Token economy is THE scarce resource.** Every recalled byte competes with working
   context. Recall returns few, dense, layered results — headline first, expand on demand —
   under an explicit token budget. Never documents when a claim suffices.
2. **The caller is intelligent.** An LLM expands its own queries (synonyms, aliases,
   rephrasing) and judges relevance. The engine does honest lexical work (FTS5+bm25) and
   returns explain data; it never needs an embedder to be useful. This keeps the engine
   hermetic BY DESIGN, not as a compromise.
3. **Recalled content is quasi-instruction — so it is armored.** Whatever memory returns
   lands in a prompt. Every recall result is wrapped as DATA (evidence envelope), carries
   `ADVISORY_NOT_AUTHORITY` + its `instruction_taint` flag, and the surface never renders
   stored content as directives. Anti-poisoning is core, not deferred polish.
4. **Sessions are the natural lifecycle.** The agent is born cold every session. The two
   hot moments are session-start (context assembly) and during/end (capture). The engine
   serves both with one cheap projection (`digest`) and low-friction capture (defaults +
   batch). Session bracketing as records stays deferred; the lifecycle is served by
   convention + hooks.

## 1. Feature map

### CORE (walking skeleton — usable memory)
| feature | what it is | LLM-first note |
|---|---|---|
| Capsule v1 | frozen record: content · provenance{source,anchor,source_hash} MANDATORY · confidence · freshness{valid_from,valid_to?} · scope{project_id} · authority_class · instruction_taint | fixed shape = cheap parsing; no capsule without origin |
| durable spool | crash-safe staged writes (copied organ, ssot-spool) | capture never loses a byte on crash |
| SQLite store | single-file, append-ordered seq ids, no clock/random inside, canonical snapshot | deterministic replay; hermetic |
| ingest | provenance-mandatory, source_hash idempotent, **smart defaults** (scope from caller context, authority_class=agent-inferred, timestamps injected), **batch array** | 2 required fields in practice; N capsules per call |
| dedup-hint (dialogue consolidation) | ingest response returns "similar existing: cap-N (score)" — the CALLER decides supersede/skip/keep | uses caller intelligence instead of auto-merge heuristics |
| retrieve | **FTS5+bm25** lexical match over multi-term caller-expanded queries + alias-taught OR-group expansion; deterministic sort key (term coverage, bm25, decayed weight = confidence × 2^(-age_days/90), valid_from, usage late key, id); **token-budgeted layered results** (headline → full capsule on demand); grounded-or-ABSTAIN | the engine is honest, the caller is smart |
| evidence envelope | every result wrapped as DATA: `ADVISORY_NOT_AUTHORITY` + taint flag + provenance + freshness + matched-terms explain | injection armor + trust calibration in one shape |
| digest | one-call compact store projection (~counts, scopes, newest, most-recalled, N headlines) sized for session-start injection | the MEMORY.md-analogue; the session-start hot path |
| get / list | by id; by scope/kind/recency | plumbing |
| usage counters | recall_count + last_recalled_at per capsule (store sidecar, NOT a Capsule field) | ranking signal and lifecycle-staleness input (consolidation's archive-age arm) only; NEVER confidence/authority (law: usage is not success evidence) |
| supersedes | explicit replace chain (sidecar relation); superseded excluded from recall by default | replace-over-append discipline, callable by the caller after a dedup-hint |
| gold-bar + zero-Python + conformance | CI gates + ported donor tests (fixtures re-authored, no `.py`) | the code bar is law |

### SHIPPED SINCE THE FIRST DESIGN (all landed in v0.1.0 — recorded here so a reader never mistakes them for gaps)
anchor-liveness flag on recall (does path:line still exist? cheap stat/grep) · session records
(start/finish bracketing episodes) · `memory_forget`/tombstone · export as markdown generated
view (human window) · scope hierarchies · confidence decay by age (advisory ranking only) ·
FTS5 synonym table fed BY the caller (the LLM teaches the index its own aliases) ·
audit trail (donor B `audit_events`: every mutation logged; orphan reclaimed 2026-07-18) ·
native bridge import of CLAUDE.md/AGENTS.md/MEMORY.md (donor B closed source enum + taint
fence before construction — the path to absorb today's file-based memory; orphan reclaimed
2026-07-18) · the three-outcome recall contract — `grounded` / `missing_evidence` / `abstain`
(the tri-state is live: `missing_evidence` counts each exclusion reason)

### DEFERRED (from the vision; unchanged)
consequence loop (outcome→rank→falsification) · preference learning/PEFT · taint SCANNER
(the flag+envelope are core; the classifier is not) · extract/classify pipelines · relation
graph beyond supersedes · visual document projection · hosted publication · vector/semantic
recall (only if caller-expanded FTS5 measurably fails in dogfood) · multi-tenant · HTTP

## 2. Engine architecture

```
caller (LLM in claude-code; 90%)          owner (human; audit/rare)
        │ MCP stdio                                │ later: CLI / md export (generated view)
        ▼                                          ▼
┌─ SURFACE ─────────────────────────────────────────────────────┐
│ tools: memory_ingest (single|batch) · memory_retrieve         │
│        memory_digest · memory_get · memory_list               │
│ every response: evidence envelope (DATA wrapper,              │
│ ADVISORY_NOT_AUTHORITY, taint flag, provenance, explain)      │
└──────────────┬────────────────────────────────────────────────┘
               ▼
┌─ ENGINE (pure core, imperative shell at edges) ───────────────┐
│ intake:   validate(Capsule v1, reject no-provenance)          │
│           → default-fill (scope, class, times injected)       │
│           → source_hash idempotency → dedup-scan → HINT       │
│ recall:   multi-term FTS5 match → bm25 rank (+usage tiebreak) │
│           → token-budget trim (layered) → envelope | ABSTAIN  │
│ digest:   compact index projection (one SELECT set)           │
│ lifecycle: supersede chain · exact-dedup                      │
│ invariants: deterministic (seq ids, injected clock) ·         │
│   advisory-always · abstain-on-nomatch · degradable ·         │
│   hermetic (zero net) · store never renders instructions      │
└──────────────┬────────────────────────────────────────────────┘
               ▼
┌─ STORE (single dir, single SQLite file + spool) ──────────────┐
│ capsules   canonical record (capsule-as-canonical-JSON        │
│            + indexed columns)                    [authority*] │
│ fts        FTS5 mirror                 [derived, rebuildable] │
│ usage      counters                    [derived, rebuildable] │
│ relations  supersedes                            [canonical]  │
│ spool/     crash-safe ingest staging             [transient]  │
└───────────────────────────────────────────────────────────────┘
*authority of what-was-stored only — never of what-is-true (advisory law)
```

Dependency direction: surface → engine → store. Derived tables rebuild from the canonical
table; deleting `fts`+`usage` loses nothing. The Capsule schema stays frozen — every new
feature lands as a sidecar table or an envelope field, never a Capsule field change.

## 3. Route changes vs the original plan (applied under owner veto)

1. **FTS5+bm25 at day one** (was: keyword scan now, FTS5 later). Reason: with caller-expanded
   multi-term queries, FTS5 is the honest lexical engine that makes hermetic recall GOOD, not
   provisional; rusqlite bundles it; ~same LOC as the scan. Kills the planned rework.
2. **`memory_digest` added to CORE** (new 5th tool). Reason: session-start context assembly is
   the #1 recall moment for an LLM; one cheap projection serves it; pairs with a future
   SessionStart hook in the NOTT plugin (integration, zero engine change).
3. **h4 consolidate → dedup-HINT in ingest** (dialogue consolidation). Reason: auto-merge
   heuristics guess; the caller knows. Engine flags, LLM decides, supersede executes.
4. **Evidence envelope + taint flag promoted to CORE** (was implicitly deferred with the
   scanner). Reason: the primary consumer eats recalled bytes as near-instructions;
   the armor is cheap (formatting + one flag already in the schema); the SCANNER stays
   deferred.
5. **Usage counters in CORE** with the legal guard: ranking signal only, never authority.
6. **Batch ingest in CORE.** LLMs act N-at-a-time; arrays kill round-trips.

Unchanged: Capsule v1 freeze, SQLite single-file, stdio-only, advisory-always, abstain,
degradable, zero-Python, gold-bar, walking-skeleton-first, 5-day adoption DoD.

## 4. Offline-first sync (`src/merge.rs` → `src/sync.rs` → the `sync` CLI)

Three layers, dependency direction downward, the merge logic written once:

- `src/merge.rs` — `plan_merge`, the pure core. Takes both sides' core rows
  (capsules, relations, tombstones) and returns the delta plan plus the
  incoming-id → local-id remap. No I/O, no clock, no randomness; every output is
  internally re-sorted, so identical inputs yield identical plans (the `export`
  determinism idiom).
- `store::merge_from` — the imperative shell. Opens the incoming store file
  read-only, computes the plan, applies it in ONE transaction: the local store is
  either fully merged or byte-untouched. A corrupt, non-store, or stale-schema
  incoming file fails closed with a typed error before anything is written.
- `src/sync.rs` — the transport shell behind `nmemory sync`. Fetches the remote
  mirror to a private temp path FIRST (a failed fetch never opens the local
  store), merges via `merge_from`, and with `--push` copies the merged local file
  back so the mirror converges to the superset. Every failure is a typed
  `SyncError`, and on every one of them the local store is left intact — a push
  failure leaves it fully merged with only the mirror stale.

**What moves.** Exactly three row families cross stores. *Capsules*: identity is
`provenance.source_hash` — an incoming capsule whose content hash the local store
already holds collapses onto the local id; a genuinely-new one is minted a fresh
`cap-<seq>` above the local ceiling, in incoming-sequence order. *Relations*:
rewritten through the id remap, unioned, deduped by kind + endpoints; a dangling
edge is dropped, never an error. *Tombstones*: forget wins across stores, matched
by content hash (id-remap as fallback); the keyed `content_hmac` is NOT portable —
each propagated tombstone is re-keyed under the local HMAC key on apply.

**What never moves.** Per-store sidecars stay where they were written: usage
counters, aliases, classification and epistemic sidecars, caller-fed vectors,
session records, and the audit journal. FTS rows are derived and rebuilt locally.
A CLI sync therefore writes no per-id audit rows — merged capsules surface under
`journal.out_of_band` in `memory_digest`'s coverage leg (state without audit
history; the chain itself stays `ok`) — while the `memory_merge` MCP tool audits
every id it adds or forgets.

**Ordering and idempotency.** The plan is deterministic and the summary type is
`Eq`: identical inputs reconcile to identical summaries, and re-reconciling an
already-merged mirror is a no-op (`+0 capsules`). Divergent claims are NEVER
auto-resolved — two different contents are two capsules, both survive, and
superseding one afterward is the caller's decision.

**The transport seam.** The remote is reached ONLY through the `Transport` trait.
Production is `ScpTransport`, which shells out to `scp` as a separate OS process:
the binary links no network stack, and the serve/engine path never names the
trait, so the hermetic zero-network serve guarantee is unchanged. Sync is
explicit, owner-invoked, opt-in — NEVER a background daemon.

## 5. MCP App resources (progressive enhancement)

The server advertises two MCP App resources over `resources/list` /
`resources/read`, MIME `text/html;profile=mcp-app`: `ui://nmemory/document`, a
readable master-detail document over `memory_export`'s generated markdown
(outline, sections, and the exact source), and `ui://nmemory/visual`, which
renders `memory_visual`'s deterministic Mermaid projections. Each is one
self-contained HTML string in `src/mcp_app.rs` — no external scripts, styles, or
URLs, no storage or device permissions; the host owns sandboxing, and the view
only speaks JSON-RPC over `postMessage` and renders escaped text. They are
progressive enhancement, not surface: hosts without MCP Apps support keep
receiving the tools' plain text payloads, and no tool contract changes.
