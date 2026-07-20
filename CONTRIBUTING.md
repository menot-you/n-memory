# Contributing to nmemory

I hold this codebase to a bar that is mechanical, not aspirational: every rule
below is enforced by the compiler or by CI. Green with the tests, or the change
is not ready.

## Setup

Rust stable, pinned by `rust-toolchain.toml` (rustup reads it automatically —
`rustfmt` and `clippy` components install with it).

```sh
cargo build --release   # single binary at target/release/nmemory
cargo test              # the full suite
```

## The bar every change meets

CI (`.github/workflows/ci.yml`) runs exactly this; the `main` ruleset requires
it green before merge:

```sh
cargo fmt --check
cargo clippy --all-targets -- -D warnings
cargo test
cargo llvm-cov --summary-only --fail-under-lines 90
```

Beyond the commands:

- **No `unsafe`.** `#![forbid(unsafe_code)]` is in `src/lib.rs` and stays.
- **No panics in library paths.** Typed errors carry what the caller decides;
  `unwrap`/`expect` do not enter library code.
- **A new rule ships its negative test.** A guarantee without a test that turns
  red on violation is a wish. If you add a law, materialize a violation and
  assert its rejection (see `tests/` for the pattern).
- **Determinism.** Same input, same bytes. Nothing in the store reads a wall
  clock or randomness outside the injected surfaces.

## What gets rejected regardless of code quality

These are the project's laws, not preferences:

- **Network access.** The binary is compiled without a networking stack — no
  sockets, no telemetry, no embedder calls. A dependency that opens a socket is
  rejected.
- **Python.** Zero Python anywhere — runtime, tests, fixtures, tooling. A
  conformance test scans for it, shebangs included.
- **Optional provenance.** A capture path that stores content without
  `source` + `anchor` breaks the core contract.
- **A fourth recall outcome.** Recall returns `grounded`, `missing_evidence`,
  or `abstain`. A path that synthesizes an answer from nothing is the one bug
  this project exists to make impossible.

## Pull requests

- One concern per PR; small diffs review honestly.
- State what changed and what you ran to prove it — paste the command output,
  not a summary of it.
- PRs merge through required checks (`checks` + `coverage`); force-pushes to
  `main` are blocked by ruleset.

## License

nmemory is licensed AGPL-3.0-only. By contributing you agree your contribution
is licensed under the same terms (inbound = outbound).
