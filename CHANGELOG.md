# Changelog

All notable changes to nMEMORY. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/). Dates are the public release dates on the
`menot-you/n-memory` mirror. Every entry states what shipped, verified against
the released source — never against commit messages alone.

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
