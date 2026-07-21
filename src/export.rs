//! # Export — markdown GENERATED VIEW of the store (the human window).
//!
//! Campaign W2 unit `w2-export` (CAMPAIGN.md W2: "export markdown view";
//! ARCHITECTURE §1 NEXT: "export as markdown generated view (human window)").
//! The renderer is PURE:
//!
//! - **No store handle, no clock, no randomness.** Callers pass the
//!   already-read rows plus the injected `generated_at`; same input, same
//!   bytes — the golden test pins the exact output. Input order does not
//!   matter: everything is re-sorted internally (projects lexicographic,
//!   kinds in the closed classification order, entries by append seq,
//!   relations by kind/endpoints, markers by id).
//! - **Generated, never authority.** The view restates stored DATA for a
//!   human auditor; it closes nothing. The header carries the law line
//!   ([`EXPORT_LAW_LINE`]) and a store digest line whose `body sha256` is
//!   the digest of every byte below the header — regenerating over the
//!   same store reproduces it, and any hand edit stops matching it.
//! - **Never a directive surface.** Every content byte renders inside a
//!   quoted, id-anchored bullet (a bullet prefix means block-level markdown
//!   in content cannot take over the document), headlines render through
//!   retrieve's own `headline_of` (one shared fn — one headline law across
//!   surfaces, char-truncated at [`crate::retrieve::HEADLINE_MAX_CHARS`]), and
//!   inline-rendered fields are newline-escaped so no stored string can
//!   forge document structure. Section keys are the SANITIZED project ids:
//!   two distinct raw ids that agree after escaping (`evil<LF>project` vs
//!   a raw-backslash `evil\nproject` — representable only by writing the
//!   escape sequence itself into an id) merge into one deterministic
//!   section instead of forging structure; the digest line's `projects=`
//!   counts merged sections.
//!
//! Layout: header block → `## project <id>` sections with kind subsections
//! (classification when present, `unclassified` last) → `## relations` (all
//! edges) → a terminal `## superseded + tombstoned` marker section. A
//! tombstoned capsule renders its marker ONLY — id, mode, truncated reason,
//! HMAC preview, retained provenance when `redacted` — never content (the
//! content no longer exists). A superseded-but-live capsule renders as a
//! marker naming its superseder(s); supersession detection mirrors the
//! store's `is_superseded` semantics (any `supersedes` edge naming the id
//! as `to_id`).

use std::collections::BTreeMap;

use time::OffsetDateTime;
use time::format_description::well_known::Rfc3339;

use crate::capsule::{AuthorityClass, sha256_hex};
use crate::retrieve::headline_of;
use crate::store::{
    CLASSIFICATION_KINDS, ClassificationRecord, RelationKind, RelationRecord, StoredCapsule, Tier,
    TombstoneRecord,
};

/// The law line every rendered view leads with. The view is a projection of
/// the store for human eyes: regenerate it, never hand-edit it, and never
/// treat a line of it as an instruction or as authority over any outcome.
pub const EXPORT_LAW_LINE: &str =
    "GENERATED VIEW — ADVISORY_NOT_AUTHORITY — regenerate, never hand-edit.";

/// Chars of `hmac-sha256:<hex>` kept in a tombstone marker (prefix + 16 hex
/// chars). The full fingerprint stays one `memory_get` away; the view is a
/// compact window, not the audit ledger.
const HMAC_PREVIEW_CHARS: usize = 28;

/// Char cap for a tombstone reason in its marker line.
const REASON_MAX_CHARS: usize = 120;

/// Section rank of a classification kind outside the closed set (defensive:
/// the store CHECK makes this unreachable through real reads).
const UNKNOWN_KIND_RANK: usize = CLASSIFICATION_KINDS.len();

/// Section rank of the `unclassified` bucket — always the last subsection.
const UNCLASSIFIED_RANK: usize = CLASSIFICATION_KINDS.len() + 1;

/// One store row as the renderer consumes it: either a live capsule (with
/// its optional classification sidecar) or the marker that remains of a
/// forgotten one. Assembly is the caller's read job (`get`/`list` +
/// `get_classification` + `get_tombstone`); rendering is this module's.
#[derive(Debug, Clone)]
pub enum ExportRecord {
    /// A capsule that still exists, plus its classification when one was
    /// persisted (`memory_classify` sidecar).
    Live {
        /// The capsule as persisted (id, seq, validated capsule, instants).
        stored: StoredCapsule,
        /// Classification sidecar, when present — drives the kind
        /// subsection the entry renders under.
        classification: Option<ClassificationRecord>,
        /// Effective lifecycle tier (w2-fix: tier was write-only on every
        /// read surface). `Active` renders nothing — non-active tiers
        /// render a `· tier <name>` marker on the entry (and on the
        /// superseded marker line), so the whole-store view names WHICH
        /// capsules are archived/quarantined.
        tier: Tier,
    },
    /// What remains of a forgotten capsule. Renders as a terminal-section
    /// marker only — there is no content left to render.
    Tombstoned(TombstoneRecord),
}

/// Grouping key for a kind subsection: closed-order rank + display name.
type KindKey = (usize, String);
/// One rendered entry row: append seq + id (sort keys) + the line itself.
type EntryRow = (i64, String, String);
/// project → kind subsection → sorted entry rows.
type ProjectBuckets = BTreeMap<String, BTreeMap<KindKey, Vec<EntryRow>>>;

/// Render the whole store view as markdown. Pure and total: no clock (the
/// caller injects `generated_at`), no randomness, no I/O — identical input
/// yields identical bytes, regardless of slice order.
///
/// q83: `stamp` controls the one non-store-derived header line. `true`
/// (the default caller path) emits `> generated_at: <now>`; `false` OMITS
/// it, so two regenerations of an unchanged store are BYTE-IDENTICAL — the
/// only churning line is gone, and the body (with its `body sha256`) never
/// depended on `now`. The memory-in-git caller passes `stamp:false` for a
/// stable diff.
#[must_use]
pub fn render_markdown(
    records: &[ExportRecord],
    relations: &[RelationRecord],
    generated_at: OffsetDateTime,
    stamp: bool,
) -> String {
    let superseders = superseders_by_target(relations);

    let mut projects: ProjectBuckets = BTreeMap::new();
    let mut markers: Vec<((u64, String), String)> = Vec::new();
    let mut superseded_count = 0usize;
    let mut tombstoned_count = 0usize;

    for record in records {
        match record {
            ExportRecord::Live {
                stored,
                classification,
                tier,
            } => {
                let id = stored.id.as_str();
                if let Some(by) = superseders.get(id) {
                    superseded_count += 1;
                    let line = format!(
                        "- {id} — superseded by {by} (project {project}){tier}",
                        id = sanitize_inline(id),
                        by = by.join(", "),
                        project = sanitize_inline(&stored.capsule.scope().project_id),
                        tier = tier_suffix(*tier),
                    );
                    markers.push((owned_id_key(id), line));
                } else {
                    projects
                        .entry(sanitize_inline(&stored.capsule.scope().project_id))
                        .or_default()
                        .entry(kind_section_key(classification.as_ref()))
                        .or_default()
                        .push((stored.seq, id.to_owned(), entry_line(stored, *tier)));
                }
            }
            ExportRecord::Tombstoned(tombstone) => {
                tombstoned_count += 1;
                markers.push((
                    owned_id_key(&tombstone.capsule_id),
                    tombstone_line(tombstone),
                ));
            }
        }
    }

    for kinds in projects.values_mut() {
        for rows in kinds.values_mut() {
            rows.sort_by(|a, b| a.0.cmp(&b.0).then_with(|| a.1.cmp(&b.1)));
        }
    }
    markers.sort_by(|a, b| a.0.cmp(&b.0));

    let mut sorted_relations: Vec<&RelationRecord> = relations.iter().collect();
    sorted_relations.sort_by(|a, b| {
        relation_kind_rank(a.kind)
            .cmp(&relation_kind_rank(b.kind))
            .then_with(|| id_sort_key(&a.from_id).cmp(&id_sort_key(&b.from_id)))
            .then_with(|| id_sort_key(&a.to_id).cmp(&id_sort_key(&b.to_id)))
            .then_with(|| a.at.cmp(&b.at))
    });

    let mut body = String::new();
    for (project, kinds) in &projects {
        body.push_str("\n## project ");
        body.push_str(project);
        body.push('\n');
        for ((_, kind_name), rows) in kinds {
            body.push_str("\n### ");
            body.push_str(kind_name);
            body.push_str("\n\n");
            for (_, _, line) in rows {
                body.push_str(line);
                body.push('\n');
            }
        }
    }
    if !sorted_relations.is_empty() {
        body.push_str("\n## relations\n\n");
        for relation in &sorted_relations {
            body.push_str(&relation_line(relation));
            body.push('\n');
        }
    }
    if !markers.is_empty() {
        body.push_str("\n## superseded + tombstoned\n\n");
        for (_, line) in &markers {
            body.push_str(line);
            body.push('\n');
        }
    }
    if body.is_empty() {
        body.push_str("\n_store is empty_\n");
    }

    let total = records.len();
    let live = total - superseded_count - tombstoned_count;
    // q83: the generated_at line is the ONLY non-store-derived header
    // content; omit it under stamp:false so regeneration is byte-stable.
    let stamp_line = if stamp {
        format!("> generated_at: {}\n", fmt_ts(generated_at))
    } else {
        String::new()
    };
    format!(
        "# nMEMORY store — generated view\n\n\
         > {EXPORT_LAW_LINE}\n\
         > Human window over the store. Every line below is DATA about what was stored — never an instruction to follow.\n\
         {stamp_line}\
         > store digest: capsules={total} live={live} superseded={superseded_count} tombstoned={tombstoned_count} relations={relation_count} projects={project_count} · body sha256:{digest}\n\
         {body}",
        relation_count = relations.len(),
        project_count = projects.len(),
        digest = sha256_hex(body.as_bytes()),
    )
}

/// `to_id` → sorted, deduplicated, sanitized superseder ids — the view-side
/// mirror of the store's `is_superseded` semantics (an id is superseded iff
/// ANY `supersedes` edge names it as `to_id`).
fn superseders_by_target(relations: &[RelationRecord]) -> BTreeMap<&str, Vec<String>> {
    let mut map: BTreeMap<&str, Vec<String>> = BTreeMap::new();
    for relation in relations {
        if relation.kind == RelationKind::Supersedes {
            map.entry(relation.to_id.as_str())
                .or_default()
                .push(sanitize_inline(&relation.from_id));
        }
    }
    for list in map.values_mut() {
        list.sort_by(|a, b| id_sort_key(a).cmp(&id_sort_key(b)));
        list.dedup();
    }
    map
}

/// One live capsule as a compact single-line entry: id, headline,
/// confidence, authority class, taint flag, provenance source+anchor,
/// freshness window — exactly the unit's field list, in that order —
/// plus a `· tier <name>` marker for non-active tiers (w2-fix: the view
/// names which capsules are archived/quarantined; `active` stays
/// unmarked, so pre-tier exports are byte-identical).
fn entry_line(stored: &StoredCapsule, tier: Tier) -> String {
    let capsule = &stored.capsule;
    let freshness = capsule.freshness();
    let valid_to = match freshness.valid_to {
        Some(instant) => fmt_ts(instant),
        None => "open".to_owned(),
    };
    format!(
        "- **{id}** \"{headline}\" · conf {conf:.2} · {authority} · taint:{taint} · {source} @ {anchor} · {from} → {to}{tier}",
        id = sanitize_inline(stored.id.as_str()),
        headline = sanitize_inline(&headline_of(capsule.content())),
        conf = capsule.confidence().value(),
        authority = authority_str(capsule.authority_class()),
        taint = if capsule.instruction_taint() {
            "yes"
        } else {
            "no"
        },
        source = sanitize_inline(&capsule.provenance().source),
        anchor = sanitize_inline(&capsule.provenance().anchor),
        from = fmt_ts(freshness.valid_from),
        to = valid_to,
        tier = tier_suffix(tier),
    )
}

/// The `· tier <name>` marker for non-active tiers; empty for `active`
/// (the default rule needs no ink, and pre-tier golden bytes hold).
fn tier_suffix(tier: Tier) -> String {
    match tier {
        Tier::Active => String::new(),
        Tier::Archived | Tier::Quarantined => format!(" · tier {tier}"),
    }
}

/// A tombstone as its terminal marker: id, mode, truncated reason, HMAC
/// preview, and — for `redacted` — the deliberately retained provenance.
/// Never content: none exists.
fn tombstone_line(tombstone: &TombstoneRecord) -> String {
    let mut line = format!(
        "- {id} — tombstoned ({mode}) · reason: \"{reason}\" · {hmac}",
        id = sanitize_inline(&tombstone.capsule_id),
        mode = tombstone.mode.as_str(),
        reason = truncate_chars(&sanitize_inline(&tombstone.reason), REASON_MAX_CHARS),
        hmac = truncate_chars(
            &sanitize_inline(&tombstone.content_hmac),
            HMAC_PREVIEW_CHARS
        ),
    );
    match (&tombstone.provenance_source, &tombstone.provenance_anchor) {
        (Some(source), Some(anchor)) => {
            line.push_str(&format!(
                " · was {source} @ {anchor}",
                source = sanitize_inline(source),
                anchor = sanitize_inline(anchor),
            ));
        }
        (Some(source), None) => {
            line.push_str(&format!(
                " · was {source}",
                source = sanitize_inline(source)
            ));
        }
        (None, Some(anchor)) => {
            line.push_str(&format!(
                " · was @ {anchor}",
                anchor = sanitize_inline(anchor)
            ));
        }
        (None, None) => {}
    }
    line
}

/// One relation edge as a directed arrow line.
fn relation_line(relation: &RelationRecord) -> String {
    format!(
        "- {from} --{kind}--> {to} · at {at}",
        from = sanitize_inline(&relation.from_id),
        kind = relation.kind.as_str(),
        to = sanitize_inline(&relation.to_id),
        at = fmt_ts(relation.at),
    )
}

/// Subsection key for a live entry: classified kinds keep the closed
/// [`CLASSIFICATION_KINDS`] order, an out-of-set kind (defensive) follows
/// them, and `unclassified` is always last.
fn kind_section_key(classification: Option<&ClassificationRecord>) -> KindKey {
    match classification {
        Some(record) => {
            let kind = sanitize_inline(&record.kind);
            match CLASSIFICATION_KINDS.iter().position(|known| *known == kind) {
                Some(rank) => (rank, kind),
                None => (UNKNOWN_KIND_RANK, kind),
            }
        }
        None => (UNCLASSIFIED_RANK, "unclassified".to_owned()),
    }
}

/// Contract-order rank of a relation kind (exhaustive: a new kind is a
/// compile error here, never a silent tail section).
const fn relation_kind_rank(kind: RelationKind) -> usize {
    match kind {
        RelationKind::Supersedes => 0,
        RelationKind::DerivedFrom => 1,
        RelationKind::Witnesses => 2,
        RelationKind::Blocks => 3,
        RelationKind::Falsifies => 4,
    }
}

/// Kebab wire name of an authority class — byte-identical to the Capsule
/// serde form (pinned by a parity test below).
const fn authority_str(class: AuthorityClass) -> &'static str {
    match class {
        AuthorityClass::ObservedFact => "observed-fact",
        AuthorityClass::UserStated => "user-stated",
        AuthorityClass::AgentInferred => "agent-inferred",
        AuthorityClass::ExternallyImported => "externally-imported",
    }
}

/// Numeric-aware sort key for `cap-<seq>` ids: `cap-2` before `cap-10`;
/// non-conforming ids sort after all conforming ones, lexicographically.
fn id_sort_key(id: &str) -> (u64, &str) {
    let numeric = id
        .strip_prefix("cap-")
        .and_then(|suffix| suffix.parse::<u64>().ok())
        .unwrap_or(u64::MAX);
    (numeric, id)
}

/// Owned form of [`id_sort_key`] for keys that must outlive their source.
fn owned_id_key(id: &str) -> (u64, String) {
    let (numeric, _) = id_sort_key(id);
    (numeric, id.to_owned())
}

/// Escape line terminators so no stored string can forge view structure
/// (headings, bullets, the digest line). Char-safe by construction.
fn sanitize_inline(text: &str) -> String {
    // Quotes are escaped so a stored first line can never CLOSE the
    // quoted headline early and forge trailing fields (conf/authority/
    // taint) on the audit line (w2 review).
    text.replace('\r', "\\r")
        .replace('\n', "\\n")
        .replace('"', "\\\"")
}

/// Char-boundary-safe truncation with `…` when anything was cut.
fn truncate_chars(text: &str, max_chars: usize) -> String {
    let truncated: String = text.chars().take(max_chars).collect();
    if text.chars().count() > max_chars {
        format!("{truncated}…")
    } else {
        truncated
    }
}

/// RFC3339 rendering of an injected instant; total (falls back to a fixed
/// marker for instants outside the representable year range).
fn fmt_ts(instant: OffsetDateTime) -> String {
    instant
        .format(&Rfc3339)
        .unwrap_or_else(|_| "unrepresentable-instant".to_owned())
}

#[cfg(test)]
mod tests {
    #![allow(
        clippy::unwrap_used,
        clippy::expect_used,
        reason = "tests use unwrap/expect so fixture failures fail at the assertion site"
    )]

    use super::*;
    use crate::capsule::{Capsule, Confidence, Freshness, Provenance, Scope};
    use crate::retrieve::HEADLINE_MAX_CHARS;
    use crate::store::Tier;
    use crate::store::{CapsuleId, TombstoneMode};
    use time::macros::datetime;

    const GENERATED_AT: OffsetDateTime = datetime!(2026-07-18 12:00:00 UTC);

    fn cap_id(id: &str) -> CapsuleId {
        // CapsuleId mints only in the store; tests obtain one through its
        // serde surface (transparent string), same as snapshot replay.
        serde_json::from_value(serde_json::Value::String(id.to_owned())).unwrap()
    }

    fn capsule(
        content: &str,
        project: &str,
        conf: f64,
        class: AuthorityClass,
        taint: bool,
        window: (OffsetDateTime, Option<OffsetDateTime>),
        prov: (&str, &str),
    ) -> Capsule {
        Capsule::new(
            content.to_owned(),
            Provenance {
                source: prov.0.to_owned(),
                anchor: prov.1.to_owned(),
                source_hash: sha256_hex(content.as_bytes()),
            },
            Confidence::new(conf).unwrap(),
            Freshness {
                valid_from: window.0,
                valid_to: window.1,
            },
            Scope {
                project_id: project.to_owned(),
            },
            class,
            taint,
        )
        .unwrap()
    }

    fn stored(id: &str, seq: i64, capsule: Capsule) -> StoredCapsule {
        StoredCapsule {
            id: cap_id(id),
            seq,
            capsule,
            created_at: datetime!(2026-07-18 00:00:00 UTC),
            session_id: None,
        }
    }

    fn live_unclassified(id: &str, seq: i64, content: &str, project: &str) -> ExportRecord {
        ExportRecord::Live {
            stored: stored(
                id,
                seq,
                capsule(
                    content,
                    project,
                    0.5,
                    AuthorityClass::AgentInferred,
                    false,
                    (datetime!(2026-07-18 00:00:00 UTC), None),
                    ("s", "a:1"),
                ),
            ),
            tier: Tier::Active,
            classification: None,
        }
    }

    fn edge(kind: RelationKind, from: &str, to: &str, at: OffsetDateTime) -> RelationRecord {
        RelationRecord {
            kind,
            from_id: from.to_owned(),
            to_id: to.to_owned(),
            at,
            origin: crate::store::RelationOrigin::Manual,
        }
    }

    /// Small corpus exercising: two projects, classified + unclassified
    /// entries, unicode content, taint, a bounded window, one superseded
    /// live capsule, one purged tombstone, all FOUR relation kinds in
    /// deliberately scrambled input order.
    fn golden_corpus() -> (Vec<ExportRecord>, Vec<RelationRecord>) {
        let records = vec![
            ExportRecord::Live {
                stored: stored(
                    "cap-4",
                    4,
                    capsule(
                        "Old tailnet rule.",
                        "nott",
                        0.5,
                        AuthorityClass::AgentInferred,
                        false,
                        (datetime!(2026-07-01 00:00:00 UTC), None),
                        ("session:2026-07-01", "notes.md:3"),
                    ),
                ),
                tier: Tier::Active,
                classification: None,
            },
            ExportRecord::Live {
                stored: stored(
                    "cap-1",
                    1,
                    capsule(
                        "Decisão: o zayout sobe sempre via tailnet.",
                        "nott",
                        0.9,
                        AuthorityClass::UserStated,
                        false,
                        (datetime!(2026-07-18 09:00:00 UTC), None),
                        ("session:2026-07-18", "CLAUDE.md:12"),
                    ),
                ),
                tier: Tier::Active,
                classification: Some(ClassificationRecord {
                    kind: "decision".to_owned(),
                    scope: "project".to_owned(),
                    at: datetime!(2026-07-18 09:30:00 UTC),
                }),
            },
            ExportRecord::Tombstoned(TombstoneRecord {
                capsule_id: "cap-5".to_owned(),
                mode: TombstoneMode::Purged,
                content_hmac: format!("hmac-sha256:{}", "aabbccddeeff0011".repeat(4)),
                at: datetime!(2026-07-18 11:00:00 UTC),
                reason: "owner request".to_owned(),
                provenance_source: None,
                provenance_anchor: None,
                source_hash: None,
            }),
            ExportRecord::Live {
                stored: stored(
                    "cap-2",
                    2,
                    capsule(
                        "Imported ops note\nsecond line detail.",
                        "nott",
                        0.4,
                        AuthorityClass::ExternallyImported,
                        true,
                        (
                            datetime!(2026-07-10 08:00:00 UTC),
                            Some(datetime!(2026-12-31 23:59:59 UTC)),
                        ),
                        ("import:claude-md", "CLAUDE.md:1"),
                    ),
                ),
                tier: Tier::Active,
                classification: None,
            },
            ExportRecord::Live {
                stored: stored(
                    "cap-3",
                    3,
                    capsule(
                        "SSOT decay lives in migration 0028.",
                        "alpha",
                        1.0,
                        AuthorityClass::ObservedFact,
                        false,
                        (datetime!(2026-06-01 00:00:00 UTC), None),
                        ("donor:ssot", "0028_memory_scoring.sql:1"),
                    ),
                ),
                tier: Tier::Active,
                classification: Some(ClassificationRecord {
                    kind: "fact".to_owned(),
                    scope: "global".to_owned(),
                    at: datetime!(2026-07-18 09:31:00 UTC),
                }),
            },
        ];
        let relations = vec![
            edge(
                RelationKind::Blocks,
                "cap-1",
                "cap-2",
                datetime!(2026-07-18 10:30:00 UTC),
            ),
            edge(
                RelationKind::Supersedes,
                "cap-2",
                "cap-4",
                datetime!(2026-07-18 10:00:00 UTC),
            ),
            edge(
                RelationKind::DerivedFrom,
                "cap-3",
                "cap-1",
                datetime!(2026-07-18 10:15:00 UTC),
            ),
            edge(
                RelationKind::Witnesses,
                "cap-3",
                "cap-1",
                datetime!(2026-07-18 10:20:00 UTC),
            ),
        ];
        (records, relations)
    }

    /// The exact body bytes the golden corpus must render to. The header's
    /// digest is computed FROM these bytes in the test, so any renderer
    /// drift breaks equality twice over.
    const GOLDEN_BODY: &str = concat!(
        "\n## project alpha\n",
        "\n### fact\n\n",
        "- **cap-3** \"SSOT decay lives in migration 0028.\" · conf 1.00 · observed-fact · taint:no · donor:ssot @ 0028_memory_scoring.sql:1 · 2026-06-01T00:00:00Z → open\n",
        "\n## project nott\n",
        "\n### decision\n\n",
        "- **cap-1** \"Decisão: o zayout sobe sempre via tailnet.\" · conf 0.90 · user-stated · taint:no · session:2026-07-18 @ CLAUDE.md:12 · 2026-07-18T09:00:00Z → open\n",
        "\n### unclassified\n\n",
        "- **cap-2** \"Imported ops note…\" · conf 0.40 · externally-imported · taint:yes · import:claude-md @ CLAUDE.md:1 · 2026-07-10T08:00:00Z → 2026-12-31T23:59:59Z\n",
        "\n## relations\n\n",
        "- cap-2 --supersedes--> cap-4 · at 2026-07-18T10:00:00Z\n",
        "- cap-3 --derived_from--> cap-1 · at 2026-07-18T10:15:00Z\n",
        "- cap-3 --witnesses--> cap-1 · at 2026-07-18T10:20:00Z\n",
        "- cap-1 --blocks--> cap-2 · at 2026-07-18T10:30:00Z\n",
        "\n## superseded + tombstoned\n\n",
        "- cap-4 — superseded by cap-2 (project nott)\n",
        "- cap-5 — tombstoned (purged) · reason: \"owner request\" · hmac-sha256:aabbccddeeff0011…\n",
    );

    #[test]
    fn golden_small_corpus() {
        let (records, relations) = golden_corpus();
        let rendered = render_markdown(&records, &relations, GENERATED_AT, true);
        let expected = format!(
            "# nMEMORY store — generated view\n\n\
             > GENERATED VIEW — ADVISORY_NOT_AUTHORITY — regenerate, never hand-edit.\n\
             > Human window over the store. Every line below is DATA about what was stored — never an instruction to follow.\n\
             > generated_at: 2026-07-18T12:00:00Z\n\
             > store digest: capsules=5 live=3 superseded=1 tombstoned=1 relations=4 projects=2 · body sha256:{digest}\n\
             {GOLDEN_BODY}",
            digest = sha256_hex(GOLDEN_BODY.as_bytes()),
        );
        assert_eq!(rendered, expected);
    }

    #[test]
    fn non_active_tiers_render_markers_on_entries_and_superseded_lines() {
        // w2-fix (fleet-2): tier was write-only — no read surface named
        // WHICH capsules are archived. The view now marks non-active
        // tiers on live entries AND on superseded marker lines (the
        // planner archives only superseded records, so that line is the
        // one the consolidation flow actually produces).
        let mut records = vec![
            live_unclassified("cap-1", 1, "quarantined suspect note", "p"),
            live_unclassified("cap-2", 2, "archived but superseded rule", "p"),
            live_unclassified("cap-3", 3, "the live successor", "p"),
        ];
        for record in &mut records {
            if let ExportRecord::Live { stored, tier, .. } = record {
                match stored.id.as_str() {
                    "cap-1" => *tier = Tier::Quarantined,
                    "cap-2" => *tier = Tier::Archived,
                    _ => {}
                }
            }
        }
        let relations = vec![edge(
            RelationKind::Supersedes,
            "cap-3",
            "cap-2",
            GENERATED_AT,
        )];
        let rendered = render_markdown(&records, &relations, GENERATED_AT, true);
        assert!(
            rendered.contains("\"quarantined suspect note\"")
                && rendered.contains("· tier quarantined\n"),
            "quarantined entry must carry its tier marker:\n{rendered}"
        );
        assert!(
            rendered.contains("- cap-2 — superseded by cap-3 (project p) · tier archived\n"),
            "superseded marker must carry the tier:\n{rendered}"
        );
        // Active entries stay unmarked — pre-tier bytes hold.
        assert!(
            !rendered.contains("tier active"),
            "active is the unmarked default:\n{rendered}"
        );
    }

    #[test]
    fn deterministic_and_input_order_independent() {
        let (records, relations) = golden_corpus();
        let first = render_markdown(&records, &relations, GENERATED_AT, true);
        let second = render_markdown(&records, &relations, GENERATED_AT, true);
        assert_eq!(first.as_bytes(), second.as_bytes());

        let mut reversed_records = records;
        reversed_records.reverse();
        let mut reversed_relations = relations;
        reversed_relations.reverse();
        let third = render_markdown(&reversed_records, &reversed_relations, GENERATED_AT, true);
        assert_eq!(first.as_bytes(), third.as_bytes());
    }

    #[test]
    fn stamp_false_regenerations_are_byte_stable_across_time() {
        // q83: the generated_at line is the ONLY thing that churns between
        // regenerations. Under stamp:false it is omitted, so two exports of
        // the SAME store at DIFFERENT instants are byte-identical — the
        // 1-line diff the memory-in-git caller kept hitting is gone.
        let (records, relations) = golden_corpus();
        let t1 = datetime!(2026-07-18 12:00:00 UTC);
        let t2 = datetime!(2026-09-30 23:59:59 UTC);
        let a = render_markdown(&records, &relations, t1, false);
        let b = render_markdown(&records, &relations, t2, false);
        assert_eq!(
            a.as_bytes(),
            b.as_bytes(),
            "stamp:false must be time-invariant"
        );
        // The stamp line is gone, but the store-digest sha line remains.
        assert!(
            !a.contains("generated_at:"),
            "stamp:false omits generated_at:\n{a}"
        );
        assert!(a.contains("body sha256:"), "store digest stays:\n{a}");
        // stamp:true still stamps (default caller path unchanged).
        let stamped = render_markdown(&records, &relations, t1, true);
        assert!(stamped.contains("> generated_at: 2026-07-18T12:00:00Z\n"));
    }

    #[test]
    fn tombstoned_renders_marker_only() {
        let records = vec![
            live_unclassified("cap-1", 1, "still alive", "p"),
            ExportRecord::Tombstoned(TombstoneRecord {
                capsule_id: "cap-9".to_owned(),
                mode: TombstoneMode::Purged,
                content_hmac: "hmac-sha256:aa".to_owned(),
                at: datetime!(2026-07-18 11:00:00 UTC),
                reason: "gone".to_owned(),
                provenance_source: None,
                provenance_anchor: None,
                source_hash: None,
            }),
            ExportRecord::Tombstoned(TombstoneRecord {
                capsule_id: "cap-10".to_owned(),
                mode: TombstoneMode::Redacted,
                content_hmac: "hmac-sha256:bb".to_owned(),
                at: datetime!(2026-07-18 11:05:00 UTC),
                reason: "pii".to_owned(),
                provenance_source: Some("scratch.md".to_owned()),
                provenance_anchor: Some("scratch.md:7".to_owned()),
                source_hash: None,
            }),
        ];
        let out = render_markdown(&records, &[], GENERATED_AT, true);

        // The tombstoned id appears exactly once — in the terminal marker
        // section, after the live project sections.
        assert_eq!(out.matches("cap-9").count(), 1);
        let markers_at = out.find("## superseded + tombstoned").unwrap();
        assert!(out.find("cap-9").unwrap() > markers_at);
        assert!(out.find("**cap-1**").unwrap() < markers_at);

        // Purged: no retained provenance. Redacted: provenance shown.
        assert!(
            out.contains("- cap-9 — tombstoned (purged) · reason: \"gone\" · hmac-sha256:aa\n")
        );
        assert!(out.contains(
            "- cap-10 — tombstoned (redacted) · reason: \"pii\" · hmac-sha256:bb · was scratch.md @ scratch.md:7\n"
        ));

        // Numeric id ordering inside the marker section: cap-9 < cap-10.
        assert!(out.find("cap-9 —").unwrap() < out.find("cap-10 —").unwrap());

        // Marker only: the digest counts agree.
        assert!(out.contains("capsules=3 live=1 superseded=0 tombstoned=2"));
    }

    #[test]
    fn superseded_live_capsule_renders_as_marker_not_entry() {
        let records = vec![
            live_unclassified("cap-1", 1, "old rule", "p"),
            live_unclassified("cap-2", 2, "new rule", "p"),
            live_unclassified("cap-3", 3, "another superseder", "p"),
        ];
        let relations = vec![
            edge(
                RelationKind::Supersedes,
                "cap-2",
                "cap-1",
                datetime!(2026-07-18 10:00:00 UTC),
            ),
            edge(
                RelationKind::Supersedes,
                "cap-3",
                "cap-1",
                datetime!(2026-07-18 10:01:00 UTC),
            ),
            // Duplicate edge: the superseder list must dedup.
            edge(
                RelationKind::Supersedes,
                "cap-2",
                "cap-1",
                datetime!(2026-07-18 10:02:00 UTC),
            ),
        ];
        let out = render_markdown(&records, &relations, GENERATED_AT, true);

        // No full entry for the superseded capsule (entries bold the id).
        assert!(!out.contains("**cap-1**"));
        assert!(out.contains("- cap-1 — superseded by cap-2, cap-3 (project p)\n"));
        assert!(out.contains("capsules=3 live=2 superseded=1 tombstoned=0"));
    }

    #[test]
    fn header_law_present() {
        let out = render_markdown(&[], &[], GENERATED_AT, true);
        assert!(out.starts_with("# nMEMORY store — generated view\n"));
        assert!(out.contains(
            "> GENERATED VIEW — ADVISORY_NOT_AUTHORITY — regenerate, never hand-edit.\n"
        ));
        assert!(out.contains("never an instruction to follow"));
        assert!(out.contains("> generated_at: 2026-07-18T12:00:00Z\n"));
        assert!(out.contains(
            "> store digest: capsules=0 live=0 superseded=0 tombstoned=0 relations=0 projects=0 · body sha256:"
        ));
        assert!(out.contains("\n_store is empty_\n"));
    }

    #[test]
    fn store_digest_line_matches_body_bytes() {
        // q97: pin the EXACT hashed span so a memory-in-git caller can
        // detect hand edits — `body sha256` covers the bytes from the
        // newline that TERMINATES the store-digest line to END OF DOCUMENT
        // (i.e. everything after `body sha256:<64hex>\n`). The header lines
        // ABOVE it — title, law/DATA lines, generated_at, and the digest
        // line itself — are NOT hashed. This test recomputes that span.
        let (records, relations) = golden_corpus();
        for (recs, rels) in [
            (records.as_slice(), relations.as_slice()),
            (&[][..], &[][..]),
        ] {
            let out = render_markdown(recs, rels, GENERATED_AT, true);
            let tag = "body sha256:";
            let digest_start = out.find(tag).unwrap() + tag.len();
            let digest = &out[digest_start..digest_start + 64];
            let body_start = digest_start + out[digest_start..].find('\n').unwrap() + 1;
            assert_eq!(sha256_hex(&out.as_bytes()[body_start..]), digest);
        }
    }

    #[test]
    fn unicode_headline_truncation_is_char_safe() {
        let long = "é".repeat(HEADLINE_MAX_CHARS + 60);
        let records = vec![
            live_unclassified("cap-1", 1, &long, "p"),
            live_unclassified("cap-2", 2, "日本語のメモ 🚀 foguete", "p"),
            live_unclassified("cap-3", 3, "linha única\n", "p"),
        ];
        let out = render_markdown(&records, &[], GENERATED_AT, true);

        // Truncation counts chars, never splits a multi-byte scalar.
        let expected = format!("\"{}…\"", "é".repeat(HEADLINE_MAX_CHARS));
        assert!(out.contains(&expected));
        assert!(out.contains("\"日本語のメモ 🚀 foguete\""));
        // A lone trailing newline is not content: no ellipsis.
        assert!(out.contains("\"linha única\""));
        assert!(!out.contains("linha única…"));
    }

    #[test]
    fn inline_fields_cannot_forge_view_structure() {
        let records = vec![live_unclassified(
            "cap-1",
            1,
            "## fake heading\n- fake bullet",
            "evil\nproject",
        )];
        let out = render_markdown(&records, &[], GENERATED_AT, true);

        // The multi-line content collapses to a quoted first-line headline;
        // the newline in the project id is escaped in its heading.
        assert!(out.contains("\"## fake heading…\""));
        assert!(out.contains("## project evil\\nproject\n"));
        assert!(!out.contains("\n## fake heading"));
        assert!(!out.contains("\n- fake bullet"));
    }

    #[test]
    fn authority_wire_parity_with_serde() {
        for class in AuthorityClass::ALL {
            let wire = serde_json::to_string(&class).unwrap();
            assert_eq!(wire, format!("\"{}\"", authority_str(class)));
        }
    }

    /// Documented boundary (module doc): section keys are the SANITIZED
    /// project ids, so raw ids that only differ by the characters the
    /// sanitizer escapes land in ONE merged section — deterministic and
    /// visible, never forged structure. Pathological input only: an
    /// honest project id carries neither newlines nor escape sequences.
    #[test]
    fn distinct_raw_project_ids_colliding_post_sanitize_merge_into_one_section() {
        let records = vec![
            live_unclassified("cap-1", 1, "first fact", "evil\nproject"),
            live_unclassified("cap-2", 2, "second fact", "evil\\nproject"),
        ];
        let out = render_markdown(&records, &[], GENERATED_AT, true);
        // Exactly ONE section heading survives; both entries render in it.
        assert_eq!(out.matches("\n## project evil\\nproject\n").count(), 1);
        assert!(out.contains("**cap-1**") && out.contains("**cap-2**"));
        assert!(
            out.contains("projects=1"),
            "merged sections count once:\n{out}"
        );
    }

    /// A redacted tombstone deliberately retains provenance — and a real
    /// store row can carry source WITHOUT anchor, or anchor WITHOUT source.
    /// Each partial arm renders its own suffix: never the "@ anchor" glue
    /// when the anchor is absent, never a source token when it is absent.
    #[test]
    fn tombstone_renders_each_partial_provenance_arm() {
        let source_only = ExportRecord::Tombstoned(TombstoneRecord {
            capsule_id: "cap-1".to_owned(),
            mode: TombstoneMode::Redacted,
            content_hmac: "hmac-sha256:aa".to_owned(),
            at: datetime!(2026-07-18 11:00:00 UTC),
            reason: "pii".to_owned(),
            provenance_source: Some("scratch.md".to_owned()),
            provenance_anchor: None,
            source_hash: None,
        });
        let anchor_only = ExportRecord::Tombstoned(TombstoneRecord {
            capsule_id: "cap-2".to_owned(),
            mode: TombstoneMode::Redacted,
            content_hmac: "hmac-sha256:bb".to_owned(),
            at: datetime!(2026-07-18 11:01:00 UTC),
            reason: "pii".to_owned(),
            provenance_source: None,
            provenance_anchor: Some("scratch.md:7".to_owned()),
            source_hash: None,
        });
        let out = render_markdown(&[source_only, anchor_only], &[], GENERATED_AT, true);
        // Source-only: "· was <source>" with NO "@" glue.
        assert!(
            out.contains(
                "- cap-1 — tombstoned (redacted) · reason: \"pii\" · hmac-sha256:aa · was scratch.md\n"
            ),
            "source-only provenance arm:\n{out}"
        );
        // Anchor-only: "· was @ <anchor>" with NO source token.
        assert!(
            out.contains(
                "- cap-2 — tombstoned (redacted) · reason: \"pii\" · hmac-sha256:bb · was @ scratch.md:7\n"
            ),
            "anchor-only provenance arm:\n{out}"
        );
    }

    /// Defensive rank (`UNKNOWN_KIND_RANK`): the store CHECK keeps kinds in
    /// the closed set, but the renderer must degrade gracefully if a row ever
    /// carries an out-of-set kind — its own section, AFTER every known kind
    /// and BEFORE `unclassified`, never a panic or a silent merge into a
    /// known section.
    #[test]
    fn out_of_set_classification_kind_sorts_into_its_own_trailing_section() {
        let classified = |id: &str, seq: i64, kind: &str| ExportRecord::Live {
            stored: stored(
                id,
                seq,
                capsule(
                    "some claim",
                    "p",
                    0.5,
                    AuthorityClass::AgentInferred,
                    false,
                    (datetime!(2026-07-18 00:00:00 UTC), None),
                    ("s", "a:1"),
                ),
            ),
            tier: Tier::Active,
            classification: Some(ClassificationRecord {
                kind: kind.to_owned(),
                scope: "project".to_owned(),
                at: datetime!(2026-07-18 09:00:00 UTC),
            }),
        };
        let records = vec![
            classified("cap-1", 1, "fact"),
            classified("cap-2", 2, "mystery-kind"),
            live_unclassified("cap-3", 3, "loose note", "p"),
        ];
        let out = render_markdown(&records, &[], GENERATED_AT, true);
        let fact_at = out.find("### fact").expect("known kind section");
        let unknown_at = out.find("### mystery-kind").expect("unknown kind section");
        let unclassified_at = out.find("### unclassified").expect("unclassified section");
        assert!(
            fact_at < unknown_at && unknown_at < unclassified_at,
            "unknown kind ranks after known, before unclassified:\n{out}"
        );
    }

    /// The fifth relation kind (`Falsifies`) had no render coverage: it must
    /// emit its wire arrow and, by contract rank (Supersedes=0 … Falsifies=4),
    /// sort LAST among mixed edges even when handed first.
    #[test]
    fn falsifies_relation_renders_and_sorts_after_supersedes() {
        let records = vec![
            live_unclassified("cap-1", 1, "claim under test", "p"),
            live_unclassified("cap-2", 2, "the refuting evidence", "p"),
        ];
        let relations = vec![
            // Falsifies handed FIRST; must still sort after supersedes.
            edge(
                RelationKind::Falsifies,
                "cap-2",
                "cap-1",
                datetime!(2026-07-18 10:00:00 UTC),
            ),
            // Supersedes between ids that are NOT live records here, so no
            // live entry is turned into a superseded marker.
            edge(
                RelationKind::Supersedes,
                "cap-8",
                "cap-9",
                datetime!(2026-07-18 10:05:00 UTC),
            ),
        ];
        let out = render_markdown(&records, &relations, GENERATED_AT, true);
        let sup_at = out
            .find("--supersedes-->")
            .expect("supersedes edge renders");
        let fal_at = out.find("--falsifies-->").expect("falsifies edge renders");
        assert!(
            sup_at < fal_at,
            "falsifies (rank 4) sorts after supersedes (rank 0):\n{out}"
        );
        assert!(
            out.contains("- cap-2 --falsifies--> cap-1 · at 2026-07-18T10:00:00Z\n"),
            "falsifies wire arrow:\n{out}"
        );
    }
}
