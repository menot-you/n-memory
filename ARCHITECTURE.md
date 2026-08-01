# nMEMORY — feature map + engine architecture (LLM-first)

```
status:    engine + feature map for the shipped crate (v0.3.0)
axiom:     nMEMORY is FOR LLMs. The primary consumer (~90%) is an agent inside a
           session, not a human. Every design choice optimizes for that caller:
           token economy, structured shapes, caller-side intelligence, injection
           safety. The human is the owner and auditor, not the hot-path reader.
```

## Shipped surface (current — 22 MCP tools)

The feature map in §1 below is the original 2026-07-18 design plan (CORE / NEXT /
DEFERRED). Most of what it filed under NEXT and DEFERRED has since shipped. The
**current live surface is 22 MCP tools**, all tested:

- **capture** — `memory_ingest` (single/batch, provenance-mandatory, optional
  caller-declared fact time) · `memory_import` (CLAUDE.md / AGENTS.md /
  memory-dir, taint-fenced)
- **recall** — `memory_retrieve` (FTS5+bm25, grounded-or-abstain, optional caller-fed
  cosine vector lane, optional fact-time window, optional exact store-local capsule
  label fence) · `memory_get` · `memory_list` · `memory_digest` (session-start
  projection) · `memory_bootstrap` (cold-start pack)
- **organize** — `memory_classify` · `memory_extract` · `memory_relate`
  (supersedes / derived_from / witnesses / blocks / falsifies / proposes /
  part_of / grounded_in / about — the closed nine) · `memory_alias` ·
  `memory_consolidate`
- **lifecycle** — `memory_forget` (tombstone / redact / purge) · `memory_outcome` ·
  `memory_preference` · `memory_pin` (S1: decay-exempt + archive-veto + surfacing;
  NEVER eligibility — fenced capsules stay fenced, taint dominates pin) ·
  `memory_session_start` · `memory_session_finish`
- **views** — `memory_export` (markdown generated view) · `memory_visual`
  (dag / relations / tiers / sessions)
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
- **app views** — three MCP App resources (`text/html;profile=mcp-app`):
  `ui://nmemory/console` (the home surface over `memory_digest`) ·
  `ui://nmemory/document` (readable document over `memory_export`) ·
  `ui://nmemory/visual` (the drawn projection over `memory_visual`) — §6.
  Resources, not tools: the 22-tool surface above is unchanged by them.

The stdio router is the standalone connector. It exposes READ and PROPOSE, never
CLOSE: nothing reachable over this connector can declare a piece of work finished.
`staged: true` creates a fenced proposal, while compatible `ratified` / `rejected`
history remains readable for authority-bearing consumers outside this connector —
the connector shows you that history, and never writes a verdict into it.

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
   batch). Session bracketing ships as explicit start/finish records plus capsule labels;
   hooks use that machinery without becoming store authority.

`memory_retrieve.session_id` fences on the capsule row's label with character-exact SQL
equality before FTS ranking or vector decode/top-K. It is a **store-local capsule label
fence**, not authentication and not a globally unique bracket identity: accepted bytes
are never trimmed, folded, normalized, shape-checked, or length-checked; only a
whitespace-only input is rejected at the server boundary. Recall never looks up the
`sessions` table and applies no TTL, so finished and orphaned labels remain recallable.
The label AND-composes with project and fact-time fences. A non-match means no capsule
with that label, never an unknown session.

## 1. Feature map

### CORE (walking skeleton — usable memory)
| feature | what it is | LLM-first note |
|---|---|---|
| Capsule v1 | frozen record: content · provenance{source,anchor,source_hash} MANDATORY · confidence · freshness{valid_from,valid_to?} · scope{project_id} · authority_class · instruction_taint | fixed shape = cheap parsing; no capsule without origin |
| durable spool | crash-safe staged writes (copied organ, ssot-spool) | capture never loses a byte on crash |
| SQLite store | single-file, append-ordered seq ids, no clock/random inside, canonical snapshot | deterministic replay; hermetic |
| ingest | provenance-mandatory, source_hash idempotent, **smart defaults** (scope from caller context, authority_class=agent-inferred, timestamps injected), **batch array**; optional `event_at` point XOR inclusive `event_from`+`event_to` range | fact time is a local sidecar on fresh capture; a dedup collapse keeps the first declaration |
| dedup-hint (dialogue consolidation) | ingest response returns "similar existing: cap-N (score)" — the CALLER decides supersede/skip/keep | uses caller intelligence instead of auto-merge heuristics |
| retrieve | **FTS5+bm25** lexical match over multi-term caller-expanded queries + alias-taught OR-group expansion; optional exact store-local capsule `session_id` label fence; optional inclusive `time_window` fact-time fence before ranking; optional `effort_id` membership fence (results fenced to one persisted epic's members, resolved via the SAME validated scope memory_relate's `part_of` container gate feeds); optional `topic_id` membership fence (results fenced to one topic node's `about` members ∪ the topic itself, ACROSS projects — a topic has no lifecycle, so it needs no persisted kind and no member minimum); deterministic sort key (term coverage, bm25, decayed weight = confidence × 2^(-age_days/90), valid_from, usage late key, id); **token-budgeted layered results** (headline → full capsule on demand); grounded-or-ABSTAIN | the engine is honest, the caller is smart; session/project/fact-time/effort/topic fences AND-compose — the two id-set fences by intersection — and fact time never feeds decay |
| evidence envelope | every result wrapped as DATA: `ADVISORY_NOT_AUTHORITY` + taint flag + provenance + freshness + matched-terms explain | injection armor + trust calibration in one shape |
| digest | one-call compact store projection (~counts, scopes, newest, most-recalled, N headlines) plus optional advisory telemetry from the existing miss and lane-override ledgers; every capped list ships its EXACT pre-cap `*_total` and every `by_project` row ships `live` beside `count` | the MEMORY.md-analogue; the session-start hot path; a projection declares its own completeness, so a truncated list is never read as a whole one and a census (live + superseded) is never read as an inventory; miss terms are bounded single-line previews and disappear under project fences because the ledger has no project attribution |
| get / list | by id; by scope/kind/recency; `get` alone reads the fact-time declaration | the fact-time sidecar stays off list/digest/export/retrieve envelopes |
| fact time | caller-declared inclusive range (`event_at` stores a degenerate range) plus `declared_at`, in a schema-v15 sidecar | event occurrence is distinct from capture time and freshness; absent means undated, never inferred |
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
│ recall:   multi-term FTS5 match → state/currency fences       │
│           → optional fact-time fence → bm25/vector rank       │
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
│ event_time declared fact-time range        [local sidecar]    │
│ relations  supersedes                            [canonical]  │
│ spool/     crash-safe ingest staging             [transient]  │
└───────────────────────────────────────────────────────────────┘
*authority of what-was-stored only — never of what-is-true (advisory law)
```

Dependency direction: surface → engine → store. Derived tables rebuild from the canonical
table; deleting `fts`+`usage` loses nothing. The Capsule schema stays frozen — every new
feature lands as a sidecar table or an envelope field, never a Capsule field change.

Caller-declared fact time is deliberately separate from all three other clocks. Capsule
`created_at` says when the store captured the bytes; freshness says when the claim may
ground recall; `event_time` says when the described fact happened. `memory_ingest`
accepts either one RFC3339 `event_at` point or the pair `event_from` + `event_to`; the
store writes the normalized inclusive range in the same transaction as the capsule and
FTS row. Dedup returns before that write, so the first declaration wins and an omitted
declaration is never backfilled by a later collapse.

`memory_retrieve.time_window` accepts one or both RFC3339 bounds. After all state and
freshness fences, but before any term/vector/fused ranking — including before the
vector lane's top-K selection — a dated capsule survives when its event range
intersects the query window inclusively. A strict gap counts as `outside_time_window`;
no declaration counts as `undated`. With no window the sidecar is not read, historical
ranking and response bytes stay unchanged, and fact time never feeds confidence decay.
The declaration is visible only on `memory_get.event_time`.

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
  store), then merges via `merge_from`. With `--push` it stages a private
  SQLite online-backup snapshot from the still-open merged connection; the
  snapshot includes committed WAL state even while another connection remains
  open. It replaces that candidate's fact-time rows with the fetched
  destination's validated declarations rebound by content hash, then pushes the
  candidate. Fetch/temp errors leave local byte-untouched; a store error never
  leaves a partial merge, although opening local may first migrate or heal it. A
  staging or transport `SyncError::Push` leaves local fully merged and the
  mirror untouched or stale.

**What `merge_from` moves.** Exactly three row families cross stores. *Capsules*: identity is
`provenance.source_hash` — an incoming capsule whose content hash the local store
already holds collapses onto the local id; a genuinely-new one is minted a fresh
`cap-<seq>` above the local ceiling, in incoming-sequence order. Its capsule
`session_id` label is copied verbatim. *Relations*:
rewritten through the id remap, unioned, deduped by kind + endpoints; a dangling
edge is dropped, never an error. *Tombstones*: forget wins across stores, matched
by content hash (id-remap as fallback); the keyed `content_hmac` is NOT portable —
each propagated tombstone is re-keyed under the local HMAC key on apply.

**What `merge_from` never moves.** Per-store sidecars stay where they were written: usage
counters, aliases, classification and epistemic sidecars, caller-fed vectors,
session records, recall-miss and lane-override telemetry, and the audit journal.
Because session records do not move while capsule labels do, an imported label can be
orphaned locally. Labels may also collide: if LOCAL and INCOMING both carry `sess-1`, a
post-merge `session_id: "sess-1"` recall intentionally grounds capsules from both stores.
That is store-local label equality, not proof of one shared bracket.
FTS rows are derived and rebuilt locally. Digest `recent_failures` contains only
the newest recall-miss rows, never lane choices; its folded terms use the shared
bounded single-line headline projection. A `project_prefix` suppresses this
unattributable store-global text. `lane_overrides_total` is separate, non-text,
all-time store-global advisory telemetry for successful explicit disagreements
with auto routing — not evidence of failure or success. Each optional read fails
open independently and is omitted on error; absence is unavailable-or-empty,
never proof that the underlying event did not occur. This paragraph describes
the merge primitive; the CLI's historical `--push` remains a whole-file mirror
operation for those pre-u06 sidecars.

Caller-declared fact time has the stricter store-local push contract. A
newly-added incoming capsule becomes undated on the receiving store; a collapse
keeps any declaration already local. Before `--push`, the private candidate
deletes sender fact time and restores only the fetched destination's declarations,
rebound by unique `source_hash` rather than store-local `cap-N`. A corrupt,
orphaned, or unmappable destination declaration fails the push phase before the
transport writes. On both destination and candidate, the indexed source-hash
projection must agree with decoded canonical provenance for a live capsule or
the retained tombstone identity for a forgotten skeleton; projection drift is
never accepted as authority. Merge and sync therefore never claim the sender's
declaration as destination-local fact time.
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

## 5. Deterministic visual projections

`memory_visual` has four closed projections. `dag` and `relations` render the
store-global relation graph, `tiers` renders the capsule set and is the only view
that accepts `project_prefix`, and `sessions` renders store-local session-label
activity. Adding `sessions` changes neither the 22-tool surface nor the two MCP
App resources; the existing visual app consumes the same generic
`{label, framing, mermaid}` response.

The sessions view reads one SQLite snapshot with one statement. Its label set is
the character-exact, BINARY set union of local `sessions.session_id` rows,
non-null `capsules.session_id` labels, and non-null
`recall_receipts.session_id` labels. Capsule and receipt counts are aggregated
independently before they join that label set, so a label with two saves and
three recalls reports 2/3 rather than multiplying the raw rows. A save is one
physical capsule row, including a forgotten capsule's retained tombstone
skeleton. A recall is one grounded receipt row, regardless of how many capsule
ids that receipt returned.

A label backed by a local bracket is `open` until `finished_at` is present and
`closed` afterward. A label with activity but no local bracket is `label only`;
this is the normal state for a merge-imported capsule label because session
records do not cross stores. If an imported label exactly collides with a local
bracket, its counts aggregate under that local bracket state. Local brackets
render first in `(started_at, session_id)` order, followed by label-only rows in
BINARY label order. Case, surrounding spaces, normalization form, NUL, and all
other bytes represented by valid UTF-8 remain distinct; no trimming, folding,
or Unicode normalization occurs. A NULL, non-TEXT, invalid-UTF-8, or
whitespace-only persisted label and a non-TEXT or malformed bracket timestamp
fail as typed store corruption rather than being coerced, filtered, or rendered.

Session node identifiers are declaration ordinals (`session_1`, `session_2`,
...) rather than label-derived identifiers, so distinct exact labels cannot
collide structurally. Before the shared Mermaid entity encoder runs, a literal
backslash is doubled and each control character plus U+2028/U+2029 is rendered
as an uppercase minimal `\u{HEX}` escape. The encoder then protects Mermaid
syntax. No raw control reaches the diagram, and NUL, a space, and literal escape
text remain visibly distinct instead of collapsing onto one rendered label.
Like every visual projection, the sessions diagram has no clock and its
provenance comment pins counts plus a body hash, so two reads of unchanged state
are byte-identical.

## 6. MCP App resources (progressive enhancement)

The server advertises three MCP App resources over `resources/list` /
`resources/read`, MIME `text/html;profile=mcp-app`:

- `ui://nmemory/console` — the HOME surface, bound to `memory_digest`. Five
  tabs over one store: the handoff threads and store shape, the blocks dag as
  ready / blocked / done work, the mission spine's epic roots, the drawn
  projection, and the stored memories with recall. It opens with the ready-set
  and exactly ONE next action, and it fails closed exactly where the digest
  does — a blocks-cycle or a `grounded_in` cycle shows the concrete cycle and
  the repair, never a fabricated ready/blocked or root answer.
- `ui://nmemory/document` — a readable master-detail document over
  `memory_export`'s generated markdown (outline, sections, exact source).
- `ui://nmemory/visual` — `memory_visual`'s deterministic Mermaid projections
  DRAWN: the emitted grammar is parsed, nodes are laid out by contract depth,
  edges become inline SVG arrows carrying the server's own class colours, and
  the exact Mermaid stays in a source panel. No Mermaid renderer is embedded.

Each resource is one self-contained HTML string in `src/mcp_app.rs` — no
external scripts, styles, or URLs, no storage or device permissions; the host
owns sandboxing, and the view only speaks JSON-RPC over `postMessage` and
renders escaped text. The design tokens, the `ui/*` handshake, the capsule
detail renderer, and the diagram renderer exist exactly once, as
`macro_rules!` blocks the three resources splice through `concat!`.

The console can WRITE, and only the recoverable, append-only verbs:
`memory_ingest`, `memory_classify`, `memory_relate`, `memory_pin`, plus
`memory_retrieve` / `memory_list` / `memory_visual` for reading. Every write
goes through a review step that shows the exact MCP tool call — name and
arguments — before it is sent, and the answer or the server's own rejection is
displayed verbatim. `memory_forget`, `memory_merge`, and `memory_consolidate`
are unreachable from every resource (asserted per resource in the module's
tests): a destructive verb keeps its blast radius in the conversation. Closing
a work item is two acts, never one button — capture the evidence, then record
the `witnesses` edge — because `witnesses` is what takes a node out of
ready/blocked, so the app cannot certify its own close.

They are progressive enhancement, not surface: hosts without MCP Apps support
keep receiving the tools' plain text payloads, and no tool contract changes.
