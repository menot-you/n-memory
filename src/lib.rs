#![forbid(unsafe_code)]
//! # nmemory — hermetic, single-file, local memory for LLM agents.
//!
//! Capture with mandatory provenance; recall that is grounded-or-abstain and
//! always labeled `ADVISORY_NOT_AUTHORITY`. Contract: `prd.nMEMORY.2.md`
//! (canonical) + `ARCHITECTURE.md` (the four LLM-first laws). Laws that never
//! bend here:
//!
//! - **Advisory, never authority.** Recall locates evidence; it never closes
//!   or influences an outcome. Degradable: nmemory down never blocks work.
//! - **No capsule without provenance** — enforced at construction
//!   ([`capsule::Capsule`]).
//! - **Hermetic.** No network at runtime, single-file SQLite, stdio only.
//! - **Deterministic store.** The store reads NO clock and NO randomness;
//!   `now` is injected at the surface boundary; ids are sequence-derived.
//!
//! Layer map (dependency direction: surface → engine → store):
//! [`server`] (stdio MCP) → [`ingest`]/[`retrieve`] → [`store`] +
//! [`spool`], all over the frozen [`capsule::Capsule`] v1.

pub mod bridge;
pub mod capsule;
pub mod classify;
pub mod consolidate;
pub mod export;
pub mod extract;
pub mod ingest;
pub mod journal;
pub mod relation;
pub mod retrieve;
pub mod server;
pub mod spool;
pub mod store;
pub mod substrate;
pub mod taint;
pub mod visual;
