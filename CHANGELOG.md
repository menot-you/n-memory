# Changelog

All notable changes to nMEMORY. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/). Dates are the public release dates on the
`menot-you/n-memory` mirror. Every entry states what shipped, verified against
the released source — never against commit messages alone.

## [0.3.0] — 2026-07-31

### Added

- `about` — a ninth `memory_relate` kind recording that a capsule is about a
  TOPIC node (`from --about--> to`), navigational only: never a dag input,
  never a recall exclusion, and byte-inert to the digest. By convention the
  topic node is a `doc` capsule, but no kind is enforced — a topic has no
  lifecycle to open or close, which is what separates it from `part_of`. It is
  the one kind that refuses a tombstoned `to`, because a forgotten topic can
  never scope a recall and the edge could never be read.
- `topic_id` on `memory_retrieve` — an optional scope fence (exact `cap-<n>`;
  a slug never resolves) that fences both lanes to the topic's `about` members
  ∪ the topic itself, ACROSS projects, AND-composing with
  project_id/project_prefix/session_id/effort_id — the two id-set fences by
  intersection. The outcome echoes `topic{topic_id, member_total}` and every
  grounded row carries `topic_role`. Scope is not eligibility: a fenced-in
  superseded, falsified, archived, or expired member still surfaces under
  `excluded`, and a fenced zero-match abstains. Omitting it is dormant — the
  envelope is byte-identical.
- `ui://nmemory/console` — a third MCP App resource, bound to `memory_digest`,
  and the home surface: handoff threads and store shape, the blocks dag as
  ready / blocked / done work, the mission spine's epic roots, the drawn
  projection, and the stored memories with recall. It opens on the ready-set
  plus exactly one next action, and it fails closed where the digest does — a
  blocks-cycle or a `grounded_in` cycle shows the concrete cycle and the
  repair, never a fabricated answer.
- Writes from the console, limited to the recoverable append-only verbs
  (`memory_ingest`, `memory_classify`, `memory_relate`, `memory_pin`). Each one
  passes a review step that shows the exact MCP tool call before sending, and
  the server's own answer or rejection is displayed verbatim.
  `memory_forget`, `memory_merge`, and `memory_consolidate` are unreachable
  from every app. Closing a work item stays two acts — capture the evidence,
  then record the `witnesses` edge — so no app certifies its own close.

### Changed

- `ui://nmemory/visual` DRAWS the projection instead of dumping the Mermaid
  string: the emitted grammar is parsed, nodes are laid out by contract depth,
  edges become inline SVG arrows carrying the server's own class colours, and
  the exact Mermaid stays in a source panel. All four views (`dag`,
  `relations`, `tiers`, `sessions`) render. No Mermaid renderer is embedded and
  the resource stays URL-free.
- The design tokens, the `ui/*` handshake, the capsule detail renderer, and the
  diagram renderer now exist exactly once, shared by all three resources.
- `memory_digest` / `memory_bootstrap` `open_efforts` now admits an effort when
  the project fence holds the epic OR any `part_of` member — previously only
  the epic's own project surfaced it — because a cross-project effort is
  reachable from any of its projects. Witness, supersede, and tombstone
  semantics are unchanged.

## [0.2.1] — 2026-07-28

### Changed

- Re-release of 0.2.0. The functional library and binary source is unchanged;
  what moves is release metadata — the version itself, the release workflows,
  and this changelog. The
  v0.2.0 GitHub release name is permanently unusable: this repository has
  immutable releases enabled, the release was published by hand before the
  pipeline attached its binaries, and an immutable release can neither
  receive assets after publication nor free its tag name for reuse. From
  this version the pipeline cuts the tag and creates the release with its
  assets and its changelog entry in one act (`auto-release`), so that
  failure mode cannot recur — and a version with no changelog entry is
  refused before anything outward exists.
- crates.io publishing begins at this version.

### Known

- The Windows x86_64 target regressed in 0.2.0 (unix-only file-descriptor
  APIs on a new code path) and is absent from this release's binaries;
  v0.1.2 was the last version with a Windows build. The npm installer
  refuses that platform by name instead of guessing. Tracked for a
  follow-up release.

## [0.2.0] — 2026-07-28

### Added

- `memory_pin` — the 22nd MCP tool: mark one capsule so decay never erodes it
  and consolidation's archive planner never sweeps it. Not a bypass: a pinned
  capsule that is superseded, quarantined, or falsified stays exactly as
  excluded from recall as without the pin. The flag surfaces wherever capsules
  already do (`memory_get`, `memory_list`, `memory_digest`).
- `nmemory git-scan` — a CLI verb (never a served tool) that checks stored
  facts against a real repository: does each anchor still resolve, do commits
  still mention the capsule. Records corroborated / drifted / missing and
  never invents a verdict it cannot back. Git is reached only from this verb;
  the MCP serve path still opens no socket and spawns no process.
- `nmemory backup --to <path>` — a live snapshot through SQLite's online
  backup API, which a plain file copy of a hot store cannot promise.
- `nmemory relate` — one typed edge from the shell, same closed vocabulary
  and container rules as the `memory_relate` tool.
- Three relation kinds, closing the vocabulary at eight: `proposes`
  (navigational — records an intent to replace, with no dag and no recall
  effect until explicitly converted), `part_of` (membership in a container),
  `grounded_in` (mission anchoring; feeds the digest's `mission` section).
- `memory_retrieve` gains `effort_id` scoping and `corroboration_blend`;
  `memory_import` reads an already-exported Notion directory.
- `memory_digest` gains `open_efforts`, `mission`, `unanchored`, and git
  witness `sources` sections, and every capped list now declares its exact
  pre-cap size — a short list is distinguishable from a truncated one.
- Distribution: an npm wrapper that downloads the platform archive and
  verifies it against the release's `SHA256SUMS` before unpacking (integrity,
  not authenticity — stated in the installer itself), and a crates.io publish
  job. Both are secret-gated and skip cleanly when unarmed.

### Changed

- On-disk schema 11 → 20. The first open on 0.2.0 migrates an existing store
  in place, automatically; an older binary then refuses the migrated store
  rather than misreading it. Back up the database and its sibling `.hmac-key`
  as one unit before upgrading — the migrating open is a one-way door.

### Fixed

- `nmemory backup` no longer walks through the door it guards: a missing
  source database is refused before any open (it used to create an empty
  store and "back up" that), and the snapshot runs read-only, so backing up a
  pre-0.2.0 store no longer migrates it first.
- A store fault during git-scan's mention pass now propagates instead of
  being recorded as "no mention"; one non-UTF-8 commit no longer aborts a
  whole scan page.
- The npm launcher no longer overwrites a child's signal death with a plain
  exit 1, and the installer no longer leaks its temp directory on a bad
  archive.

## [0.1.2] — 2026-07-23

### Added

- Windows x86_64 release target (zip + `.mcpb`).
- `.mcpb` bundles built per target, for one-click MCP installs.
- README: Tools section and the standard `mcpServers` config block.

### Changed

- Release rebuilds of an existing tag fetch the bundler from `main`, so a
  dispatched rebuild cannot miss it.

## [0.1.1] — 2026-07-22

### Added

- `memory_merge` — reconcile a second store into this one (schema v11).
- MCP Apps resources `ui://nmemory/document` and `ui://nmemory/visual`, with
  plain-text fallback for hosts that render neither.
- Session lock: one writer per store, enforced at open.
- Anchor-root configuration for `anchor_live` probes.

## [0.1.0] — 2026-07-20

First public release.

- Capsule store: capture with mandatory provenance (`source` + `anchor`,
  rejected otherwise), recall as evidence with exactly three honest outcomes —
  grounded, missing_evidence, abstain — never a fabricated fourth.
- 21 MCP tools over stdio; every answer framed `ADVISORY_NOT_AUTHORITY`.
- Hermetic serve: no network stack linked; one SQLite file on your disk;
  explicit `sync` shells out to `scp` in a separate process.
- One-line installer (`https://no.tt/install`) with per-platform release
  binaries and `SHA256SUMS`.
