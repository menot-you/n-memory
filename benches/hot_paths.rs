//! Deterministic instruction-count benchmarks for nMEMORY's three hot paths.
//!
//! WHY INSTRUCTION COUNTS AND NOT WALL TIME. A wall-clock benchmark on a shared
//! runner measures the runner: another job, a thermal event, or a noisy
//! neighbour moves the number more than any plausible code change, so the only
//! way to keep such a benchmark green is to set its threshold wide enough to
//! miss real regressions. iai-callgrind runs each benchmark under Valgrind's
//! callgrind and counts INSTRUCTIONS EXECUTED. That number is a property of the
//! code and its inputs, so it repeats to the instruction across machines and
//! across runs, and a 3% regression is a real 3% regression rather than noise.
//!
//! THE LIMITS ARE iai-callgrind's OWN, and that is a rule rather than a
//! preference (`d29-one-gate-per-law`). The tool already compares against a
//! saved baseline and fails on a soft or hard limit breach; adding a comparator
//! of this repository's own would put two enforcers on one law, and the day they
//! disagreed the reviewer would have to work out which one was authoritative.
//! What the repository adds is the DECISION rule — moving a limit costs a
//! ratchet entry naming the axis — and that lives in `quality/ratchets.toml`,
//! never here.
//!
//! WHAT IS AND IS NOT MEASURED. Each benchmark sets up its store OUTSIDE the
//! measured region, so the count describes the path under test rather than
//! SQLite's table creation. The clock is injected at a fixed instant, because a
//! benchmark that read the wall clock would branch differently on freshness
//! windows depending on the day it ran.
//!
//! VALGRIND IS REQUIRED TO RUN THIS, and it is NOT optional. Without it
//! `cargo bench` fails naming the missing tool rather than reporting a number it
//! could not measure — the same fail-closed rule every gate here follows.

use std::hint::black_box;
use std::path::Path;

use iai_callgrind::{
    Callgrind, EventKind, LibraryBenchmarkConfig, library_benchmark, library_benchmark_group, main,
};
use nmemory::capsule::AuthorityClass;
use nmemory::ingest::{IngestDefaults, IngestRequest, ingest};
use nmemory::retrieve::{RetrieveQuery, retrieve};
use nmemory::store::{ListFilter, Store};
use time::OffsetDateTime;
use time::macros::datetime;

/// A FIXED instant. Reading the wall clock would make freshness comparisons
/// branch differently depending on the day, and the instruction count with them.
const NOW: OffsetDateTime = datetime!(2026-07-29 12:00:00 UTC);

fn defaults() -> IngestDefaults {
    IngestDefaults {
        project_id: "bench".to_string(),
    }
}

fn request(index: usize) -> IngestRequest {
    IngestRequest {
        // Long enough that hashing and the taint scan dominate the call rather
        // than the surrounding bookkeeping, and distinct per index so no capture
        // is deduplicated away by content hash.
        content: format!(
            "capsule {index}: the store hashes this content and scans it for \
             instruction taint before any row is written. decidimos usar SQLite \
             aqui, and the gate MUST reject a dangling citation."
        ),
        source: "bench".to_string(),
        anchor: format!("bench/fixture.md:{index}"),
        confidence: None,
        valid_from: None,
        valid_to: None,
        project_id: Some("bench".to_string()),
        authority_class: Some(AuthorityClass::ObservedFact),
        instruction_taint: None,
        supersedes: None,
        session_id: None,
        event_time: None,
    }
}

/// A store carrying `count` capsules, built OUTSIDE any measured region.
fn seeded_store(count: usize) -> Store {
    let mut store = match Store::open_in_memory() {
        Ok(store) => store,
        Err(error) => panic!("bench setup could not open an in-memory store: {error}"),
    };
    for index in 0..count {
        if let Err(error) = ingest(&mut store, request(index), defaults(), NOW) {
            panic!("bench setup could not seed capsule {index}: {error}");
        }
    }
    store
}

/// An empty store, so the ingest benchmark measures ONE capture rather than one
/// capture plus the cost of whatever the setup already wrote.
fn empty_store() -> Store {
    match Store::open_in_memory() {
        Ok(store) => store,
        Err(error) => panic!("bench setup could not open an in-memory store: {error}"),
    }
}

fn ranking_query() -> RetrieveQuery {
    RetrieveQuery {
        terms: vec!["SQLite".to_string(), "citation".to_string()],
        project_id: Some("bench".to_string()),
        ..RetrieveQuery::default()
    }
}

// --- 1. the ingest content-hash path -----------------------------------------
//
// One capture end to end: content hashing, the taint scan, capsule validation,
// and the row write. This is the path every `memory_ingest` call pays.
#[library_benchmark]
#[bench::single_capture(empty_store())]
fn ingest_content_hash_path(mut store: Store) {
    let outcome = ingest(&mut store, request(0), defaults(), NOW);
    black_box(outcome.is_ok());
}

// --- 2. the retrieve ranking path --------------------------------------------
//
// A two-term query over a seeded store: FTS5 match, then the ranking and
// exclusion pipeline that decides what grounds the answer. The store is seeded
// outside the measured region, so the count is the query's, not the seeding's.
#[library_benchmark]
#[bench::over_64_capsules(seeded_store(64))]
fn retrieve_ranking_path(mut store: Store) {
    let response = retrieve(&mut store, &ranking_query(), NOW, Path::new("/bench"));
    black_box(response.is_ok());
}

// --- 3. the digest projection ------------------------------------------------
//
// THE STORE-SIDE READ, NAMED HONESTLY. The MCP `memory_digest` handler is an
// `async fn` on the server, and benchmarking it would fold a tokio runtime's
// scheduling into the instruction count — which is exactly the nondeterminism
// this file exists to avoid. Its dominant cost is
// `store.list(ListFilter::default())` and the projection over what comes back,
// and that is what is measured here. The async wrapper is NOT covered, and
// saying so is worth more than a number that silently included a runtime.
#[library_benchmark]
#[bench::over_64_capsules(seeded_store(64))]
fn digest_projection_path(store: Store) {
    let listed = store.list(ListFilter::default());
    let _ = black_box(listed.map(|capsules| capsules.len()));
}

library_benchmark_group!(
    name = hot_paths;
    benchmarks = ingest_content_hash_path, retrieve_ranking_path, digest_projection_path
);

// THE REGRESSION LIMITS, and they are the tool's own — no custom comparator
// exists anywhere in this repository (`d29-one-gate-per-law`).
//
//   SOFT 5%  a warning. Instruction counts move slightly with a compiler
//            upgrade or a dependency bump, and failing on that would train
//            everyone to raise the limit.
//   HARD 15% a failure. A change of this size is a change in what the code
//            DOES, and it MUST be explained rather than absorbed.
//
// Moving either number is a change to the standard, not to the code, so it
// costs a `d<N>-<slug>` decision naming the `bench` axis — the same rule
// quality/ratchets.toml holds for every other floor.
// The limits hang off the CALLGRIND TOOL config rather than the benchmark
// config, because they are a property of the metric the tool collects — the
// same file can carry a second tool with limits of its own.
main!(
    config = LibraryBenchmarkConfig::default()
        .tool(
            Callgrind::default()
                .soft_limits([(EventKind::Ir, 5.0)])
                .hard_limits([(EventKind::Ir, 15.0)])
        );
    library_benchmark_groups = hot_paths
);
