//! # Extract — deterministic, non-LLM candidate extraction (campaign W1, `.2` §6 u6b).
//!
//! Turns one free-text blob into `0..N` typed [`ExtractCandidate`]s — the
//! metabolize step that feeds [`crate::classify`]. Pure engine module: no
//! store dependency, no clock, no randomness, no network, no LLM. The same
//! blob always yields the same candidates in the same order (structurally
//! guaranteed: this module is a pure function of its `&str` input, iterating
//! only `const` rule tables in declaration order and segments in source
//! order — no map iteration anywhere).
//!
//! ## The donor delta (documented, not hidden)
//!
//! Donor B's extract (`mcps/memory/src/lifecycle/extract.rs` @6d495898, zero
//! authority) derives candidates from STRUCTURED episodes
//! (`tool_calls[].file_path` → fact, multi-step tool sequences → procedure,
//! `outcome == "error"` → decision). nMEMORY ingests free text, so those
//! structural heuristics do not transfer; what transfers is their LAW, kept
//! here by construction:
//!
//! - **Never fabricate.** Every candidate's `content` is a verbatim
//!   substring of the source blob (whitespace-trimmed, list/heading markers
//!   stripped — stripping only ever REMOVES leading formatting; it never
//!   synthesizes, joins, or rewords a byte).
//! - **Closed kinds.** Every candidate is exactly one [`CandidateKind`]
//!   (`fact` | `procedure` | `decision` | `task` | `epic` | `brainstorm` |
//!   `doc` | `constraint` | `capability` | `failure_pattern`) before it
//!   reaches classify — the donor's closed W1 triple, wire names carried
//!   verbatim, plus the W2 work-plane kinds (CAMPAIGN rung: the SSOT
//!   work/docs plane returns as capsule KINDS), plus the u-r11 governance
//!   kinds (PRD R11: prohibition / applicability / failure-shape records;
//!   `proof` and `outcome` are DELIBERATE non-kinds — witnesses edges +
//!   provenance already are the proof, and outcomes are the `out-<n>`
//!   record class). Adding a kind is a deliberate, reviewed ontology
//!   change, never an incidental one.
//! - **`0..N` honest.** A blob with nothing extractable yields an empty
//!   `Vec`, never an invented candidate; empty input yields empty output.
//! - **Over-generate, never over-claim.** `kind` is a deterministic HINT and
//!   every candidate names the literal [`ExtractCandidate::cue`] that fired.
//!   False positives are tolerable — the intelligent caller and
//!   [`crate::classify`] refine (LLM-first law 2) — fabricated content is
//!   not.
//! - **Produce-only.** Extracting the same blob twice yields the same
//!   candidates twice; dedup/merge across calls stays the consolidate
//!   lane's job (donor parity: "extract does not dedup").
//!
//! ## Segmentation rules (S1–S4)
//!
//! - **S1 — segment = sentence within line.** The candidate unit is one
//!   line of the blob, split further at conservative sentence boundaries
//!   (S1b, w1d): `.`/`!`/`?` + space + an uppercase opener, with dotted
//!   and single-char words before the boundary treated as abbreviations
//!   (never split). **S1c (w2-fix)**: `; ` splits unconditionally — a
//!   rule-dense bullet joins independent rules with semicolons ("rmcp
//!   races frames; serialize one request in flight") and each clause is
//!   its own capsule-sized claim. Every piece stays a verbatim substring
//!   — a prose paragraph no longer collapses into one four-sentence
//!   "decision".
//! - **S2 — fenced code is skipped.** Lines between ``` fences carry code,
//!   not claims; extracting `let x = 5;` as a "fact" is noise by
//!   construction. An unterminated fence skips through end-of-blob
//!   (deterministic; the honest reading of a half-open fence). **S2b
//!   (w1d)**: markdown INDENTED code lines (4+ spaces / tab, not list
//!   dress) are skipped the same way — pasted tracebacks are not claims.
//! - **S3 — markers are stripped.** Leading markdown formatting — heading
//!   hashes, blockquote `>`, bullets (`- `/`* `/`+ `), checkboxes
//!   (`[ ] `/`[x] `), ordered markers (`1. `/`1) `) — is peeled before
//!   analysis, so the candidate content is the claim, not its list dress.
//!   A bare ordered marker is deliberately NOT procedure evidence on its
//!   own (documented improvement over a naive step rule: numbered lists
//!   enumerate facts just as often as steps — the BODY decides the kind).
//!   **S3b (w2-kinds)**: an UNCHECKED checkbox `[ ] ` is the one marker
//!   that is not neutral dress — it is the author literally flagging open
//!   work, recorded as rule T1 evidence while still being stripped from
//!   the content. A checked `[x] `/`[X] ` box is done work and stays
//!   neutral (the body decides the kind). **S3c (w2-fix) — chat/log
//!   dress**: a leading bracketed stamp carrying a digit (`[10:05] `,
//!   `[2026-07-18] `) is neutral dress, and AFTER one was stripped, ONE
//!   leading speaker tag (`tiago: `) is too — unless the tag word is
//!   itself a cue/label word (`todo:`, `doc:`, `marco:` … stay content).
//!   This is what lets the start-anchored work-plane labels fire on
//!   realistic chat-prefixed session-log lines; the label rules
//!   themselves stay start-anchored (authored evidence must OPEN the
//!   segment once dress is peeled).
//! - **S4 — no-claim segments are skipped.** Fewer than two words carries
//!   no claim; a segment ending in `?` is a question, not a claim — neither
//!   yields a candidate. **S4b (w2-kinds), the question exception**:
//!   a question carrying an open-exploration shape (rule B1 idea label or
//!   rule B2 opener) is a `brainstorm` candidate — exploration is exactly
//!   what the brainstorm kind records. **S4c (w3)**: a CHECKBOXED question
//!   with NO exploration cue is work the author literally filed
//!   (`- [ ] is this even needed?`), so rule T1's `[ ] ` evidence outranks
//!   the question-not-claim rule and the segment falls back to a `task`
//!   candidate instead of being silently dropped. Questions can only ever
//!   yield `brainstorm`, the S4c checkboxed-`task` fallback, or nothing:
//!   no other kind (fact/procedure/decision/epic/doc and the u-r11
//!   governance kinds alike) ever fires on a `?` segment.
//!
//! ## Kind rules — fixed precedence
//! `Task > Epic > Brainstorm > Doc > Procedure-label > Constraint-label >
//! Capability > FailurePattern-label > Decision > Prohibition >
//! Failure-frame > Procedure-shape > Fact`
//!
//! Two rule families, in one fixed ladder:
//!
//! - **Label rules (T, E, B, DC, P0, CN1, CP1/CP2, FP1 — w2-kinds +
//!   w2-fix + u-r11).** Explicit labels the author wrote (`[ ]` checkbox,
//!   `todo:`, `epic:`, `ideia:`, `runbook:`,
//!   `procedure:`/`procedimento:`, `constraint:`/`restrição:`,
//!   `capability:`/`capacidade:`/`use when …`,
//!   `failure:`/`falha:`/`symptom:`/`sintoma:` …) are AUTHORED evidence,
//!   so they outrank every shape heuristic: `- [ ] use tabs instead of
//!   spaces` is an open task that happens to describe a choice, not a
//!   decision, and `procedure: we chose to restart X` is a labeled
//!   procedure, not a decision (w2-fix: the procedure kind's own label
//!   word now derives it, like every sibling kind). All label rules
//!   anchor at the segment START (word-boundary safe), so at most one can
//!   fire per segment and their relative order is fixed by the ladder
//!   above.
//! - **Shape rules (D, CN2, FP2, P1–P3, F).** Decision cues are the
//!   rarest and most specific signal, so they win over the u-r11
//!   governance frames (a negated-modal prohibition, then a failure
//!   idiom — both word-pair matched, position-free), which win over the
//!   (very common) imperative shape, which wins over the (broadest)
//!   declarative shape. **F1b (w2-fix)** adds the mid-segment
//!   prescriptive frame (`… never/always/nunca/sempre/jamais …`): a
//!   standing rule like "the existence probe never blocks a capture" is a
//!   claim worth a candidate even without an F2 entity — over-fire is
//!   tolerated by the over-generate law, fabrication is not. The bare
//!   adverbs stay OUT of rule CN2 on purpose: only the negated modal
//!   (`must not`, `não pode(m)`, `não deve(m)`) is unambiguous
//!   prohibition.
//!
//! One segment yields at most ONE candidate; the cue records exactly which
//! rule and literal fired. Each rule is documented at its `const` table
//! below.

use serde::{Deserialize, Serialize};

/// The closed candidate-type vocabulary: every candidate is typed as
/// EXACTLY one of these before reaching classify. The W1 triple
/// (fact/procedure/decision) is donor parity with its wire names carried
/// verbatim; the W2 work-plane kinds (task/epic/brainstorm/doc) realize
/// the CAMPAIGN rung "SSOT work/docs plane returns as capsule KINDS".
///
/// Wire names are snake_case (house parity with
/// [`crate::relation::RelationKind`]); every kind is a single lowercase
/// word, so the W1 bytes are identical to the historical `lowercase`
/// forms — no wire change for existing stored labels.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CandidateKind {
    /// A declarative claim about the world (entity-bearing).
    Fact,
    /// A how-to / standing operating rule (imperative or step shaped).
    Procedure,
    /// A recorded choice (decision verbs, locked/chosen/ratified language).
    Decision,
    /// An open work item (w2-kinds: imperative-todo shaped — unchecked
    /// checkbox, todo/pendente markers).
    Task,
    /// A grouping initiative other work hangs off (w2-kinds:
    /// epic/initiative/milestone labels).
    Epic,
    /// An open question or idea — exploration, not a claim (w2-kinds:
    /// idea labels, open-exploration questions).
    Brainstorm,
    /// Reference / longform documentation material (w2-kinds:
    /// doc/runbook/guide labels; plus classify's longform fallback).
    Doc,
    /// A standing prohibition or hard limit (u-r11 governance plane:
    /// constraint/restrição labels, negated-modal prohibition frames).
    Constraint,
    /// What something is FOR — an applicability claim (u-r11:
    /// capability/capacidade labels, use-when openers).
    Capability,
    /// A recurring failure shape — symptom plus context (u-r11:
    /// failure/falha/symptom/sintoma labels, fails-with/breaks-when
    /// frames). The first multi-word kind: its snake_case wire name is
    /// `failure_pattern`.
    FailurePattern,
}

impl CandidateKind {
    /// All candidate kinds (closed enum — exactly ten), declaration
    /// order.
    pub const ALL: [CandidateKind; 10] = [
        CandidateKind::Fact,
        CandidateKind::Procedure,
        CandidateKind::Decision,
        CandidateKind::Task,
        CandidateKind::Epic,
        CandidateKind::Brainstorm,
        CandidateKind::Doc,
        CandidateKind::Constraint,
        CandidateKind::Capability,
        CandidateKind::FailurePattern,
    ];

    /// The wire name (snake_case; the single-word kinds are byte-identical
    /// to the donor's lowercase names — `failure_pattern` is the first
    /// kind where snake_case and lowercase actually differ).
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            CandidateKind::Fact => "fact",
            CandidateKind::Procedure => "procedure",
            CandidateKind::Decision => "decision",
            CandidateKind::Task => "task",
            CandidateKind::Epic => "epic",
            CandidateKind::Brainstorm => "brainstorm",
            CandidateKind::Doc => "doc",
            CandidateKind::Constraint => "constraint",
            CandidateKind::Capability => "capability",
            CandidateKind::FailurePattern => "failure_pattern",
        }
    }
}

/// One extracted candidate: a verbatim segment of the source blob, its
/// deterministic kind hint, and the literal cue that fired.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ExtractCandidate {
    /// Verbatim substring of the source blob (trimmed, markers stripped —
    /// never synthesized or reworded; see module doc "never fabricate").
    pub content: String,
    /// Which closed kind the heuristics assign.
    pub kind: CandidateKind,
    /// Why: `<rule>:'<literal>'` — the exact rule id and the literal token
    /// or phrase that fired, so every kind hint is auditable back to bytes
    /// actually present in the content.
    pub cue: String,
}

/// **Rule D1 — decision words.** A segment containing one of these words
/// (matched on WORD boundaries — the segment is split into
/// alphanumeric+apostrophe runs, so `"blocked"` can never fire `"locked"`)
/// records a choice. Matched in segment order; the first hit is the cue.
/// Closed, auditable list — alphabetical for review. English AND
/// Portuguese (w1d: the owner's canon is partly PT; `decisão:` /
/// `decidimos …` lines silently produced nothing). q107: PT `-ado/-ada`
/// participles are gender-paired (congelado/congelada, escolhido/escolhida,
/// rejeitado/rejeitada, travado/travada, vetado/vetada, …), so `adotada` and
/// `decidida` join their masculines — "a política foi adotada" records a
/// choice like "o padrão foi adotado", not a bare fact.
const DECISION_WORDS: &[&str] = &[
    "adopted",
    "adotada",
    "adotado",
    "adotamos",
    "chose",
    "chosen",
    "congelado",
    "congelada",
    "decided",
    "decidida",
    "decidido",
    "decidimos",
    "decidiu",
    "decision",
    "decisao",
    "decisão",
    "escolhemos",
    "escolhida",
    "escolhido",
    "frozen",
    "locked",
    "optamos",
    "opted",
    "ratificado",
    "ratificada",
    "ratified",
    "rejeitado",
    "rejeitada",
    "rejected",
    "settled",
    "superseded",
    "supersedes",
    "travado",
    "travada",
    "vetado",
    "vetada",
    "veto",
    "vetoed",
];

/// **Rule D2 — decision phrases.** Multi-word choice framings checked as
/// case-folded substrings AFTER D1 misses, in this declared order (first
/// declared hit is the cue). `"instead of"` deliberately outranks the
/// imperative opener a line like "use tabs instead of spaces" also carries:
/// such a line records a choice, not merely a step. EN + PT.
const DECISION_PHRASES: &[&str] = &[
    "we will use",
    "going with",
    "instead of",
    "vamos usar",
    "em vez de",
    "ao invés de",
    "ao inves de",
];

/// **Rule P1 — step prefix.** A segment whose body literally starts with
/// `step <digits>` is a procedure step regardless of the body shape — the
/// author named it a step. (The brief's "step patterns → Procedure",
/// narrowed: BARE ordered markers `1.`/`1)` are stripped as neutral list
/// dress instead — see module doc S3 — because numbered lists enumerate
/// facts as often as steps; only the literal word "step" is unambiguous.)
///
/// **Rule P2 — imperative opener.** A segment whose FIRST word (case
/// folded) is one of these openers is imperative-shaped: a how-to or a
/// standing rule ("run cargo fmt", "never push"). First-word anchoring is
/// what keeps this from firing mid-sentence ("we run tests" does not
/// trip). `always`/`never`/`don't`/`do` are prescriptive prefixes, not
/// verbs, but open standing rules the same way. Closed, auditable list —
/// alphabetical, English AND Portuguese (w1d: `sempre …` / `nunca …` /
/// `não rode …` standing rules silently produced nothing).
/// Over-triggering (e.g. "test coverage is low" → procedure)
/// is tolerated by the over-generate law; fabrication is not.
const IMPERATIVE_OPENERS: &[&str] = &[
    "abra",
    "add",
    "adicione",
    "always",
    "aplique",
    "apply",
    "atualize",
    "avoid",
    "build",
    "call",
    "check",
    "commit",
    "configure",
    "copy",
    "create",
    "crie",
    "delete",
    "deploy",
    "disable",
    "do",
    "don't",
    "enable",
    "ensure",
    "evite",
    "execute",
    "exija",
    "export",
    "exporte",
    "faca",
    "faça",
    "fetch",
    "garanta",
    "gere",
    "import",
    "importe",
    "inicie",
    "inject",
    "install",
    "instale",
    "jamais",
    "keep",
    "mantenha",
    "move",
    "nao",
    "não",
    "never",
    "nunca",
    "open",
    "pare",
    "pin",
    "prefer",
    "prefira",
    "pull",
    "push",
    "rebase",
    "register",
    "reinicie",
    "remova",
    "remove",
    "restart",
    "rode",
    "run",
    "sempre",
    "serialize",
    "set",
    "start",
    "stop",
    "strip",
    "suba",
    "test",
    "teste",
    "update",
    "use",
    "valide",
    "validate",
    "verifique",
    "verify",
    "wrap",
];

/// **Rule P0 — procedure label (w2-fix).** A segment OPENING with one of
/// these markers + the [`labeled`] separator (`procedure: start the
/// server with --db`, `procedimento — religar o listener`) is a labeled
/// procedure: the kind's own label word derives it, consistent with every
/// sibling kind's label cue (fleet-2: `Procedure:` content silently
/// classified as fact or derived nothing). Ladder slot: BELOW the
/// work-plane labels, ABOVE the decision shape — an authored label beats
/// a shape heuristic. Closed, auditable list — alphabetical, EN + PT.
const PROCEDURE_LABELS: &[&str] = &[
    "how-to",
    "howto",
    "passo a passo",
    "passos",
    "procedimento",
    "procedure",
    "steps",
];

/// **Rule P0b — how-to opener (w2-fix).** A segment whose folded body
/// STARTS with one of these goal openers (word-boundary via
/// [`opener_prefix`]) names a how-to without any separator: `how to
/// religar o listener`, `steps to redeploy staging`. Closed list — EN
/// then PT, declaration order is cue-attribution order.
const PROCEDURE_OPENERS: &[&str] = &["how to", "steps to", "como fazer", "passos para"];

/// **Rule F1 — declarative frame.** A segment is fact-shaped only when it
/// carries one of these copular/relational frames (case-folded substring,
/// space-delimited so `"is"` inside a word can never fire) AND rule F2
/// finds an entity. Checked in this declared order; the first declared hit
/// is the cue. EN + PT (w1d), plus the standing-rule modals
/// `must`/`deve`/`devem` — "X must stay OFF until FY26" is a claim worth a
/// candidate.
///
/// **q107 — PT number pairs.** Every PT copular/relational verb here is
/// number-paired (é/são, foi/foram, era/eram, requer/requerem,
/// depende de/dependem de, significa/significam, aponta para/apontam para,
/// roda em/rodam em, pertence a/pertencem a, contém/contêm, deve/devem), so
/// a plural-subject claim ("PRs … devem ter dois revisores") mines like its
/// singular twin — the `deve`-only gap silently dropped real minutes. The
/// EN lexical verbs stay 3rd-person-singular BY DESIGN: only the auxiliaries
/// number-pair (is/are, was/were, has/have, all present), and completing
/// them was the owner's established pattern; adding EN bare-verb plurals
/// (`use`, `own`, `mean`, …) would extend behavior past an existing pair, so
/// it is left as an owner-visible advisory, not this sweep.
const DECLARATIVE_FRAMES: &[&str] = &[
    " is ",
    " are ",
    " was ",
    " were ",
    " has ",
    " have ",
    " lives at ",
    " lives in ",
    " uses ",
    " requires ",
    " depends on ",
    " defaults to ",
    " runs on ",
    " contains ",
    " belongs to ",
    " means ",
    " maps to ",
    " points to ",
    " equals ",
    " = ",
    " -> ",
    " => ",
    " owns ",
    " must ",
    " é ",
    " são ",
    " foi ",
    " foram ",
    " era ",
    " eram ",
    " fica em ",
    " ficam em ",
    " usa ",
    " usam ",
    " tem ",
    " têm ",
    " requer ",
    " requerem ",
    " depende de ",
    " dependem de ",
    " significa ",
    " significam ",
    " aponta para ",
    " apontam para ",
    " roda em ",
    " rodam em ",
    " pertence a ",
    " pertencem a ",
    " contém ",
    " contêm ",
    " deve ",
    " devem ",
];

/// **Rule F1b — prescriptive frame (w2-fix).** A segment carrying one of
/// these standing-rule adverbs on a WORD boundary ([`words`], so
/// `"nevermind"` can never fire `"never"`) is a claim about standing
/// behavior — "the existence probe never blocks a capture" — and yields a
/// fact candidate WITHOUT the F2 entity requirement (rule-dense gotcha
/// bullets rarely carry a concrete-entity token; q8). First-word
/// `never`/`always` stays rule P2's imperative shape — F1b only fires
/// when F1 missed, so the ladder is unchanged for every previously
/// extracted segment. Closed list — EN + PT.
const PRESCRIPTIVE_WORDS: &[&str] = &["never", "always", "nunca", "sempre", "jamais"];

/// **Rule T2 — todo word (w2-kinds; q85: the task kind's OWN label
/// words).** A segment whose FIRST word (word boundary via [`words`], so
/// `"todos"`/`"tasks"`/`"tarefas"` can never fire `"todo"`/`"task"`/
/// `"tarefa"`) is one of these todo markers is an open work item
/// regardless of body shape — the author filed it as pending. `task` and
/// `tarefa` are the task kind's own name words: because [`words`] splits
/// on the `:`/`—` separator, the LABEL form (`task: migrar …`,
/// `Tarefa: revisar …`) derives here through the first-word check, exactly
/// as `todo:` already did — the sibling of q64's procedure-label rule for
/// a single-token marker. Closed, auditable list — alphabetical, English
/// AND Portuguese. (Rule **T1** — the unchecked `[ ] ` checkbox — is
/// recorded during S3 stripping, see [`strip_markers`]; it is the
/// strongest task evidence and needs no table.)
const TODO_WORDS: &[&str] = &[
    "fixme",
    "pendencia",
    "pendência",
    "pendente",
    "tarefa",
    "task",
    "todo",
];

/// **Rule T3 — todo label (w2-kinds).** Multi-word PT todo framing checked
/// with the shared [`labeled`] anchor (`a fazer: revisar o PR`). A
/// mid-sentence `a fazer` (e.g. "há muito a fazer") never fires — the
/// label must open the segment and carry the separator.
const TODO_LABELS: &[&str] = &["a fazer"];

/// **Rule E1 — epic label (w2-kinds).** A segment OPENING with one of
/// these grouping/initiative markers + the [`labeled`] separator
/// (`epic: auth revamp`, `iniciativa — consolidar runbooks`) names a
/// grouping construct other work hangs off. Closed, auditable list —
/// alphabetical, English AND Portuguese. Over-fire corners (`marco:` as a
/// person's name) are tolerated by the over-generate law.
const EPIC_LABELS: &[&str] = &[
    "campaign",
    "campanha",
    "epic",
    "epico",
    "initiative",
    "iniciativa",
    "marco",
    "milestone",
    "workstream",
    "épico",
];

/// **Rule B1 — idea label (w2-kinds).** A segment OPENING with one of
/// these idea markers + the [`labeled`] separator (`ideia: usar WAL`)
/// records exploration. Closed list — alphabetical, EN + PT
/// (`brainstorm` is both).
const IDEA_LABELS: &[&str] = &[
    "brainstorm",
    "hipotese",
    "hipótese",
    "hypothesis",
    "idea",
    "ideia",
];

/// **Rule B2 — open-question opener (w2-kinds).** A segment whose folded
/// body STARTS with one of these exploration openers (word-boundary
/// checked: the opener must be followed by space/`,`/`?` or end the body)
/// is possibility-space probing — the brainstorm shape — question mark or
/// not. This list is also the ONE gate through which a `?` segment can
/// yield a candidate (module doc S4b): `what if we cache the digest?` is
/// a brainstorm; `do we need the spool?` stays a skipped non-claim.
/// Closed list — EN then PT, declaration order is cue-attribution order.
const OPEN_QUESTION_OPENERS: &[&str] = &[
    "could we",
    "should we",
    "what if",
    "why not",
    "deveriamos",
    "deveríamos",
    "e se",
    "poderiamos",
    "poderíamos",
    "por que nao",
    "por que não",
    "que tal",
];

/// **Rule DC1 — doc label (w2-kinds).** A segment OPENING with one of
/// these reference markers + the [`labeled`] separator
/// (`runbook: como religar o listener`, `reference — spool format`) names
/// reference material. Closed list — alphabetical, EN + PT (`manual` is
/// both). The longform side of the doc kind (rule **DC2**) lives in
/// [`crate::classify`]: it is a whole-blob property, not a segment cue.
const DOC_LABELS: &[&str] = &[
    "doc",
    "docs",
    "documentacao",
    "documentation",
    "documentação",
    "guia",
    "guide",
    "manual",
    "readme",
    "reference",
    "referencia",
    "referência",
    "runbook",
];

/// **Rule CN1 — constraint label (u-r11).** A segment OPENING with one of
/// these markers + the [`labeled`] separator (`constraint: one embedder
/// per store`, `restrição — não usar rede em runtime`) names a standing
/// prohibition or hard limit the author filed as such. Closed, auditable
/// list — alphabetical, EN + PT (both diacritic spellings). The plural
/// (`constraints are hard…`) never fires: the separator requirement IS
/// the word boundary (q107 plural/substring discipline).
const CONSTRAINT_LABELS: &[&str] = &["constraint", "restricao", "restrição"];

/// **Rule CN2 — prohibition frame (u-r11).** A segment carrying one of
/// these NEGATED-modal word PAIRS (matched on [`words`] boundaries via
/// [`word_pair_cue`], so `"não poderia"` / `"não poderemos"` can never
/// fire `"não pode"`) is a standing prohibition — "the API key must not
/// be logged", "o deploy não pode rodar na sexta". Position-free: the
/// pair fires segment-start and mid-segment alike. Deliberately EXCLUDES
/// the bare adverbs never/nunca/sempre/jamais — first-word they are rule
/// P2's imperative shape and mid-segment rule F1b's prescriptive fact,
/// both established derivations with their own pins; only the negated
/// modal is unambiguous prohibition. PT modals are number-paired (q107:
/// pode/podem, deve/devem, both diacritic spellings); EN
/// `must not`/`must never` have no plural form. Declaration order is
/// cue-attribution order.
const PROHIBITION_FRAMES: &[(&str, &str)] = &[
    ("must", "not"),
    ("must", "never"),
    ("não", "pode"),
    ("nao", "pode"),
    ("não", "podem"),
    ("nao", "podem"),
    ("não", "deve"),
    ("nao", "deve"),
    ("não", "devem"),
    ("nao", "devem"),
];

/// **Rule CP1 — capability label (u-r11).** A segment OPENING with one of
/// these markers + the [`labeled`] separator (`capability: renders the
/// store as mermaid`) names an applicability claim — what something is
/// FOR. Closed list — EN + PT.
const CAPABILITY_LABELS: &[&str] = &["capability", "capacidade"];

/// **Rule CP2 — use-when opener (u-r11).** A segment whose folded body
/// STARTS with one of these applicability openers (word-boundary via
/// [`opener_prefix`], the P0b discipline) names when to reach for
/// something: `use when the store file is corrupted`, `use quando
/// precisar de recall semântico`. Start-anchored ON PURPOSE: a bare
/// `use …` imperative stays rule P2 procedure, and a mid-segment
/// "we use when-clauses" never fires. Closed list — EN then PT.
const USE_WHEN_OPENERS: &[&str] = &["use when", "use quando"];

/// **Rule FP1 — failure label (u-r11).** A segment OPENING with one of
/// these failure/symptom markers + the [`labeled`] separator (`failure:
/// OOM on exports`, `sintoma — digest trava`) names a recurring failure
/// shape. Closed, auditable list — alphabetical, EN + PT. A bare
/// mid-segment `falha`/`failure` word is NEVER a cue — only the labeled
/// form and the rule FP2 frames fire.
const FAILURE_LABELS: &[&str] = &["failure", "falha", "sintoma", "symptom"];

/// **Rule FP2 — failure frame (u-r11).** A segment carrying one of these
/// failure-idiom word PAIRS (matched on [`words`] boundaries via
/// [`word_pair_cue`], so `"fails without"` can never fire
/// `"fails with"`) records how something breaks — "the build fails with
/// OOM", "o job falha quando roda em paralelo". PT verbs are
/// number-paired (q107: falha/falham, quebra/quebram); the EN lexical
/// verbs stay 3rd-person-singular BY the q107 EN law. Declaration order
/// is cue-attribution order.
const FAILURE_FRAMES: &[(&str, &str)] = &[
    ("fails", "with"),
    ("fails", "when"),
    ("breaks", "with"),
    ("breaks", "when"),
    ("falha", "com"),
    ("falham", "com"),
    ("falha", "quando"),
    ("falham", "quando"),
    ("quebra", "com"),
    ("quebram", "com"),
    ("quebra", "quando"),
    ("quebram", "quando"),
];

/// Extract `0..N` typed candidates from a free-text blob.
///
/// Deterministic and pure (module doc): candidates arrive in source order,
/// one per extractable segment; a blob with nothing extractable returns an
/// empty `Vec` (never fabricated), and `extract("")` is `[]`.
#[must_use]
pub fn extract(text: &str) -> Vec<ExtractCandidate> {
    let mut candidates = Vec::new();
    let mut in_fence = false;
    for line in text.lines() {
        let trimmed = line.trim();
        // Rule S2: toggle on fence lines, skip fenced interiors.
        if trimmed.starts_with("```") {
            in_fence = !in_fence;
            continue;
        }
        if in_fence {
            continue;
        }
        // Rule S2b (w1d): a markdown INDENTED code line (4+ spaces or a
        // tab, and not list/quote dress) carries code — tracebacks and
        // snippets pasted into session logs are never claims.
        if is_indented_code(line) {
            continue;
        }
        // Rule S1b (w1d): sentences WITHIN a line are analyzed
        // independently — a prose paragraph is not one giant candidate.
        // Every piece is still a verbatim substring of the source.
        for sentence in split_sentences(trimmed) {
            // O2: a `; `-split clause is otherwise verbatim, but a trailing
            // `;` is the separator between clauses, not content — trim it
            // (and any space it left) off the candidate slice.
            let sentence = sentence.strip_suffix(';').map_or(sentence, str::trim_end);
            if let Some(candidate) = candidate_for_segment(sentence) {
                candidates.push(candidate);
            }
        }
    }
    candidates
}

/// Rule S2b probe: 4+ leading spaces or any leading tab marks a markdown
/// indented code line — unless the indented text is list/quote dress
/// (nested bullets legitimately indent deep).
fn is_indented_code(line: &str) -> bool {
    let indent_len = line.len() - line.trim_start_matches([' ', '\t']).len();
    let indent = line.get(..indent_len).unwrap_or("");
    let deep = indent.contains('\t') || indent.len() >= 4;
    if !deep {
        return false;
    }
    let rest = line.trim_start();
    let dressed = rest.starts_with(['-', '*', '+', '>', '#'])
        || rest.starts_with("[ ]")
        || rest.starts_with("[x]")
        || rest.starts_with("[X]")
        || rest.chars().next().is_some_and(|c| c.is_ascii_digit());
    !dressed
}

/// Rule S1b splitter: split a trimmed line at `. ` / `! ` / `? `
/// boundaries whose next non-space char is uppercase and whose preceding
/// word is longer than one char (so `e.g. Foo`, `i.e. Bar`, and initials
/// never split; `1.96` has no following space and cannot). Rule S1c
/// (w2-fix): `; ` splits UNCONDITIONALLY — semicolon-joined clauses are
/// independent claims (rule-dense gotcha bullets), never abbreviations,
/// and the next clause legitimately opens lowercase. Every piece is a
/// verbatim substring; a line with no boundary passes through whole.
fn split_sentences(line: &str) -> Vec<&str> {
    let bytes = line.as_bytes();
    let mut pieces = Vec::new();
    let mut start = 0usize;
    for (i, b) in bytes.iter().enumerate() {
        if *b == b';' && bytes.get(i + 1) == Some(&b' ') {
            // S1c: unconditional clause boundary (piece keeps its `;`,
            // staying a verbatim substring like the `.` path below).
            if let Some(piece) = line.get(start..=i) {
                pieces.push(piece.trim());
            }
            start = i + 1;
            continue;
        }
        if !matches!(b, b'.' | b'!' | b'?') || bytes.get(i + 1) != Some(&b' ') {
            continue;
        }
        // The word immediately before the punctuation, in the current piece.
        let before = line.get(start..i).unwrap_or("");
        let prev_word = before.rsplit(char::is_whitespace).next().unwrap_or("");
        if prev_word.chars().count() <= 1 || prev_word.contains('.') {
            continue; // abbreviation dot ("e.g. X", "J. Smith") — no boundary
        }
        let after = line.get(i + 1..).unwrap_or("").trim_start();
        if !after.chars().next().is_some_and(char::is_uppercase) {
            continue;
        }
        if let Some(piece) = line.get(start..=i) {
            pieces.push(piece.trim());
        }
        start = i + 1;
    }
    if let Some(tail) = line.get(start..) {
        let tail = tail.trim();
        if !tail.is_empty() {
            pieces.push(tail);
        }
    }
    pieces
}

/// Apply S3/S4 + the kind rules to one trimmed line (module doc: fixed
/// precedence `Task > Epic > Brainstorm > Doc > Procedure-label >
/// Constraint-label > Capability > FailurePattern-label > Decision >
/// Prohibition > Failure-frame > Procedure-shape > Fact`; label rules are
/// start-anchored so at most one can fire).
fn candidate_for_segment(line: &str) -> Option<ExtractCandidate> {
    let (body, unchecked_checkbox) = strip_markers(line);
    // Rule S4: fewer than two words carries no claim...
    if body.split_whitespace().count() < 2 {
        return None;
    }
    let folded = body.to_lowercase();
    // ...and a question is not a claim (guards e.g. "do we need X?" from
    // the P2 opener "do") — EXCEPT the S4b brainstorm gate: an
    // open-exploration question (B1/B2) is exactly what the brainstorm
    // kind records — and the S4c filed-work fallback: a CHECKBOXED
    // question with no exploration cue is open work the author literally
    // filed (`[ ] `, rule T1 evidence), so it yields a task instead of
    // being silently dropped. No other kind can fire on a `?` segment.
    if body.ends_with('?') {
        if let Some(cue) = brainstorm_cue(&folded) {
            return Some(ExtractCandidate {
                content: body.to_string(),
                kind: CandidateKind::Brainstorm,
                cue,
            });
        }
        if unchecked_checkbox {
            return task_cue(&folded, true).map(|cue| ExtractCandidate {
                content: body.to_string(),
                kind: CandidateKind::Task,
                cue,
            });
        }
        return None;
    }
    let (kind, cue) = task_cue(&folded, unchecked_checkbox)
        .map(|cue| (CandidateKind::Task, cue))
        .or_else(|| epic_cue(&folded).map(|cue| (CandidateKind::Epic, cue)))
        .or_else(|| brainstorm_cue(&folded).map(|cue| (CandidateKind::Brainstorm, cue)))
        .or_else(|| doc_cue(&folded).map(|cue| (CandidateKind::Doc, cue)))
        .or_else(|| procedure_label_cue(&folded).map(|cue| (CandidateKind::Procedure, cue)))
        .or_else(|| constraint_label_cue(&folded).map(|cue| (CandidateKind::Constraint, cue)))
        .or_else(|| capability_cue(&folded).map(|cue| (CandidateKind::Capability, cue)))
        .or_else(|| failure_label_cue(&folded).map(|cue| (CandidateKind::FailurePattern, cue)))
        .or_else(|| decision_cue(&folded).map(|cue| (CandidateKind::Decision, cue)))
        .or_else(|| prohibition_cue(&folded).map(|cue| (CandidateKind::Constraint, cue)))
        .or_else(|| failure_frame_cue(&folded).map(|cue| (CandidateKind::FailurePattern, cue)))
        .or_else(|| procedure_cue(&folded).map(|cue| (CandidateKind::Procedure, cue)))
        .or_else(|| fact_cue(&folded, body).map(|cue| (CandidateKind::Fact, cue)))?;
    Some(ExtractCandidate {
        content: body.to_string(),
        kind,
        cue,
    })
}

/// Rule S3: peel leading markdown dress (headings, blockquotes, bullets,
/// checkboxes, ordered markers) until stable. Terminates because every
/// productive pass strictly shortens the slice (strippers only remove
/// leading bytes, never rewrite).
///
/// S3b (w2-kinds): the returned flag records whether an UNCHECKED
/// checkbox `[ ] ` was among the stripped dress — rule T1 evidence (the
/// author flagged open work). Checked boxes `[x] `/`[X] ` stay neutral.
fn strip_markers(line: &str) -> (&str, bool) {
    let mut rest = line.trim();
    let mut unchecked_checkbox = false;
    let mut saw_stamp = false;
    let mut speaker_stripped = false;
    loop {
        let before = rest;
        rest = rest.trim_start_matches('#').trim_start();
        rest = rest.trim_start_matches('>').trim_start();
        for marker in ["- ", "* ", "+ ", "[x] ", "[X] "] {
            if let Some(after) = rest.strip_prefix(marker) {
                rest = after.trim_start();
            }
        }
        if let Some(after) = rest.strip_prefix("[ ] ") {
            rest = after.trim_start();
            unchecked_checkbox = true; // rule T1 evidence (S3b)
        }
        rest = strip_ordered_marker(rest);
        // Rule S3c (w2-fix): chat/log dress — bracketed stamp, then (once,
        // and only on stamp evidence) one speaker tag.
        if let Some(after) = strip_bracket_stamp(rest) {
            rest = after;
            saw_stamp = true;
        }
        if saw_stamp
            && !speaker_stripped
            && let Some(after) = strip_speaker_tag(rest)
        {
            rest = after;
            speaker_stripped = true;
        }
        if rest == before {
            return (rest, unchecked_checkbox);
        }
    }
}

/// Rule S3c-1: one leading bracketed stamp `[...] ` whose interior is
/// short (≤ 32 bytes), digit-bearing, and bracket-free — a chat/log
/// timestamp (`[10:05]`, `[2026-07-18 10:05]`), never a checkbox (`[ ]`
/// carries no digit, and is stripped earlier anyway) and never content
/// like `[warn]` (no digit). Returns the remainder after the stamp and
/// its mandatory following space.
fn strip_bracket_stamp(rest: &str) -> Option<&str> {
    let interior_and_rest = rest.strip_prefix('[')?;
    let close = interior_and_rest.find(']')?;
    let interior = &interior_and_rest[..close];
    if close > 32 || !interior.bytes().any(|b| b.is_ascii_digit()) || interior.contains('[') {
        return None;
    }
    let after = interior_and_rest.get(close + 1..)?;
    Some(after.strip_prefix(' ')?.trim_start())
}

/// Rule S3c-2: one leading speaker tag `word: ` — a single
/// alphanumeric-ish token (letters/digits/`_`/`-`/`.`, ≤ 24 bytes)
/// immediately followed by `:` + a space. Consulted only after a
/// bracketed stamp proved the segment chat-shaped, at most once per
/// segment, and NEVER when the tag word is itself a cue/label word
/// ([`is_cue_word`]: `todo:`, `doc:`, `decisão:` … stay content).
fn strip_speaker_tag(rest: &str) -> Option<&str> {
    let colon = rest.find(':')?;
    let tag = &rest[..colon];
    if tag.is_empty()
        || tag.len() > 24
        || !tag
            .chars()
            .all(|c| c.is_alphanumeric() || matches!(c, '_' | '-' | '.'))
        || is_cue_word(&tag.to_lowercase())
    {
        return None;
    }
    Some(rest.get(colon + 1..)?.strip_prefix(' ')?.trim_start())
}

/// The single-token cue/label vocabulary rule S3c-2 must never strip as a
/// "speaker" — every single-word marker of every rule table. A chat tag
/// colliding with one (`marco:` as a person's name) keeps its label
/// reading, tolerated by the over-generate law.
fn is_cue_word(folded: &str) -> bool {
    TODO_WORDS.contains(&folded)
        || EPIC_LABELS.contains(&folded)
        || IDEA_LABELS.contains(&folded)
        || DOC_LABELS.contains(&folded)
        || PROCEDURE_LABELS.contains(&folded)
        || CONSTRAINT_LABELS.contains(&folded)
        || CAPABILITY_LABELS.contains(&folded)
        || FAILURE_LABELS.contains(&folded)
        || DECISION_WORDS.contains(&folded)
        || IMPERATIVE_OPENERS.contains(&folded)
        || folded == "step"
}

/// Strip one leading `"<digits>. "` / `"<digits>) "` list marker. A number
/// NOT followed by `.`/`)`+space (e.g. `"1.96 is the pin"`) is content,
/// not a marker, and is left intact.
fn strip_ordered_marker(rest: &str) -> &str {
    let digits = rest.len() - rest.trim_start_matches(|c: char| c.is_ascii_digit()).len();
    if digits == 0 {
        return rest;
    }
    let after = &rest[digits..];
    if let Some(after_sep) = after.strip_prefix('.').or_else(|| after.strip_prefix(')'))
        && let Some(after_space) = after_sep.strip_prefix(' ')
    {
        return after_space.trim_start();
    }
    rest
}

/// Word-boundary iterator: alphanumeric+apostrophe runs of the folded
/// segment (apostrophes kept so `"don't"` stays one word).
fn words(folded: &str) -> impl Iterator<Item = &str> {
    folded
        .split(|c: char| !(c.is_alphanumeric() || c == '\''))
        .filter(|w| !w.is_empty())
}

/// Shared label anchor for the work-plane label rules (T3/E1/B1/DC1): the
/// folded body STARTS with the marker immediately followed by a colon
/// (spaces allowed before it) or a spaced dash — `"epic: x"`, `"epic : x"`,
/// `"epic — x"`, `"epic - x"`. The separator requirement IS the word
/// boundary: `"epical: x"` strips to `"al: x"` and fails; `"epic-ish"`
/// has no space before the dash and fails. First declared hit is the cue.
fn labeled(folded: &str, markers: &[&'static str]) -> Option<&'static str> {
    markers.iter().copied().find(|marker| {
        let Some(rest) = folded.strip_prefix(marker) else {
            return false;
        };
        if rest.starts_with(':') {
            return true;
        }
        let Some(spaced) = rest.strip_prefix(' ') else {
            return false;
        };
        spaced.starts_with(':') || spaced.starts_with("— ") || spaced.starts_with("- ")
    })
}

/// Shared opener anchor for rule B2: the folded body STARTS with the
/// opener, word-boundary checked — the opener must be followed by a
/// space/`,`/`?` or end the body (`"what iffy"` can never fire
/// `"what if"`). First declared hit is the cue.
fn opener_prefix(folded: &str, openers: &[&'static str]) -> Option<&'static str> {
    openers.iter().copied().find(|opener| {
        folded.strip_prefix(opener).is_some_and(|rest| {
            rest.is_empty()
                || rest.starts_with(' ')
                || rest.starts_with(',')
                || rest.starts_with('?')
        })
    })
}

/// Rules T1 (unchecked checkbox — S3b evidence), T2 (todo word), then T3
/// (todo label), in that order (strongest authored evidence first).
fn task_cue(folded: &str, unchecked_checkbox: bool) -> Option<String> {
    if unchecked_checkbox {
        return Some("todo-checkbox:'[ ]'".to_string());
    }
    if let Some(first) = words(folded).next()
        && TODO_WORDS.contains(&first)
    {
        return Some(format!("todo-word:'{first}'"));
    }
    labeled(folded, TODO_LABELS).map(|marker| format!("todo-label:'{marker}'"))
}

/// Rule E1.
fn epic_cue(folded: &str) -> Option<String> {
    labeled(folded, EPIC_LABELS).map(|marker| format!("epic-label:'{marker}'"))
}

/// Rules B1 (idea label) then B2 (open-question opener). Called from BOTH
/// paths: the precedence ladder AND the S4b question gate — one rule set,
/// so a labeled idea and an open question classify identically with or
/// without their `?`.
fn brainstorm_cue(folded: &str) -> Option<String> {
    if let Some(marker) = labeled(folded, IDEA_LABELS) {
        return Some(format!("idea-label:'{marker}'"));
    }
    opener_prefix(folded, OPEN_QUESTION_OPENERS).map(|opener| format!("open-question:'{opener}'"))
}

/// Rule DC1 (rule DC2 — the longform fallback — lives in
/// [`crate::classify`], whole-blob property).
fn doc_cue(folded: &str) -> Option<String> {
    labeled(folded, DOC_LABELS).map(|marker| format!("doc-label:'{marker}'"))
}

/// Shared word-pair anchor for the u-r11 frame rules (CN2/FP2): the first
/// declared `(a, b)` pair appearing as CONSECUTIVE [`words`] of the folded
/// segment is the cue — exact word boundaries on both sides, position-free
/// (`"não poderia"` never fires `("não", "pode")`; `"fails without"`
/// never fires `("fails", "with")`).
fn word_pair_cue(folded: &str, pairs: &[(&'static str, &'static str)]) -> Option<String> {
    let tokens: Vec<&str> = words(folded).collect();
    for (a, b) in pairs {
        if tokens.windows(2).any(|pair| pair[0] == *a && pair[1] == *b) {
            return Some(format!("{a} {b}"));
        }
    }
    None
}

/// Rule CN1 — the AUTHORED constraint label (ladder: with the label
/// block, above the decision shape).
fn constraint_label_cue(folded: &str) -> Option<String> {
    labeled(folded, CONSTRAINT_LABELS).map(|marker| format!("constraint-label:'{marker}'"))
}

/// Rule CN2 — the prohibition frame (ladder: below the decision shape —
/// choice language is the rarer, more specific signal — and above the
/// procedure/fact shapes, so a negated modal outranks the broad
/// imperative and declarative readings).
fn prohibition_cue(folded: &str) -> Option<String> {
    word_pair_cue(folded, PROHIBITION_FRAMES).map(|pair| format!("prohibition:'{pair}'"))
}

/// Rules CP1 (capability label) then CP2 (use-when opener) — AUTHORED
/// applicability evidence, the exact P0/P0b split (label + opener, one
/// rule fn, above the decision shape).
fn capability_cue(folded: &str) -> Option<String> {
    if let Some(marker) = labeled(folded, CAPABILITY_LABELS) {
        return Some(format!("capability-label:'{marker}'"));
    }
    opener_prefix(folded, USE_WHEN_OPENERS).map(|opener| format!("use-when:'{opener}'"))
}

/// Rule FP1 — the AUTHORED failure/symptom label (ladder: with the label
/// block, above the decision shape).
fn failure_label_cue(folded: &str) -> Option<String> {
    labeled(folded, FAILURE_LABELS).map(|marker| format!("failure-label:'{marker}'"))
}

/// Rule FP2 — the failure frame (ladder: below rule CN2 — a prohibition
/// is the more specific governance signal — and above the procedure/fact
/// shapes).
fn failure_frame_cue(folded: &str) -> Option<String> {
    word_pair_cue(folded, FAILURE_FRAMES).map(|pair| format!("failure-frame:'{pair}'"))
}

/// Rules D1 then D2 (module doc precedence).
fn decision_cue(folded: &str) -> Option<String> {
    for word in words(folded) {
        if DECISION_WORDS.contains(&word) {
            return Some(format!("decision-word:'{word}'"));
        }
    }
    for phrase in DECISION_PHRASES {
        if folded.contains(phrase) {
            return Some(format!("decision-phrase:'{phrase}'"));
        }
    }
    None
}

/// Rule P0 (procedure label) then P0b (how-to opener) — the AUTHORED
/// procedure evidence, sitting ABOVE the decision shape in the ladder
/// (module doc: an authored label beats a shape heuristic).
fn procedure_label_cue(folded: &str) -> Option<String> {
    if let Some(marker) = labeled(folded, PROCEDURE_LABELS) {
        return Some(format!("procedure-label:'{marker}'"));
    }
    opener_prefix(folded, PROCEDURE_OPENERS).map(|opener| format!("how-to-opener:'{opener}'"))
}

/// Rules P1, P2, then P3.
fn procedure_cue(folded: &str) -> Option<String> {
    if let Some(after) = folded.strip_prefix("step ")
        && after.chars().next().is_some_and(|c| c.is_ascii_digit())
    {
        let number: String = after.chars().take_while(char::is_ascii_digit).collect();
        return Some(format!("step-prefix:'step {number}'"));
    }
    let first = words(folded).next()?;
    if IMPERATIVE_OPENERS.contains(&first) {
        return Some(format!("imperative-opener:'{first}'"));
    }
    // **Rule P3 — goal recipe** (w1d): "to <goal>: <steps>" / PT "para
    // <objetivo>: <passos>" is how-to shaped even though the imperative
    // verb sits after the colon ("to redeploy staging: run make …").
    if (folded.starts_with("to ") || folded.starts_with("para ")) && folded.contains(": ") {
        let opener = if folded.starts_with("to ") {
            "to"
        } else {
            "para"
        };
        return Some(format!("goal-colon:'{opener} …:'"));
    }
    None
}

/// Rule F1 (frame) AND rule F2 (entity) — both required; the cue names
/// both literals. Rule F1b (w2-fix) as the fallback: a mid-segment
/// prescriptive standing-rule adverb yields a fact WITHOUT the entity
/// requirement (module doc; first-word prescriptives were already rule
/// P2's imperative shape and never reach here).
fn fact_cue(folded: &str, original: &str) -> Option<String> {
    if let Some(frame) = DECLARATIVE_FRAMES.iter().find(|f| folded.contains(**f))
        && let Some(entity) = entity_in(original)
    {
        return Some(format!("declarative:'{}' entity:'{entity}'", frame.trim()));
    }
    words(folded)
        .find(|w| PRESCRIPTIVE_WORDS.contains(w))
        .map(|w| format!("prescriptive:'{w}'"))
}

/// **Rule F2 — entity.** The first whitespace token of the ORIGINAL-case
/// segment that names something concrete, where "concrete" is this closed,
/// auditable test (surrounding punctuation stripped first):
///
/// - a backtick-wrapped code span kept whole (`` `nsh` ``),
/// - a path/symbol shape (`/`, `\`, `::`),
/// - any digit (versions, ports, line refs, ids, dates),
/// - an internal dot (`PLAN.md`, `menot.you`),
/// - uppercase beyond the first char (`SQLite`, `nMEMORY`, `SSOT` — plain
///   sentence-initial capitals like `Nott` deliberately do NOT count).
///
/// Over-inclusive corners (`MUST`, `TODO` read as entities) are tolerated
/// by the over-generate law; the alternative — guessing at semantics — is
/// the fabrication this module forbids itself.
fn entity_in(segment: &str) -> Option<String> {
    for raw in segment.split_whitespace() {
        if raw.len() > 2 && raw.starts_with('`') && raw.ends_with('`') {
            return Some(raw.to_string());
        }
        let token = raw.trim_matches(|c: char| {
            matches!(
                c,
                '(' | ')'
                    | '['
                    | ']'
                    | '{'
                    | '}'
                    | '.'
                    | ','
                    | ';'
                    | ':'
                    | '!'
                    | '?'
                    | '"'
                    | '\''
                    | '`'
                    | '…'
            )
        });
        if token.chars().count() < 2 {
            continue;
        }
        // Code-shape fence (w1d): a token with INTERIOR call/expression
        // punctuation (`psycopg.connect(dsn)`, `foo{bar}`, `x;`) is code
        // debris, not an entity — pairing with the `=`/`->` frames it
        // turned tracebacks into "fact" candidates.
        if token.contains(['(', ')', '{', '}', ';']) {
            continue;
        }
        let concrete = token.contains('/')
            || token.contains('\\')
            || token.contains("::")
            || token.chars().any(|c| c.is_ascii_digit())
            || token.contains('.')
            || token.chars().skip(1).any(char::is_uppercase);
        if concrete {
            return Some(token.to_string());
        }
    }
    None
}

#[cfg(test)]
mod tests {
    #![allow(
        clippy::unwrap_used,
        clippy::expect_used,
        reason = "tests use unwrap/expect so fixture failures fail at the assertion site"
    )]

    use super::*;

    /// Multi-kind fixture blob: markdown dress, all three kinds, noise
    /// lines, a question, a fenced code block, and a step prefix.
    const FIXTURE: &str = "\
# session notes 2026-07-18

We chose SQLite over Postgres for the store.
- run cargo fmt before every commit
- the crate lives at capabilities/nmemory/
the default port is 4320
hello there, general chatter with no claim shape
do we need the spool?
```rust
let fenced = \"code lines are never extracted\";
```
1. install the toolchain
2. build the crate
Step 3: restart the listener
Locked: Capsule v1 is frozen.
thanks
";

    /// The golden table: exact content, kind, and cue for every candidate,
    /// in source order.
    fn golden() -> Vec<(&'static str, CandidateKind, &'static str)> {
        vec![
            (
                "We chose SQLite over Postgres for the store.",
                CandidateKind::Decision,
                "decision-word:'chose'",
            ),
            (
                "run cargo fmt before every commit",
                CandidateKind::Procedure,
                "imperative-opener:'run'",
            ),
            (
                "the crate lives at capabilities/nmemory/",
                CandidateKind::Fact,
                "declarative:'lives at' entity:'capabilities/nmemory/'",
            ),
            (
                "the default port is 4320",
                CandidateKind::Fact,
                "declarative:'is' entity:'4320'",
            ),
            (
                "install the toolchain",
                CandidateKind::Procedure,
                "imperative-opener:'install'",
            ),
            (
                "build the crate",
                CandidateKind::Procedure,
                "imperative-opener:'build'",
            ),
            (
                "Step 3: restart the listener",
                CandidateKind::Procedure,
                "step-prefix:'step 3'",
            ),
            (
                "Locked: Capsule v1 is frozen.",
                CandidateKind::Decision,
                "decision-word:'locked'",
            ),
        ]
    }

    #[test]
    fn golden_extraction_table_over_the_fixture_blob() {
        let candidates = extract(FIXTURE);
        let expected = golden();
        assert_eq!(
            candidates.len(),
            expected.len(),
            "candidate count drifted: {candidates:#?}"
        );
        for (candidate, (content, kind, cue)) in candidates.iter().zip(expected) {
            assert_eq!(candidate.content, content);
            assert_eq!(candidate.kind, kind, "kind drifted for {content:?}");
            assert_eq!(candidate.cue, cue, "cue drifted for {content:?}");
        }
    }

    #[test]
    fn extraction_is_deterministic_same_input_same_output_twice() {
        assert_eq!(extract(FIXTURE), extract(FIXTURE));
    }

    #[test]
    fn every_candidate_content_is_verbatim_in_the_source() {
        // The never-fabricate law as a property: each content is a literal
        // substring of the blob — nothing synthesized, joined, or reworded.
        for candidate in extract(FIXTURE) {
            assert!(
                FIXTURE.contains(&candidate.content),
                "fabricated content: {:?}",
                candidate.content
            );
        }
    }

    #[test]
    fn empty_and_whitespace_only_input_yield_empty_output() {
        assert!(extract("").is_empty());
        assert!(extract("   \n\t\n  ").is_empty());
    }

    #[test]
    fn unextractable_prose_yields_zero_candidates_not_a_fabrication() {
        assert!(extract("hello there\nthanks for the chat\nsee you around soon").is_empty());
    }

    #[test]
    fn questions_and_single_words_are_skipped() {
        // "do" is an imperative opener, but a question is not a claim (S4).
        assert!(extract("do we need the spool?").is_empty());
        // One word carries no claim, even an imperative one (S4).
        assert!(extract("run").is_empty());
    }

    #[test]
    fn a_checkboxed_question_with_no_exploration_cue_falls_back_to_task() {
        // S4c: the author FILED the question as open work — the `[ ] `
        // checkbox (T1 evidence) outranks the question-not-claim rule when
        // no brainstorm cue fires, so the filed item is never dropped.
        let filed = extract("- [ ] is this even needed?");
        assert_eq!(
            filed.len(),
            1,
            "a checkboxed question must extract, got {filed:?}"
        );
        assert_eq!(filed[0].kind, CandidateKind::Task);
        assert_eq!(filed[0].cue, "todo-checkbox:'[ ]'");
        assert_eq!(filed[0].content, "is this even needed?");

        // S4b still wins when an exploration cue DOES fire: a checkboxed
        // open question is exploration, not a silent task.
        let explored = extract("- [ ] what if we cache the digest?");
        assert_eq!(explored.len(), 1);
        assert_eq!(explored[0].kind, CandidateKind::Brainstorm);

        // An UNFILED cue-less question still yields nothing (S4 holds).
        assert!(extract("is this even needed?").is_empty());
    }

    #[test]
    fn decision_words_match_on_word_boundaries_not_substrings() {
        // "blocked" contains "locked": the word-boundary split must keep
        // this a Fact (frame " was " + entity "PR"), never a Decision.
        let candidates = extract("the PR was blocked by CI");
        assert_eq!(candidates.len(), 1);
        assert_eq!(candidates[0].kind, CandidateKind::Fact);
        assert_eq!(candidates[0].cue, "declarative:'was' entity:'PR'");
    }

    #[test]
    fn bare_ordered_markers_are_neutral_the_body_decides_the_kind() {
        // Documented S3 improvement over a naive step rule: a numbered
        // FACT stays a fact; the marker is dress, not procedure evidence.
        let candidates = extract("1. the store is SQLite");
        assert_eq!(candidates.len(), 1);
        assert_eq!(candidates[0].content, "the store is SQLite");
        assert_eq!(candidates[0].kind, CandidateKind::Fact);
    }

    #[test]
    fn a_number_that_is_content_is_not_stripped_as_a_marker() {
        let candidates = extract("1.96 is the toolchain pin");
        assert_eq!(candidates.len(), 1);
        assert_eq!(candidates[0].content, "1.96 is the toolchain pin");
        assert_eq!(candidates[0].kind, CandidateKind::Fact);
        assert_eq!(candidates[0].cue, "declarative:'is' entity:'1.96'");
    }

    #[test]
    fn markers_are_stripped_but_content_stays_verbatim() {
        let candidates = extract("- [x] run cargo fmt");
        assert_eq!(candidates.len(), 1);
        assert_eq!(candidates[0].content, "run cargo fmt");
        assert_eq!(candidates[0].cue, "imperative-opener:'run'");
    }

    #[test]
    fn standing_rules_are_procedures() {
        let candidates = extract("never push from a workflow lane");
        assert_eq!(candidates.len(), 1);
        assert_eq!(candidates[0].kind, CandidateKind::Procedure);
        assert_eq!(candidates[0].cue, "imperative-opener:'never'");
    }

    #[test]
    fn decision_phrase_outranks_the_imperative_opener() {
        // "use ..." alone is P2; "instead of" makes it a recorded choice
        // (D2 precedence, documented at DECISION_PHRASES).
        let candidates = extract("use tabs instead of spaces");
        assert_eq!(candidates.len(), 1);
        assert_eq!(candidates[0].kind, CandidateKind::Decision);
        assert_eq!(candidates[0].cue, "decision-phrase:'instead of'");
    }

    #[test]
    fn declarative_without_an_entity_is_not_a_fact() {
        // Frame " is " present, but no token passes rule F2 — no candidate
        // (never a guessed fact).
        assert!(extract("this is fine somehow").is_empty());
    }

    #[test]
    fn fenced_code_is_never_extracted_including_unterminated_fences() {
        assert!(extract("```\nlet x = 5;\n```").is_empty());
        // Unterminated: skip through end of blob (S2).
        assert!(extract("```\nthe port is 4320").is_empty());
    }

    #[test]
    fn q107_pt_plural_modal_devem_mines_like_its_singular() {
        // The consumer literal, as a `; `-split clause inside a bigger line
        // (like the real minutes blob): the plural modal `devem` must mine a
        // fact — the `deve`-only table silently dropped it (0 candidates).
        let blob = "regras da sessão; PRs acima de 400 linhas devem ter dois revisores; fim";
        let cues: Vec<_> = extract(blob).into_iter().map(|c| (c.kind, c.cue)).collect();
        assert!(
            cues.iter()
                .any(|(k, cue)| *k == CandidateKind::Fact
                    && cue == "declarative:'devem' entity:'PRs'"),
            "devem clause must mine a fact candidate, got {cues:?}"
        );
        // The byte-identical singular stays green (regression guard).
        let singular = extract("a API deve ter dois revisores");
        assert_eq!(singular.len(), 1);
        assert_eq!(singular[0].kind, CandidateKind::Fact);
        assert_eq!(singular[0].cue, "declarative:'deve' entity:'API'");
    }

    #[test]
    fn q107_declarative_pt_number_pairs_fire_both_directions() {
        // Every swept singular/plural pair, both directions — each case
        // carries an entity anchor (F2). The singular rows are the green
        // baseline; the plural rows were the silent drops before q107.
        let cases = [
            (
                "o deploy da API foi manual",
                "declarative:'foi' entity:'API'",
            ),
            (
                "os deploys da API foram manuais",
                "declarative:'foram' entity:'API'",
            ),
            (
                "o schema da API era estável",
                "declarative:'era' entity:'API'",
            ),
            (
                "os schemas da API eram estáveis",
                "declarative:'eram' entity:'API'",
            ),
            (
                "o job da API requer 2 revisores",
                "declarative:'requer' entity:'API'",
            ),
            (
                "os jobs da API requerem 2 revisores",
                "declarative:'requerem' entity:'API'",
            ),
            (
                "o build da API depende de cache",
                "declarative:'depende de' entity:'API'",
            ),
            (
                "os builds da API dependem de cache",
                "declarative:'dependem de' entity:'API'",
            ),
            (
                "o erro 500 significa timeout",
                "declarative:'significa' entity:'500'",
            ),
            (
                "os erros 500 significam timeout",
                "declarative:'significam' entity:'500'",
            ),
            (
                "o DNS aponta para 10.0.0.1",
                "declarative:'aponta para' entity:'DNS'",
            ),
            (
                "os registros DNS apontam para 10.0.0.1",
                "declarative:'apontam para' entity:'DNS'",
            ),
            (
                "o zayout roda em 4320",
                "declarative:'roda em' entity:'4320'",
            ),
            (
                "os servidores rodam em 4320",
                "declarative:'rodam em' entity:'4320'",
            ),
            (
                "o repo pertence a AUTH",
                "declarative:'pertence a' entity:'AUTH'",
            ),
            (
                "os repos pertencem a AUTH",
                "declarative:'pertencem a' entity:'AUTH'",
            ),
            (
                "o BLOB contém 3 chaves",
                "declarative:'contém' entity:'BLOB'",
            ),
            (
                "os BLOBs contêm 3 chaves",
                "declarative:'contêm' entity:'BLOBs'",
            ),
            (
                "a API deve ter dois revisores",
                "declarative:'deve' entity:'API'",
            ),
            (
                "os PRs da API devem ter dois revisores",
                "declarative:'devem' entity:'PRs'",
            ),
        ];
        for (text, cue) in cases {
            let candidates = extract(text);
            assert_eq!(
                candidates.len(),
                1,
                "want one candidate from {text:?}, got {candidates:?}"
            );
            assert_eq!(
                candidates[0].kind,
                CandidateKind::Fact,
                "kind drifted for {text:?}"
            );
            assert_eq!(candidates[0].cue, cue, "cue drifted for {text:?}");
        }
    }

    #[test]
    fn q107_decision_pt_gender_pairs_fire_both_directions() {
        // The `-ado/-ada` participle gender pairs: the feminine rows join
        // their masculines so a choice recorded in the feminine is a
        // decision, not a bare `foi` fact.
        let cases = [
            ("o padrão foi adotado", "decision-word:'adotado'"),
            (
                "a política foi adotada pela equipe",
                "decision-word:'adotada'",
            ),
            ("o plano foi decidido", "decision-word:'decidido'"),
            ("a migração foi decidida", "decision-word:'decidida'"),
        ];
        for (text, cue) in cases {
            let candidates = extract(text);
            assert_eq!(
                candidates.len(),
                1,
                "want one candidate from {text:?}, got {candidates:?}"
            );
            assert_eq!(
                candidates[0].kind,
                CandidateKind::Decision,
                "kind drifted for {text:?}"
            );
            assert_eq!(candidates[0].cue, cue, "cue drifted for {text:?}");
        }
    }

    #[test]
    fn q108_declarative_gate_is_an_entity_anchor_broader_than_caps_or_number() {
        // Row q108 example — FIRES via an ALL-CAPS acronym anchor.
        let fires = extract("A API é lenta");
        assert_eq!(fires.len(), 1);
        assert_eq!(fires[0].kind, CandidateKind::Fact);
        assert_eq!(fires[0].cue, "declarative:'é' entity:'API'");
        // OMITS — same cue, no anchor: the noise fence, never a guessed fact.
        assert!(extract("o sistema é resiliente").is_empty());
        // The anchor is BROADER than "ALL-CAPS acronym or number" (the shape
        // the row named): a mixed-case internal capital, a path/symbol shape,
        // and a backtick code span each qualify — the honest gate documented
        // on the extract description.
        let anchors = [
            ("o backend é SQLite", "declarative:'é' entity:'SQLite'"),
            (
                "o log fica em /var/log/x",
                "declarative:'fica em' entity:'/var/log/x'",
            ),
            ("o binário é `nsh`", "declarative:'é' entity:'`nsh`'"),
        ];
        for (text, cue) in anchors {
            let c = extract(text);
            assert_eq!(c.len(), 1, "want one candidate from {text:?}, got {c:?}");
            assert_eq!(c[0].kind, CandidateKind::Fact);
            assert_eq!(c[0].cue, cue, "cue drifted for {text:?}");
        }
    }

    #[test]
    fn portuguese_cues_fire_like_their_english_twins() {
        // w1d stress fix: bilingual notes silently produced nothing.
        let cases = [
            (
                "Decisão: o zayout sobe sempre via tailnet, porta 4320.",
                CandidateKind::Decision,
                "decision-word:'decisão'",
            ),
            (
                "decidimos usar Postgres 16 no staging",
                CandidateKind::Decision,
                "decision-word:'decidimos'",
            ),
            (
                "Nunca faça deploy às sextas-feiras",
                CandidateKind::Procedure,
                "imperative-opener:'nunca'",
            ),
            (
                "Sempre reinicie o listener depois de rotacionar o token.",
                CandidateKind::Procedure,
                "imperative-opener:'sempre'",
            ),
            (
                "Não rode `cargo sqlx prepare` sem o banco de teste de pé",
                CandidateKind::Procedure,
                "imperative-opener:'não'",
            ),
            (
                "o freeze do zayout é o padrão da w1",
                CandidateKind::Fact,
                "declarative:'é' entity:'w1'",
            ),
        ];
        for (text, kind, cue) in cases {
            let candidates = extract(text);
            assert_eq!(candidates.len(), 1, "no candidate from {text:?}");
            assert_eq!(candidates[0].kind, kind, "kind drifted for {text:?}");
            assert_eq!(candidates[0].cue, cue, "cue drifted for {text:?}");
        }
    }

    #[test]
    fn sentences_within_a_prose_line_are_analyzed_independently() {
        // w1d stress fix: one paragraph collapsed into a single
        // four-sentence "decision" (junk included).
        let paragraph = "We decided to pin Rust at 1.79 for the w1 train. \
                         Always run `cargo deny check` before tagging. \
                         The ledger keeps ninety days of snapshots. \
                         Weather was nice today.";
        let candidates = extract(paragraph);
        let contents: Vec<&str> = candidates.iter().map(|c| c.content.as_str()).collect();
        assert_eq!(
            contents,
            vec![
                "We decided to pin Rust at 1.79 for the w1 train.",
                "Always run `cargo deny check` before tagging.",
            ],
            "sentence-level candidates; sentences with no cue/entity yield none"
        );
        assert_eq!(candidates[0].kind, CandidateKind::Decision);
        assert_eq!(candidates[1].kind, CandidateKind::Procedure);
        // Abbreviations never split.
        let one = extract("use tabs instead of spaces, e.g. Makefile targets");
        assert_eq!(one.len(), 1);
        assert_eq!(
            one[0].content,
            "use tabs instead of spaces, e.g. Makefile targets"
        );
    }

    #[test]
    fn indented_code_and_call_expressions_are_not_fact_candidates() {
        // w1d stress fix: a pasted traceback line surfaced as a "fact"
        // candidate (declarative:'=' entity:'psycopg.connect(dsn').
        let log = "the import failed at 09:12\n    conn = psycopg.connect(dsn)\n\tretry = True";
        let candidates = extract(log);
        assert!(
            candidates
                .iter()
                .all(|c| !c.content.contains("psycopg") && !c.content.contains("retry")),
            "indented code lines skipped, got {candidates:#?}"
        );
        // Unindented call expressions cannot be entities either.
        assert!(extract("conn = psycopg.connect(dsn)").is_empty());
        // Deeply indented list dress still extracts.
        let nested = extract("      - run cargo fmt before every commit");
        assert_eq!(nested.len(), 1);
        assert_eq!(nested[0].kind, CandidateKind::Procedure);
    }

    #[test]
    fn goal_colon_lines_are_procedures() {
        // w1d: "to X: run Y then verify Z" is how-to shaped despite the
        // non-imperative opener; PT "para X: …" too.
        let en = extract("to redeploy staging: run `make deploy-staging` and then verify healthz");
        assert_eq!(en.len(), 1);
        assert_eq!(en[0].kind, CandidateKind::Procedure);
        assert_eq!(en[0].cue, "goal-colon:'to …:'");
        let pt = extract("para religar o worker: rode `systemctl restart worker` e confira o log");
        assert_eq!(pt.len(), 1);
        assert_eq!(pt[0].kind, CandidateKind::Procedure);
    }

    #[test]
    fn modal_standing_rules_are_fact_candidates() {
        // w1d: "X must stay OFF until FY26" carried no candidate at all.
        let candidates =
            extract("Feature flag new_tax_engine must stay OFF in prod until FY26 close");
        assert_eq!(candidates.len(), 1);
        assert_eq!(candidates[0].kind, CandidateKind::Fact);
        assert!(candidates[0].cue.starts_with("declarative:'must'"));
    }

    #[test]
    fn rule_dense_semicolon_bullets_each_surface_a_candidate() {
        // w2-fix (q8 remainder): every bullet of a rule-dense Gotchas
        // section yields at least one candidate — S1c splits `; ` clauses,
        // "serialize" is an imperative opener, and F1b catches the
        // mid-segment prescriptive standing rule.
        let gotchas = "## Gotchas\n\
             - sqlite WAL mode must be enabled before the first write or reads block.\n\
             - rmcp races pipelined frames; serialize strictly one request in flight.\n\
             - anchors are advisory; the existence probe never blocks a capture.\n";
        let candidates = extract(gotchas);
        let cues: Vec<&str> = candidates.iter().map(|c| c.cue.as_str()).collect();
        assert!(
            cues.iter().any(|c| c.starts_with("declarative:'must'")),
            "WAL bullet must stay a fact, got {cues:?}"
        );
        assert!(
            cues.contains(&"imperative-opener:'serialize'"),
            "serialize clause must be a procedure, got {cues:?}"
        );
        assert!(
            cues.contains(&"prescriptive:'never'"),
            "prescriptive clause must be a fact (F1b), got {cues:?}"
        );
        // Each of the three bullets contributed: the serialize and
        // prescriptive candidates are clause-level verbatim substrings.
        let contents: Vec<&str> = candidates.iter().map(|c| c.content.as_str()).collect();
        assert!(contents.contains(&"serialize strictly one request in flight."));
        assert!(contents.contains(&"the existence probe never blocks a capture."));
    }

    #[test]
    fn procedure_label_and_howto_openers_derive_procedure() {
        // w2-fix: the procedure kind's own label word derives it, in both
        // languages, like every sibling kind's label cue.
        let cases = [
            (
                "Procedure: start the nmemory server with --db <path>",
                "procedure-label:'procedure'",
            ),
            (
                "procedimento: religar o listener do runner",
                "procedure-label:'procedimento'",
            ),
            (
                "How to redeploy staging without downtime",
                "how-to-opener:'how to'",
            ),
            (
                "Steps to rotate the hmac key safely",
                "how-to-opener:'steps to'",
            ),
            (
                "como fazer o backup do sqlite em prod",
                "how-to-opener:'como fazer'",
            ),
        ];
        for (line, want_cue) in cases {
            let candidates = extract(line);
            assert_eq!(candidates.len(), 1, "{line:?} → {candidates:?}");
            assert_eq!(candidates[0].kind, CandidateKind::Procedure, "{line:?}");
            assert_eq!(candidates[0].cue, want_cue, "{line:?}");
        }
        // The authored label outranks the decision SHAPE (module doc
        // ladder: Procedure-label > Decision)…
        let labeled = extract("procedure: we chose to restart X then verify Y");
        assert_eq!(labeled.len(), 1);
        assert_eq!(labeled[0].kind, CandidateKind::Procedure);
        // …while a work-plane label still outranks the procedure label.
        let task = extract("todo: procedure: document the restart");
        assert_eq!(task[0].kind, CandidateKind::Task);
    }

    #[test]
    fn chat_prefixed_lines_reach_the_work_plane_labels() {
        // w2-fix (S3c): a realistic chat/log prefix `[10:05] name: ` is
        // neutral dress — the start-anchored work-plane labels fire like
        // their bare-line forms.
        let log = "[10:02] tiago: decisão: ficamos com sqlite WAL\n\
             [10:05] tiago: TODO: migrar o cron do backup para systemd timers ainda esta semana.\n\
             [10:06] ana: ideia: cachear o digest na sessão\n\
             [10:07] bob: epic: consolidar os runbooks de deploy\n\
             [10:08] ana: runbook: religar o listener em duas etapas\n";
        let candidates = extract(log);
        let kinds: Vec<CandidateKind> = candidates.iter().map(|c| c.kind).collect();
        assert!(kinds.contains(&CandidateKind::Decision), "{candidates:?}");
        assert!(kinds.contains(&CandidateKind::Task), "{candidates:?}");
        assert!(kinds.contains(&CandidateKind::Brainstorm), "{candidates:?}");
        assert!(kinds.contains(&CandidateKind::Epic), "{candidates:?}");
        assert!(kinds.contains(&CandidateKind::Doc), "{candidates:?}");
        // The stripped content drops the chat dress but stays verbatim.
        assert!(
            candidates
                .iter()
                .any(|c| c.content.starts_with("TODO: migrar o cron")),
            "{candidates:?}"
        );

        // A tag word that IS a cue word is never stripped as a speaker…
        let bare_label = extract("[10:09] todo: revisar o PR aberto");
        assert_eq!(bare_label[0].kind, CandidateKind::Task);
        assert_eq!(bare_label[0].cue, "todo-word:'todo'");
        // …a digit-free bracket is content, not a stamp…
        let warn = extract("[warn] disk usage is at 91 percent");
        assert_eq!(warn.len(), 1, "{warn:?}");
        assert!(warn[0].content.starts_with("[warn]"), "{warn:?}");
        // …and without a stamp, `word:` openers keep their label reading.
        let no_stamp = extract("runbook: religar o listener em duas etapas");
        assert_eq!(no_stamp[0].kind, CandidateKind::Doc);
    }

    #[test]
    fn candidate_kind_wire_names_round_trip() {
        // Closed set of TEN: the donor W1 triple (bytes unchanged), the
        // w2 work-plane kinds, and the u-r11 governance kinds — snake_case
        // wire names (`failure_pattern` is the first where snake_case and
        // lowercase differ), serde/as_str agreeing, unknowns rejected.
        let expected_wire = [
            (CandidateKind::Fact, "\"fact\""),
            (CandidateKind::Procedure, "\"procedure\""),
            (CandidateKind::Decision, "\"decision\""),
            (CandidateKind::Task, "\"task\""),
            (CandidateKind::Epic, "\"epic\""),
            (CandidateKind::Brainstorm, "\"brainstorm\""),
            (CandidateKind::Doc, "\"doc\""),
            (CandidateKind::Constraint, "\"constraint\""),
            (CandidateKind::Capability, "\"capability\""),
            (CandidateKind::FailurePattern, "\"failure_pattern\""),
        ];
        for (kind, expected) in expected_wire {
            let json = serde_json::to_string(&kind).unwrap();
            assert_eq!(json, expected);
            let back: CandidateKind = serde_json::from_str(&json).unwrap();
            assert_eq!(back, kind);
            assert_eq!(kind.as_str(), &expected[1..expected.len() - 1]);
        }
        // ALL is exactly the pinned set, in declaration order.
        assert_eq!(CandidateKind::ALL.len(), 10);
        assert_eq!(CandidateKind::ALL, expected_wire.map(|(kind, _)| kind));
        assert!(serde_json::from_str::<CandidateKind>("\"episode\"").is_err());
        assert!(serde_json::from_str::<CandidateKind>("\"Task\"").is_err());
        // The deliberate NON-kinds stay rejected (PRD R11: witnesses +
        // provenance are the proof; outcome is the out-<n> record class).
        assert!(serde_json::from_str::<CandidateKind>("\"proof\"").is_err());
        assert!(serde_json::from_str::<CandidateKind>("\"outcome\"").is_err());
    }

    // ── w2-kinds: work-plane cue rules, positive + negative per kind ────

    #[test]
    fn task_cues_fire_positive_and_negative() {
        // T1 — the unchecked checkbox is authored open-work evidence; the
        // dress is stripped but recorded.
        let t1 = extract("- [ ] run the migration on staging");
        assert_eq!(t1.len(), 1);
        assert_eq!(t1[0].kind, CandidateKind::Task);
        assert_eq!(t1[0].cue, "todo-checkbox:'[ ]'");
        assert_eq!(t1[0].content, "run the migration on staging");
        // T2 — EN and PT todo words (first-word anchored).
        let t2 = extract("TODO: wire the exporter to nSHIP");
        assert_eq!(t2.len(), 1);
        assert_eq!(t2[0].kind, CandidateKind::Task);
        assert_eq!(t2[0].cue, "todo-word:'todo'");
        let pt = extract("Pendente: revisar o PR de import");
        assert_eq!(pt.len(), 1);
        assert_eq!(pt[0].kind, CandidateKind::Task);
        assert_eq!(pt[0].cue, "todo-word:'pendente'");
        // T3 — the PT label with the shared separator anchor.
        let t3 = extract("A fazer: rodar o backfill de embeddings");
        assert_eq!(t3.len(), 1);
        assert_eq!(t3[0].kind, CandidateKind::Task);
        assert_eq!(t3[0].cue, "todo-label:'a fazer'");
        // Negative: a CHECKED box is done work — neutral dress, the body
        // decides (stays the W1 procedure).
        let done = extract("- [x] run cargo fmt");
        assert_eq!(done.len(), 1);
        assert_eq!(done[0].kind, CandidateKind::Procedure);
        // Negative: "todos" is not "todo" (word boundary), and this
        // segment carries no other cue/entity — honest zero.
        assert!(extract("todos os agentes usam o spool").is_empty());
    }

    #[test]
    fn task_kind_own_label_derives_in_both_languages() {
        // q85: the task kind's OWN name word (`task:` / `Tarefa:`) derives
        // task on the extract surface, in EN and PT — the exact defect
        // class q64 fixed for the procedure kind. The label form reaches
        // rule T2 because [`words`] splits on the `:` separator, so the
        // first token is the bare marker.
        let c7 =
            extract("task: migrar o parser de manifesto para streaming antes do corte de sexta");
        assert_eq!(c7.len(), 1);
        assert_eq!(c7[0].kind, CandidateKind::Task);
        assert_eq!(c7[0].cue, "todo-word:'task'");
        let c1 = extract("Tarefa: revisar a política de retenção de sessões antigas");
        assert_eq!(c1.len(), 1);
        assert_eq!(c1[0].kind, CandidateKind::Task);
        assert_eq!(c1[0].cue, "todo-word:'tarefa'");
        // Negative: the word boundary holds — "tasks"/"tarefas" are not the
        // markers, and these segments carry no other cue/entity.
        assert!(extract("tasks were assigned across the team").is_empty());
        assert!(extract("tarefas ficaram para a próxima sprint").is_empty());
    }

    #[test]
    fn semicolon_clause_candidate_trims_trailing_separator() {
        // O2: a `; `-split clause is verbatim EXCEPT the trailing `;`
        // separator, which is punctuation between clauses, not content.
        let candidates = extract("epic: unify the recall metric; task: land the schema fix");
        let contents: Vec<&str> = candidates.iter().map(|c| c.content.as_str()).collect();
        // The first clause carried the `;` before O2; now it is trimmed.
        assert!(
            contents.contains(&"epic: unify the recall metric"),
            "leading clause must drop its trailing ';': {contents:?}"
        );
        assert!(
            !contents.iter().any(|c| c.ends_with(';')),
            "no candidate keeps a trailing ';': {contents:?}"
        );
    }

    #[test]
    fn epic_cues_fire_positive_and_negative() {
        let en = extract("Epic: memory work plane for W2");
        assert_eq!(en.len(), 1);
        assert_eq!(en[0].kind, CandidateKind::Epic);
        assert_eq!(en[0].cue, "epic-label:'epic'");
        // PT label with the em-dash separator variant.
        let pt = extract("Iniciativa — consolidar os runbooks de deploy");
        assert_eq!(pt.len(), 1);
        assert_eq!(pt[0].kind, CandidateKind::Epic);
        assert_eq!(pt[0].cue, "epic-label:'iniciativa'");
        let ms = extract("Milestone: fleet-2 com zero fricção");
        assert_eq!(ms.len(), 1);
        assert_eq!(ms[0].kind, CandidateKind::Epic);
        assert_eq!(ms[0].cue, "epic-label:'milestone'");
        // Negative: an unlabeled mid-sentence "epic" is no grouping
        // construct (and carries no other cue/entity)...
        assert!(extract("that epic refactor was painful").is_empty());
        // ...and the separator anchor is the word boundary: "epical:" is
        // not "epic:".
        assert!(extract("epical: not a real label word").is_empty());
    }

    #[test]
    fn brainstorm_cues_fire_positive_and_negative() {
        // B1 — idea labels, PT and EN.
        let b1 = extract("Ideia: usar WAL no sqlite para o spool");
        assert_eq!(b1.len(), 1);
        assert_eq!(b1[0].kind, CandidateKind::Brainstorm);
        assert_eq!(b1[0].cue, "idea-label:'ideia'");
        // B2 — open-exploration questions survive the S4 question guard
        // through the S4b gate.
        let b2 = extract("what if we cache the digest per session?");
        assert_eq!(b2.len(), 1);
        assert_eq!(b2[0].kind, CandidateKind::Brainstorm);
        assert_eq!(b2[0].cue, "open-question:'what if'");
        let pt = extract("E se a gente projetar o dag por kind?");
        assert_eq!(pt.len(), 1);
        assert_eq!(pt[0].kind, CandidateKind::Brainstorm);
        assert_eq!(pt[0].cue, "open-question:'e se'");
        // B2 fires without the question mark too — the opener is the cue.
        let flat = extract("could we precompute the ready set");
        assert_eq!(flat.len(), 1);
        assert_eq!(flat[0].kind, CandidateKind::Brainstorm);
        assert_eq!(flat[0].cue, "open-question:'could we'");
        // Negative: a non-exploration question is still not a claim (S4).
        assert!(extract("do we need the spool?").is_empty());
        // Negative: opener needs its word boundary ("what iffy...").
        assert!(extract("what iffy weather we got").is_empty());
        // Negative: mid-sentence "idea" is not a label.
        assert!(extract("the idea needs more work").is_empty());
    }

    #[test]
    fn doc_cues_fire_positive_and_negative() {
        let en = extract("Runbook: como religar o listener depois de um deploy");
        assert_eq!(en.len(), 1);
        assert_eq!(en[0].kind, CandidateKind::Doc);
        assert_eq!(en[0].cue, "doc-label:'runbook'");
        let dash = extract("Reference — spool file format details");
        assert_eq!(dash.len(), 1);
        assert_eq!(dash[0].kind, CandidateKind::Doc);
        assert_eq!(dash[0].cue, "doc-label:'reference'");
        let pt = extract("Documentação: fluxo de ingest com spool e fsync");
        assert_eq!(pt.len(), 1);
        assert_eq!(pt[0].kind, CandidateKind::Doc);
        assert_eq!(pt[0].cue, "doc-label:'documentação'");
        // Negative: unlabeled "docs" mid-shape is not a doc (and has no
        // entity for the fact frame)...
        assert!(extract("docs are outdated here").is_empty());
        // ...and imperatives ABOUT docs stay procedures (existing cue
        // untouched by w2-kinds).
        let imp = extract("update the docs after every schema bump");
        assert_eq!(imp.len(), 1);
        assert_eq!(imp[0].kind, CandidateKind::Procedure);
        assert_eq!(imp[0].cue, "imperative-opener:'update'");
    }

    #[test]
    fn work_plane_labels_outrank_shape_cues() {
        // The unchecked checkbox outranks the decision phrase in its body:
        // an open task that DESCRIBES a choice is still open work.
        let t = extract("- [ ] use tabs instead of spaces");
        assert_eq!(t.len(), 1);
        assert_eq!(t[0].kind, CandidateKind::Task);
        assert_eq!(t[0].cue, "todo-checkbox:'[ ]'");
        // An epic label outranks the decision word in its body...
        let e = extract("Epic: decided scope for the auth revamp");
        assert_eq!(e.len(), 1);
        assert_eq!(e[0].kind, CandidateKind::Epic);
        // ...and WITHOUT the label the same body is the W1 decision —
        // existing cues untouched.
        let d = extract("decided scope for the auth revamp");
        assert_eq!(d.len(), 1);
        assert_eq!(d[0].kind, CandidateKind::Decision);
        // A checkboxed QUESTION goes through the S4b gate first:
        // exploration on a todo list is brainstorm — and with no
        // open-exploration shape it falls back to the S4c filed task
        // (its own test carries the full matrix).
        let q = extract("- [ ] should we split the store?");
        assert_eq!(q.len(), 1);
        assert_eq!(q[0].kind, CandidateKind::Brainstorm);
        assert_eq!(q[0].cue, "open-question:'should we'");
        assert_eq!(
            extract("- [ ] is this even needed?")[0].kind,
            CandidateKind::Task
        );
    }

    // ── u-r11: constraint / capability / failure_pattern cues ───────────

    #[test]
    fn constraint_labels_and_prohibition_frames_mine_constraint() {
        // Rule CN1: the kind's own label words, EN + PT (both diacritic
        // spellings), through the shared `labeled` anchor.
        for (line, cue) in [
            (
                "constraint: one embedder per store",
                "constraint-label:'constraint'",
            ),
            (
                "restrição — não usar rede em runtime",
                "constraint-label:'restrição'",
            ),
            (
                "restricao: capsule v1 fica congelada",
                "constraint-label:'restricao'",
            ),
        ] {
            let got = extract(line);
            assert_eq!(got.len(), 1, "{line:?} must mine exactly one: {got:?}");
            assert_eq!(got[0].kind.as_str(), "constraint", "{line:?}");
            assert_eq!(got[0].cue, cue, "{line:?}");
        }
        // Rule CN2: negated-modal prohibition frames, mid-segment (EN
        // "must not"/"must never", PT number-paired não pode(m)/deve(m)).
        for (line, cue) in [
            ("the API key must not be logged", "prohibition:'must not'"),
            ("o deploy não pode rodar na sexta", "prohibition:'não pode'"),
            (
                "segredos nao devem aparecer em logs",
                "prohibition:'nao devem'",
            ),
        ] {
            let got = extract(line);
            assert_eq!(got.len(), 1, "{line:?} must mine exactly one: {got:?}");
            assert_eq!(got[0].kind.as_str(), "constraint", "{line:?}");
            assert_eq!(got[0].cue, cue, "{line:?}");
        }
        // ...and segment-START prohibitions fire through the same closed
        // list as openers ("não pode subir segredo" opens with the modal).
        let opener = extract("não pode subir segredo pro repo");
        assert_eq!(opener.len(), 1, "{opener:?}");
        assert_eq!(opener[0].kind.as_str(), "constraint");
    }

    #[test]
    fn constraint_false_positive_guards_hold() {
        // Plural/substring guard (q107 family): "constraints" without the
        // label separator is not the label.
        for candidate in extract("constraints are hard to design well") {
            assert_ne!(candidate.kind.as_str(), "constraint", "{candidate:?}");
        }
        // The bare prescriptive adverbs stay with their EXISTING rules:
        // first-word "never" is still rule P2 procedure...
        let p2 = extract("never push directly to main");
        assert_eq!(p2.len(), 1);
        assert_eq!(p2[0].kind, CandidateKind::Procedure);
        assert_eq!(p2[0].cue, "imperative-opener:'never'");
        // ...and mid-segment "never" is still rule F1b fact — a standing
        // claim, not a prohibition frame.
        let f1b = extract("the existence probe never blocks a capture");
        assert_eq!(f1b.len(), 1);
        assert_eq!(f1b[0].kind, CandidateKind::Fact);
        assert_eq!(f1b[0].cue, "prescriptive:'never'");
        // A decision word outranks the prohibition frame: choice language
        // is the rarer, more specific signal (existing ladder law).
        let d = extract("decidimos que o deploy não pode rodar na sexta");
        assert_eq!(d.len(), 1);
        assert_eq!(d[0].kind, CandidateKind::Decision);
    }

    #[test]
    fn capability_labels_and_use_when_openers_mine_capability() {
        // Rule CP1: the kind's own label words, EN + PT.
        for (line, cue) in [
            (
                "capability: renders the store as mermaid",
                "capability-label:'capability'",
            ),
            (
                "capacidade — exporta o store inteiro como markdown",
                "capability-label:'capacidade'",
            ),
        ] {
            let got = extract(line);
            assert_eq!(got.len(), 1, "{line:?} must mine exactly one: {got:?}");
            assert_eq!(got[0].kind.as_str(), "capability", "{line:?}");
            assert_eq!(got[0].cue, cue, "{line:?}");
        }
        // Rule CP2: the applicability opener ("use when" / "use quando"),
        // start-anchored like P0b.
        for (line, cue) in [
            (
                "use when the store file is corrupted",
                "use-when:'use when'",
            ),
            (
                "use quando precisar de recall semântico",
                "use-when:'use quando'",
            ),
        ] {
            let got = extract(line);
            assert_eq!(got.len(), 1, "{line:?} must mine exactly one: {got:?}");
            assert_eq!(got[0].kind.as_str(), "capability", "{line:?}");
            assert_eq!(got[0].cue, cue, "{line:?}");
        }
    }

    #[test]
    fn capability_false_positive_guards_hold() {
        // A bare "use ..." imperative stays rule P2 procedure — CP2 needs
        // the full "use when"/"use quando" opener.
        let p2 = extract("use the staging key for smoke tests");
        assert_eq!(p2.len(), 1);
        assert_eq!(p2[0].kind, CandidateKind::Procedure);
        assert_eq!(p2[0].cue, "imperative-opener:'use'");
        // "use tabs instead of spaces" stays the documented D2 decision.
        let d2 = extract("use tabs instead of spaces");
        assert_eq!(d2.len(), 1);
        assert_eq!(d2[0].kind, CandidateKind::Decision);
        assert_eq!(d2[0].cue, "decision-phrase:'instead of'");
        // Mid-segment "use when" never fires — the opener is
        // start-anchored ("we use when-clauses" opens with "we").
        for candidate in extract("we use when-clauses in the SQL layer") {
            assert_ne!(candidate.kind.as_str(), "capability", "{candidate:?}");
        }
        // Plural guard: "capacidades" without the separator is not the
        // label.
        for candidate in extract("capacidades do sistema são amplas") {
            assert_ne!(candidate.kind.as_str(), "capability", "{candidate:?}");
        }
    }

    #[test]
    fn failure_labels_and_failure_frames_mine_failure_pattern() {
        // Rule FP1: failure/symptom label words, EN + PT.
        for (line, cue) in [
            (
                "failure: OOM on exports over 2GB",
                "failure-label:'failure'",
            ),
            (
                "falha: listener morre depois do deploy",
                "failure-label:'falha'",
            ),
            (
                "symptom: retries pile up after restart",
                "failure-label:'symptom'",
            ),
            (
                "sintoma — digest trava com dag cíclico",
                "failure-label:'sintoma'",
            ),
        ] {
            let got = extract(line);
            assert_eq!(got.len(), 1, "{line:?} must mine exactly one: {got:?}");
            assert_eq!(got[0].kind.as_str(), "failure_pattern", "{line:?}");
            assert_eq!(got[0].cue, cue, "{line:?}");
        }
        // Rule FP2: failure frames mid-segment (EN 3rd-person-singular by
        // the q107 EN law; PT number-paired falha(m)/quebra(m)).
        for (line, cue) in [
            (
                "the build fails with OOM after a rebase",
                "failure-frame:'fails with'",
            ),
            (
                "o job falha quando roda em paralelo",
                "failure-frame:'falha quando'",
            ),
            (
                "os workers falham com timeout na fila",
                "failure-frame:'falham com'",
            ),
            (
                "the exporter breaks when the store is empty",
                "failure-frame:'breaks when'",
            ),
        ] {
            let got = extract(line);
            assert_eq!(got.len(), 1, "{line:?} must mine exactly one: {got:?}");
            assert_eq!(got[0].kind.as_str(), "failure_pattern", "{line:?}");
            assert_eq!(got[0].cue, cue, "{line:?}");
        }
    }

    #[test]
    fn failure_pattern_false_positive_guards_hold() {
        // Bare "falha" mid-segment is NOT a cue — only the labeled form
        // and the closed frames fire (never a single loose word).
        for candidate in extract("o teste falha às vezes sem motivo") {
            assert_ne!(candidate.kind.as_str(), "failure_pattern", "{candidate:?}");
        }
        // Plural label guard: "failures"/"falhas" without the separator.
        for candidate in extract("failures are inevitable in distributed systems") {
            assert_ne!(candidate.kind.as_str(), "failure_pattern", "{candidate:?}");
        }
        // "fails with" needs the frame's word boundary: "fails without"
        // must not fire "fails with" (substring guard, q107 family).
        for candidate in extract("the job fails without any log line") {
            assert_ne!(candidate.kind.as_str(), "failure_pattern", "{candidate:?}");
        }
    }
}
