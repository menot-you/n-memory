//! # `memory_visual` — deterministic Mermaid projections of the store.
//!
//! A GENERATED VIEW, in the `memory_export` gold-bar sense: a derived
//! artifact regenerated from store state, never hand-edited, byte-identical
//! across two calls on the same store. It carries NO clock — the only
//! non-determinism export forbids — and, like export, pins its own bytes
//! with a `body sha256` over the diagram statements (the provenance
//! comment + header sit above the hashed span, exactly as export's title
//! and digest line sit above its body).
//!
//! Three read-only projections, selected by the closed [`VisualView`] enum:
//!
//! - **`dag`** — the blocks-dag as `graph TD`, ready / blocked / done nodes
//!   each styled distinctly. It obeys the SAME law as `memory_digest`'s dag
//!   section: the projection is recomputed per call via
//!   [`relation::Dag::project_excluding`]; superseded/tombstoned capsules are
//!   dead to it and VANISH; a witnessed blocks-participant is DONE —
//!   proof-carrying closure, styled distinctly and KEPT in the graph (it
//!   stays live and recallable, unlike a dead node); and a LIVE blocks-cycle
//!   among non-done members fails closed — the diagram renders ONLY the
//!   concrete cycle members plus a fail-closed banner, never a partial
//!   healthy graph (mirror of [`crate::server`]'s `DagStatus::Cycle`). The
//!   three exits: `witnesses` closes with proof, `supersedes` replaces,
//!   forget destroys.
//! - **`relations`** — every edge as `graph LR`, one arrow per relation kind
//!   with the kind as the edge label, in `memory_export`'s `## relations`
//!   order (kind rank, then `from`, then `to`, then `at`).
//! - **`tiers`** — capsule ids grouped by effective lifecycle tier
//!   (active / archived / quarantined) as a `flowchart`, each node annotated
//!   with the SHARED headline ([`crate::retrieve::headline_of`] — the one
//!   headline law across every surface).
//!
//! ## Syntax safety
//!
//! Capsule ids are safe Mermaid identifiers, but HEADLINES are stored bytes
//! and may carry anything. [`mermaid_label`] entity-encodes every character
//! that could terminate or confuse a quoted label (`"`, brackets, braces,
//! parens, angle brackets, pipes, backticks) and folds control characters to
//! spaces, so no stored first line can break the diagram — the sanitizer's
//! postconditions are pinned by test below. [`node_id`] independently
//! sanitizes the identifier position.
//!
//! This module is PURE: every renderer is a deterministic function of its
//! inputs (edges, tombstones, tier rows). The [`crate::server`] handler does
//! the store I/O and hands the data down. The `dag`/`relations` renderers are
//! STORE-GLOBAL by construction — they take no fence, so nothing can restrict
//! them to a subtree and hide a cross-fence blocks-cycle behind a healthy
//! graph; only `tiers` is scoped, and the handler fences it before this layer.

use std::collections::{BTreeMap, BTreeSet};

use serde::{Deserialize, Serialize};

use crate::capsule::sha256_hex;
use crate::relation::{Dag, RelationKind, RelationRecord};
use crate::retrieve::{AdvisoryLabel, DataFraming};
use crate::store::Tier;

/// The closed projection vocabulary on the wire. An out-of-set value is a
/// schema-level rejection (no catch-all arm), exactly like every other
/// closed nmemory enum.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize, schemars::JsonSchema)]
#[serde(rename_all = "lowercase")]
pub enum VisualView {
    /// The blocks-dag (`graph TD`): ready vs blocked, fail-closed on cycles.
    Dag,
    /// Every relation edge (`graph LR`), the kind as the arrow label.
    Relations,
    /// Capsule ids grouped by effective lifecycle tier (`flowchart`).
    Tiers,
}

/// `memory_visual` params: the projection, and an optional tiers-only fence.
#[derive(Debug, Clone, Deserialize, schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct VisualParams {
    /// Which projection to render — closed set `dag` | `relations` | `tiers`.
    pub view: VisualView,
    /// Optional subtree fence honored ONLY by `view=tiers` (the capsule-set
    /// view), exactly like `memory_digest`'s capsule sections (exact id or
    /// id + `/...`; an empty or `/`-terminated prefix is rejected with a
    /// teaching error). `dag` and `relations` are STORE-GLOBAL like
    /// `memory_digest` and take no fence: a prefix passed with either is
    /// rejected by the handler, never silently ignored.
    #[serde(default)]
    pub project_prefix: Option<String>,
}

/// `memory_visual` response — the Mermaid string, advisory-labeled like
/// every read surface.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct VisualResponse {
    /// Always the literal `ADVISORY_NOT_AUTHORITY` (unforgeable).
    pub label: AdvisoryLabel,
    /// Always the literal `DATA` (unforgeable).
    pub framing: DataFraming,
    /// The deterministic Mermaid diagram. Paste it into any mermaid
    /// renderer; regenerating over an unchanged store reproduces it
    /// byte-for-byte (the `body sha256` in the provenance comment proves it).
    pub mermaid: String,
}

/// One row for the `tiers` projection: a capsule id, its EFFECTIVE tier, and
/// the shared headline (already run through [`crate::retrieve::headline_of`]).
#[derive(Debug, Clone)]
pub struct TierRow {
    /// Capsule id (`cap-<n>`).
    pub id: String,
    /// Effective lifecycle tier.
    pub tier: Tier,
    /// The headline — the SHARED first-line truncation, not a private copy.
    pub headline: String,
}

/// Render the `dag` projection. `edges` is the whole converted relation set —
/// STORE-GLOBAL, never fenced (fencing could hide a cross-fence blocks-cycle
/// behind a healthy graph; the handler rejects a prefix for this view), so the
/// fail-closed-on-cycle law holds unconditionally. `tombstoned` ids are dead
/// to the projection.
#[must_use]
pub fn render_dag(edges: &[RelationRecord], tombstoned: &BTreeSet<String>) -> String {
    let refs: Vec<&RelationRecord> = edges.iter().collect();
    // Project exactly as memory_digest does — the SAME law, the SAME cycle
    // detection. Ids outside the blocks universe (e.g. global tombstones)
    // are harmless to project_excluding.
    match Dag::project_excluding(edges, tombstoned) {
        Ok(dag) => render_dag_ok(&refs, &dag),
        Err(err) => render_dag_cycle(&err.cycle, err.entangled.len()),
    }
}

/// The acyclic dag: ready ∪ blocked ∪ done live participants, the blocks
/// edges among live nodes, and distinct ready/blocked/done styling.
fn render_dag_ok(edges: &[&RelationRecord], dag: &Dag) -> String {
    let ready: Vec<String> = dag.ready().into_iter().map(str::to_string).collect();
    // Witnessed blocks-participants — DONE (u-r3): styled distinctly and KEPT
    // in the graph (a done capsule stays live and recallable, unlike a dead
    // node, which vanishes).
    let done: Vec<String> = dag.done().into_iter().map(str::to_string).collect();
    // Mirror memory_digest's `blocked` derivation verbatim: live, NON-DONE
    // blocks participants with at least one LIVE blocker.
    let blocked: Vec<String> = edges
        .iter()
        .filter(|e| e.kind() == RelationKind::Blocks)
        .flat_map(|e| [e.from_id(), e.to_id()])
        .collect::<BTreeSet<&str>>()
        .into_iter()
        .filter(|id| dag.is_live(id) && !dag.is_done(id) && !dag.blocked_by(id).is_empty())
        .map(str::to_string)
        .collect();

    // Drawable edges: blocks edges with both endpoints live (a dead endpoint
    // is never rendered, so its edge is dead too; a DONE endpoint stays live,
    // so its edges are drawn and the done node keeps its context). Deduped +
    // sorted.
    let mut drawn: BTreeSet<(SortKey, SortKey, String, String)> = BTreeSet::new();
    for e in edges {
        if e.kind() == RelationKind::Blocks && dag.is_live(e.from_id()) && dag.is_live(e.to_id()) {
            drawn.insert((
                sort_key(e.from_id()),
                sort_key(e.to_id()),
                e.from_id().to_string(),
                e.to_id().to_string(),
            ));
        }
    }

    let mut nodes: Vec<&String> = ready
        .iter()
        .chain(blocked.iter())
        .chain(done.iter())
        .collect();
    nodes.sort_by_key(|id| sort_key(id));

    let mut body: Vec<String> = Vec::new();
    if nodes.is_empty() {
        body.push(placeholder("no blocks-dag in scope"));
    } else {
        for id in &nodes {
            body.push(node_decl(id, id));
        }
        for (_, _, from, to) in &drawn {
            body.push(format!("    {} --> {}", node_id(from), node_id(to)));
        }
        // nMEMORY light design system (tokens from the canonical assets/*.svg):
        // green = flows, amber = waiting, warm neutral = closed with proof.
        body.push("    classDef ready fill:#EDF2EA,stroke:#698664,color:#36332F;".to_string());
        body.push("    classDef blocked fill:#F9EEDD,stroke:#C88B32,color:#36332F;".to_string());
        body.push("    classDef done fill:#F0EAE0,stroke:#4A4640,color:#36332F;".to_string());
        if !ready.is_empty() {
            body.push(class_line(&ready, "ready"));
        }
        if !blocked.is_empty() {
            body.push(class_line(&blocked, "blocked"));
        }
        if !done.is_empty() {
            body.push(class_line(&done, "done"));
        }
    }
    let counts = format!("nodes={} edges={}", nodes.len(), drawn.len());
    frame("dag", "graph TD", &counts, &body)
}

/// The fail-closed cycle render: ONLY the concrete cycle members + a banner,
/// never a partial healthy graph — the SAME law and wording as
/// `memory_digest`'s `DagStatus::Cycle`.
fn render_dag_cycle(cycle: &[String], entangled_total: usize) -> String {
    let mut body: Vec<String> = Vec::new();
    // The cycle vec is already deterministic (rotated smallest-first).
    for id in cycle {
        body.push(node_decl(id, id));
    }
    // Ring edges: each member blocks the next, the last blocks the first
    // (forward `blocks` direction — the DagCycleError contract).
    if !cycle.is_empty() {
        for i in 0..cycle.len() {
            let from = &cycle[i];
            let to = &cycle[(i + 1) % cycle.len()];
            body.push(format!("    {} --> {}", node_id(from), node_id(to)));
        }
    }
    let path = if cycle.is_empty() {
        "cycle".to_string()
    } else {
        let mut p = cycle.join(" → ");
        p.push_str(" → ");
        p.push_str(&cycle[0]);
        p
    };
    let banner = format!(
        "blocks-cycle fail-closed: {path} · no ready/blocked answer · supersede, forget, or \
         witness a member, then re-digest · {entangled_total} entangled"
    );
    body.push(format!("    cycle_banner[\"{}\"]", mermaid_label(&banner)));
    body.push("    classDef cycle fill:#F9EEDD,stroke:#C88B32,color:#36332F;".to_string());
    body.push(
        "    classDef banner fill:#FBE9E4,stroke:#E45A43,color:#36332F,stroke-width:2px;"
            .to_string(),
    );
    if !cycle.is_empty() {
        body.push(class_line(cycle, "cycle"));
    }
    body.push("    class cycle_banner banner;".to_string());
    let counts = format!("cycle={} entangled={entangled_total}", cycle.len());
    frame("dag", "graph TD", &counts, &body)
}

/// Render the `relations` projection: every edge (STORE-GLOBAL, never fenced)
/// as `graph LR`, the kind as the arrow label, in `memory_export`'s
/// `## relations` order.
#[must_use]
pub fn render_relations(edges: &[RelationRecord]) -> String {
    // Dedupe on (kind, from, to) — relate is idempotent, but a view never
    // relies on upstream uniqueness. Sorted by the export relations order.
    let mut seen: BTreeSet<(usize, SortKey, SortKey, String, String, String)> = BTreeSet::new();
    for e in edges {
        seen.insert((
            kind_rank(e.kind()),
            sort_key(e.from_id()),
            sort_key(e.to_id()),
            e.kind().as_str().to_string(),
            e.from_id().to_string(),
            e.to_id().to_string(),
        ));
    }

    let mut node_set: BTreeSet<(SortKey, String)> = BTreeSet::new();
    for (_, _, _, _, from, to) in &seen {
        node_set.insert((sort_key(from), from.clone()));
        node_set.insert((sort_key(to), to.clone()));
    }

    let mut body: Vec<String> = Vec::new();
    if node_set.is_empty() {
        body.push(placeholder("no relations in scope"));
    } else {
        for (_, id) in &node_set {
            body.push(node_decl(id, id));
        }
        for (_, _, _, kind, from, to) in &seen {
            body.push(format!("    {} -->|{kind}| {}", node_id(from), node_id(to)));
        }
    }
    let counts = format!("nodes={} edges={}", node_set.len(), seen.len());
    frame("relations", "graph LR", &counts, &body)
}

/// Render the `tiers` projection: capsule ids grouped by effective tier as a
/// `flowchart`, headline-annotated. Empty tiers are omitted (data-dependent
/// presence, like export's empty sections).
#[must_use]
pub fn render_tiers(rows: &[TierRow]) -> String {
    let mut by_tier: BTreeMap<usize, Vec<&TierRow>> = BTreeMap::new();
    for row in rows {
        by_tier.entry(tier_rank(row.tier)).or_default().push(row);
    }
    for group in by_tier.values_mut() {
        group.sort_by_key(|row| sort_key(&row.id));
    }

    let mut body: Vec<String> = Vec::new();
    // Fixed tier order — the closed vocabulary, active first.
    for tier in Tier::ALL {
        let Some(group) = by_tier.get(&tier_rank(tier)) else {
            continue;
        };
        if group.is_empty() {
            continue;
        }
        body.push(format!(
            "    subgraph tier_{name}[\"{name} — {count}\"]",
            name = tier.as_str(),
            count = group.len(),
        ));
        for row in group {
            let label = mermaid_label(&format!("{}: {}", row.id, row.headline));
            body.push(format!("        {}[\"{}\"]", node_id(&row.id), label));
        }
        body.push("    end".to_string());
    }
    if body.is_empty() {
        body.push(placeholder("store is empty in scope"));
    }
    let tier_count = |t: Tier| by_tier.get(&tier_rank(t)).map_or(0, Vec::len);
    let counts = format!(
        "capsules={} active={} archived={} quarantined={}",
        rows.len(),
        tier_count(Tier::Active),
        tier_count(Tier::Archived),
        tier_count(Tier::Quarantined),
    );
    frame("tiers", "flowchart TB", &counts, &body)
}

// --- shared, deterministic helpers -----------------------------------------

/// Assemble the final diagram: the header, a provenance comment carrying the
/// counts and a `body sha256`, then the body. The sha covers EXACTLY the body
/// statements (the header and provenance line sit above it), mirroring
/// `memory_export`'s pinned-span precedent — no clock, so two calls on the
/// same store are byte-identical.
fn frame(view: &str, header: &str, counts: &str, body: &[String]) -> String {
    let body_text = body.join("\n");
    let sha = sha256_hex(body_text.as_bytes());
    format!(
        "{header}\n%% nMEMORY visual · view={view} · {counts} · body sha256:{sha}\n{body_text}\n"
    )
}

/// A single-node placeholder for an empty projection — a valid diagram is
/// still returned (an empty `graph` body is a parse error in some renderers).
fn placeholder(text: &str) -> String {
    format!("    empty[\"{}\"]", mermaid_label(text))
}

/// `id["label"]` with both positions independently sanitized.
fn node_decl(id: &str, label: &str) -> String {
    format!("    {}[\"{}\"]", node_id(id), mermaid_label(label))
}

/// `class a,b,c <name>;` over sanitized node ids (never emitted for an empty
/// set — an empty class list is a Mermaid syntax error).
fn class_line(ids: &[String], class: &str) -> String {
    let joined = ids
        .iter()
        .map(|id| node_id(id))
        .collect::<Vec<_>>()
        .join(",");
    format!("    class {joined} {class};")
}

/// Numeric-aware sort key for `cap-<seq>` ids — `cap-2` before `cap-10`;
/// non-conforming ids sort after, lexicographically. Mirrors export's
/// `id_sort_key` so the relations order is byte-identical to the export view.
type SortKey = (u64, String);
fn sort_key(id: &str) -> SortKey {
    let numeric = id
        .strip_prefix("cap-")
        .and_then(|suffix| suffix.parse::<u64>().ok())
        .unwrap_or(u64::MAX);
    (numeric, id.to_string())
}

/// Contract-order rank of a relation kind — mirrors export's
/// `relation_kind_rank` (exhaustive: a new kind is a compile error here).
const fn kind_rank(kind: RelationKind) -> usize {
    match kind {
        RelationKind::Supersedes => 0,
        RelationKind::DerivedFrom => 1,
        RelationKind::Witnesses => 2,
        RelationKind::Blocks => 3,
        RelationKind::Falsifies => 4,
    }
}

/// Fixed rank of a lifecycle tier — active, then archived, then quarantined.
const fn tier_rank(tier: Tier) -> usize {
    match tier {
        Tier::Active => 0,
        Tier::Archived => 1,
        Tier::Quarantined => 2,
    }
}

/// Sanitize a string into a safe Mermaid NODE IDENTIFIER: every character
/// outside `[A-Za-z0-9_]` becomes `_`. Capsule ids (`cap-<n>`) collapse to
/// `cap_<n>` — collision-free, since capsule ids differ in their numeric
/// tail; the human id rides in the quoted label instead.
fn node_id(id: &str) -> String {
    id.chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || c == '_' {
                c
            } else {
                '_'
            }
        })
        .collect()
}

/// Entity-encode a string for a QUOTED Mermaid label so no stored byte can
/// terminate or confuse the diagram. Postconditions (pinned by test): the
/// output contains none of `" [ ] { } ( ) < > | ` and no line breaks or other
/// control characters. `#` is encoded FIRST so the entities introduced after
/// it are never re-encoded.
fn mermaid_label(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for c in text.chars() {
        match c {
            '#' => out.push_str("#35;"),
            '"' => out.push_str("#quot;"),
            '[' => out.push_str("#91;"),
            ']' => out.push_str("#93;"),
            '{' => out.push_str("#123;"),
            '}' => out.push_str("#125;"),
            '(' => out.push_str("#40;"),
            ')' => out.push_str("#41;"),
            '<' => out.push_str("#lt;"),
            '>' => out.push_str("#gt;"),
            '|' => out.push_str("#124;"),
            '`' => out.push_str("#96;"),
            // Line breaks and every other control char (incl. the unicode
            // line/paragraph separators a first line can smuggle) fold to a
            // single space — a label is always one visual line.
            '\u{2028}' | '\u{2029}' => out.push(' '),
            c if c.is_control() => out.push(' '),
            c => out.push(c),
        }
    }
    out
}

#[cfg(test)]
mod tests {
    #![allow(
        clippy::unwrap_used,
        clippy::expect_used,
        reason = "tests use unwrap/expect so fixture failures fail at the assertion site"
    )]

    use super::*;
    use time::macros::datetime;

    const AT: time::OffsetDateTime = datetime!(2026-07-18 12:00:00 UTC);

    fn edge(kind: RelationKind, from: &str, to: &str) -> RelationRecord {
        RelationRecord::new(kind, from.to_string(), to.to_string(), AT).expect("valid edge")
    }

    fn row(id: &str, tier: Tier, headline: &str) -> TierRow {
        TierRow {
            id: id.to_string(),
            tier,
            headline: headline.to_string(),
        }
    }

    /// The forbidden raw characters a label must never leak (they would
    /// terminate or confuse a quoted Mermaid label).
    const FORBIDDEN: &[char] = &[
        '"', '[', ']', '{', '}', '(', ')', '<', '>', '|', '`', '\n', '\r', '\t',
    ];

    fn assert_label_safe(label: &str) {
        for c in FORBIDDEN {
            assert!(
                !label.contains(*c),
                "sanitized label leaked {c:?}: {label:?}"
            );
        }
        assert!(
            !label.chars().any(char::is_control),
            "sanitized label leaked a control char: {label:?}"
        );
    }

    #[test]
    fn sanitizer_postconditions_hold_on_hostile_bytes() {
        // Quotes, every bracket family, pipes, backticks, angle brackets,
        // hashes, newlines, CR, tab, unicode line separators, and a stray
        // control byte — the full hostile-headline set.
        let hostile = "a\"b[c]{d}(e)<f>|g`h#i\nj\rk\tl\u{2028}m\u{2029}n\u{0007}o";
        let safe = mermaid_label(hostile);
        assert_label_safe(&safe);
        // Deterministic: same input → same bytes.
        assert_eq!(safe, mermaid_label(hostile));
        // A `#` from the source is encoded, so it can never open a spurious
        // entity: the only `#` runs left are the ones the encoder introduced.
        assert!(safe.contains("#35;"), "source # encoded: {safe}");
        assert!(safe.contains("#quot;"), "quote encoded: {safe}");
    }

    #[test]
    fn hostile_headline_never_breaks_the_tiers_diagram() {
        let rows = vec![
            row("cap-1", Tier::Active, "normal headline"),
            row(
                "cap-2",
                Tier::Active,
                "evil \"] end); classDef x fill:#000; %% \n injected",
            ),
        ];
        let out = render_tiers(&rows);
        // Structural safety: brackets and quotes balance, because the only
        // brackets/quotes are the ones the renderer emits (node syntax), the
        // hostile bytes inside labels having been entity-encoded.
        assert_eq!(
            out.matches('[').count(),
            out.matches(']').count(),
            "unbalanced brackets — a label leaked one:\n{out}"
        );
        assert_eq!(
            out.matches('"').count() % 2,
            0,
            "odd number of quotes — a label leaked one:\n{out}"
        );
        // Every label body (between the quotes of a node decl) is safe.
        for label in quoted_labels(&out) {
            assert_label_safe(&label);
        }
        // Deterministic.
        assert_eq!(out, render_tiers(&rows));
    }

    /// Extract each `"..."` label body from a rendered diagram.
    fn quoted_labels(diagram: &str) -> Vec<String> {
        let mut labels = Vec::new();
        let mut chars = diagram.chars();
        while let Some(c) = chars.next() {
            if c == '"' {
                let mut label = String::new();
                for c in chars.by_ref() {
                    if c == '"' {
                        break;
                    }
                    label.push(c);
                }
                labels.push(label);
            }
        }
        labels
    }

    #[test]
    fn dag_is_deterministic_and_styles_ready_vs_blocked() {
        // cap-1 blocks cap-2; cap-2 blocks cap-3. Ready: cap-1. Blocked:
        // cap-2, cap-3.
        let edges = vec![
            edge(RelationKind::Blocks, "cap-1", "cap-2"),
            edge(RelationKind::Blocks, "cap-2", "cap-3"),
        ];
        let out = render_dag(&edges, &BTreeSet::new());
        let twice = render_dag(&edges, &BTreeSet::new());
        assert_eq!(out, twice, "two calls are byte-identical");
        assert!(out.starts_with("graph TD\n"), "dag header:\n{out}");
        assert!(out.contains("class cap_1 ready;"), "cap-1 ready:\n{out}");
        assert!(
            out.contains("classDef ready") && out.contains("classDef blocked"),
            "both styles present:\n{out}"
        );
        // Ready and blocked are DISTINCT classes.
        assert!(
            out.contains("class cap_2,cap_3 blocked;"),
            "cap-2/cap-3 blocked:\n{out}"
        );
    }

    #[test]
    fn dag_fails_closed_on_a_live_cycle_cycle_only_no_healthy_graph() {
        // cap-1 -> cap-2 -> cap-1 is a live blocks-cycle; cap-9 -> cap-8 is a
        // healthy edge that MUST NOT appear in the fail-closed render.
        let edges = vec![
            edge(RelationKind::Blocks, "cap-1", "cap-2"),
            edge(RelationKind::Blocks, "cap-2", "cap-1"),
            edge(RelationKind::Blocks, "cap-9", "cap-8"),
        ];
        let out = render_dag(&edges, &BTreeSet::new());
        assert!(
            out.contains("blocks-cycle fail-closed"),
            "banner present:\n{out}"
        );
        assert!(
            out.contains("cap_1") && out.contains("cap_2"),
            "cycle members:\n{out}"
        );
        // The healthy edge is ABSENT — no partial healthy graph.
        assert!(
            !out.contains("cap_9") && !out.contains("cap_8"),
            "healthy graph leaked into a fail-closed render:\n{out}"
        );
        assert!(out.contains("re-digest"), "digest-wording repair:\n{out}");
        assert_eq!(out, render_dag(&edges, &BTreeSet::new()), "deterministic");
    }

    #[test]
    fn dag_supersede_kills_a_cycle_the_forget_seam_analog() {
        // Same cycle, but cap-1 is superseded — dead to the projection, so
        // the cycle dissolves and a healthy graph returns.
        let edges = vec![
            edge(RelationKind::Blocks, "cap-1", "cap-2"),
            edge(RelationKind::Blocks, "cap-2", "cap-1"),
            edge(RelationKind::Supersedes, "cap-3", "cap-1"),
        ];
        let out = render_dag(&edges, &BTreeSet::new());
        assert!(
            !out.contains("blocks-cycle"),
            "supersede should dissolve the cycle:\n{out}"
        );
    }

    #[test]
    fn dag_styles_witnessed_nodes_as_done_and_frees_dependents() {
        // cap-1 blocks cap-2 (cap-1 ready, cap-2 blocked). Witness cap-1 (cap-3
        // attests it): cap-1 becomes DONE — distinct style — and cap-2 is freed
        // to ready. cap-3 merely witnesses, so it is not a blocks node.
        let edges = vec![
            edge(RelationKind::Blocks, "cap-1", "cap-2"),
            edge(RelationKind::Witnesses, "cap-3", "cap-1"),
        ];
        let out = render_dag(&edges, &BTreeSet::new());
        assert_eq!(out, render_dag(&edges, &BTreeSet::new()), "deterministic");
        assert!(out.contains("classDef done"), "done style present:\n{out}");
        assert!(
            out.contains("class cap_1 done;"),
            "cap-1 styled done:\n{out}"
        );
        assert!(
            out.contains("class cap_2 ready;"),
            "cap-2 freed to ready:\n{out}"
        );
        // Done is EXCLUSIVE — cap-1 is neither ready nor blocked.
        assert!(!out.contains("class cap_1 ready"), "done != ready:\n{out}");
        assert!(
            !out.contains("cap_3"),
            "the witness is not a dag node:\n{out}"
        );
    }

    #[test]
    fn dag_witness_dissolves_a_cycle_but_keeps_the_done_node() {
        // cap-1 <-> cap-2 is a live cycle; witnessing cap-1 dissolves it (like
        // supersede) — but, UNLIKE supersede, cap-1 stays drawn, styled done.
        let edges = vec![
            edge(RelationKind::Blocks, "cap-1", "cap-2"),
            edge(RelationKind::Blocks, "cap-2", "cap-1"),
            edge(RelationKind::Witnesses, "cap-3", "cap-1"),
        ];
        let out = render_dag(&edges, &BTreeSet::new());
        assert!(
            !out.contains("blocks-cycle"),
            "witness dissolves the cycle:\n{out}"
        );
        assert!(
            out.contains("class cap_1 done;"),
            "cap-1 kept and styled done:\n{out}"
        );
    }

    #[test]
    fn relations_orders_edges_like_the_export_relations_section() {
        // Kinds out of order on input; the render must sort by kind rank
        // (supersedes, derived_from, witnesses, blocks), then from, then to.
        let edges = vec![
            edge(RelationKind::Blocks, "cap-2", "cap-10"),
            edge(RelationKind::Supersedes, "cap-5", "cap-4"),
            edge(RelationKind::Blocks, "cap-2", "cap-3"),
        ];
        let out = render_relations(&edges);
        let sup = out.find("|supersedes|").expect("supersedes edge");
        let first_blocks = out.find("|blocks|").expect("blocks edge");
        assert!(sup < first_blocks, "supersedes sorts before blocks:\n{out}");
        // Numeric-aware: cap-3 (2->3) before cap-10 (2->10).
        let to3 = out.find("cap_2 -->|blocks| cap_3").expect("2->3");
        let to10 = out.find("cap_2 -->|blocks| cap_10").expect("2->10");
        assert!(to3 < to10, "cap-3 before cap-10 (numeric):\n{out}");
    }

    #[test]
    fn tiers_groups_by_effective_tier_in_fixed_order() {
        let rows = vec![
            row("cap-3", Tier::Quarantined, "tainted import"),
            row("cap-1", Tier::Active, "live fact"),
            row("cap-2", Tier::Archived, "aged decision"),
        ];
        let out = render_tiers(&rows);
        let a = out.find("tier_active").expect("active subgraph");
        let r = out.find("tier_archived").expect("archived subgraph");
        let q = out.find("tier_quarantined").expect("quarantined subgraph");
        assert!(
            a < r && r < q,
            "fixed tier order active<archived<quarantined:\n{out}"
        );
        assert!(out.contains("flowchart TB\n"), "tiers header:\n{out}");
        assert_eq!(out, render_tiers(&rows), "deterministic");
    }

    #[test]
    fn empty_projections_still_return_a_valid_single_node_diagram() {
        let empty: Vec<RelationRecord> = Vec::new();
        assert!(render_dag(&empty, &BTreeSet::new()).contains("empty[\""));
        assert!(render_relations(&empty).contains("empty[\""));
        assert!(render_tiers(&[]).contains("empty[\""));
    }

    #[test]
    fn provenance_line_pins_the_body_and_carries_no_clock() {
        let edges = vec![edge(RelationKind::Blocks, "cap-1", "cap-2")];
        let out = render_dag(&edges, &BTreeSet::new());
        assert!(
            out.contains("body sha256:"),
            "provenance sha present:\n{out}"
        );
        // The sha is over the body; recompute it and confirm the pin.
        let (_, body) = out.split_once("body sha256:").expect("has sha line");
        let sha = body.lines().next().expect("sha value").trim();
        let body_text = out
            .splitn(3, '\n')
            .nth(2)
            .expect("body after header+provenance")
            .trim_end_matches('\n');
        assert_eq!(
            sha256_hex(body_text.as_bytes()),
            sha,
            "body sha256 pins the body"
        );
    }

    #[test]
    fn unknown_view_is_a_closed_enum_rejection() {
        let ok: Result<VisualParams, _> =
            serde_json::from_value(serde_json::json!({"view": "dag"}));
        assert!(ok.is_ok(), "known view deserializes");
        let bad: Result<VisualParams, _> =
            serde_json::from_value(serde_json::json!({"view": "bogus"}));
        assert!(bad.is_err(), "unknown view is rejected by the closed enum");
        let extra: Result<VisualParams, _> =
            serde_json::from_value(serde_json::json!({"view": "dag", "nope": 1}));
        assert!(extra.is_err(), "deny_unknown_fields rejects stray keys");
    }

    #[test]
    fn relations_renders_all_five_kinds_in_contract_rank_order() {
        // The existing ordering test exercises only supersedes + blocks; the
        // other three kind_rank arms (derived_from=1, witnesses=2, falsifies=4)
        // never rendered. One edge of EACH kind, shuffled on input, must come
        // back in export rank order:
        // supersedes < derived_from < witnesses < blocks < falsifies.
        let edges = vec![
            edge(RelationKind::Falsifies, "cap-9", "cap-10"),
            edge(RelationKind::Blocks, "cap-7", "cap-8"),
            edge(RelationKind::Witnesses, "cap-5", "cap-6"),
            edge(RelationKind::DerivedFrom, "cap-3", "cap-4"),
            edge(RelationKind::Supersedes, "cap-1", "cap-2"),
        ];
        let out = render_relations(&edges);
        let s = out.find("|supersedes|").expect("supersedes edge renders");
        let d = out
            .find("|derived_from|")
            .expect("derived_from edge renders");
        let w = out.find("|witnesses|").expect("witnesses edge renders");
        let b = out.find("|blocks|").expect("blocks edge renders");
        let f = out.find("|falsifies|").expect("falsifies edge renders");
        assert!(
            s < d && d < w && w < b && b < f,
            "kinds must render in contract-rank order:\n{out}"
        );
        assert_eq!(out.matches("-->|").count(), 5, "one arrow per kind:\n{out}");
        assert_eq!(out, render_relations(&edges), "deterministic");
    }

    #[test]
    fn relations_sorts_non_capsule_ids_after_numeric_capsule_ids() {
        // sort_key is numeric-aware for `cap-<n>` and falls non-conforming ids
        // to u64::MAX — they sort AFTER every cap-id. A `falsifies` from_id may
        // be an outcome id (`out-<n>`, a non-conforming id), so this ordering
        // is a real wire shape, not a hypothetical.
        let edges = vec![
            edge(RelationKind::Falsifies, "out-1", "cap-2"),
            edge(RelationKind::Blocks, "cap-1", "cap-2"),
        ];
        let out = render_relations(&edges);
        let cap1 = out.find("cap_1[").expect("cap-1 node declared");
        let cap2 = out.find("cap_2[").expect("cap-2 node declared");
        let out1 = out.find("out_1[").expect("out-1 node declared");
        assert!(
            cap1 < out1 && cap2 < out1,
            "numeric cap-ids sort before the non-conforming out-1:\n{out}"
        );
    }

    #[test]
    fn dag_cycle_render_stays_fail_closed_on_a_degenerate_empty_cycle() {
        // render_dag_cycle guards the empty-cycle case; production always hands
        // it a concrete non-empty cycle (DagCycleError never carries an empty
        // one), but the guard must still hold fail-closed if reached: the
        // banner renders, NO ring edges are drawn, and no healthy graph leaks.
        let empty: Vec<String> = Vec::new();
        let out = render_dag_cycle(&empty, 3);
        assert!(out.starts_with("graph TD\n"), "dag header:\n{out}");
        assert!(
            out.contains("blocks-cycle fail-closed: cycle "),
            "fail-closed banner with the degenerate path:\n{out}"
        );
        assert!(
            out.contains("3 entangled"),
            "entangled total carried:\n{out}"
        );
        assert!(out.contains("cycle=0 entangled=3"), "counts:\n{out}");
        assert!(
            !out.contains("-->"),
            "no ring edges on an empty cycle:\n{out}"
        );
        assert!(
            out.contains("class cycle_banner banner;"),
            "banner still styled:\n{out}"
        );
        assert_eq!(out, render_dag_cycle(&empty, 3), "deterministic");
    }

    #[test]
    fn dag_with_every_participant_witnessed_renders_all_done_and_nothing_ready() {
        // cap-1 blocks cap-2, and BOTH are witnessed → both DONE. The graph has
        // closed with proof: `ready` and `blocked` are empty, yet the done
        // nodes stay drawn (a done node keeps its context, unlike a dead one).
        // Exercises the ready-empty / blocked-empty render branches every other
        // dag fixture — each carrying at least one ready node — skips.
        let edges = vec![
            edge(RelationKind::Blocks, "cap-1", "cap-2"),
            edge(RelationKind::Witnesses, "cap-3", "cap-1"),
            edge(RelationKind::Witnesses, "cap-4", "cap-2"),
        ];
        let out = render_dag(&edges, &BTreeSet::new());
        assert!(
            out.contains("class cap_1,cap_2 done;"),
            "both nodes styled done:\n{out}"
        );
        // No ready/blocked class-application line — the whole graph is closed.
        // (`classDef ready ...` is always declared, but no `class ... ready;`.)
        assert!(!out.contains("ready;"), "nothing ready:\n{out}");
        assert!(!out.contains("blocked;"), "nothing blocked:\n{out}");
        // The blocks edge survives: a done endpoint stays live, so its context
        // is kept (nodes=2 edges=1).
        assert!(
            out.contains("cap_1 --> cap_2"),
            "edge kept for context:\n{out}"
        );
        assert!(out.contains("nodes=2 edges=1"), "counts:\n{out}");
        assert_eq!(out, render_dag(&edges, &BTreeSet::new()), "deterministic");
    }
}
