"""LongMemEval pilot benchmark execution and report synthesis."""

from __future__ import annotations

import datetime
import json
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nmemory.capsule import parse_rfc3339
from nmemory.database import STORE_FILE
from nmemory.ingest import IngestDefaults, IngestError, IngestRequest, ingest
from nmemory.store import Store
from nott_database import RefusedError, SchemaError

from nmemory_evals import artifacts, driver, longmemeval, recall, score
from nmemory_evals.artifacts import BENCH_VERSION
from nmemory_evals.driver import Driver, EvaluationMode
from nmemory_evals.longmemeval import Instance
from nmemory_evals.recall import CategoryRecall, PrecisionAtK, RecallAtK, ScoredRow
from nmemory_evals.renamed import env
from nmemory_evals.score import RecallOutcome

SESSION_GRANULARITY: str = "session"
LANES: tuple[str, ...] = ("term", "vector", "fused", "auto")
EXECUTIONS: tuple[str, ...] = ("per_instance", "phased")
METRIC: str = (
    "session-level retrieval recall and precision of the annotated evidence sessions, "
    "measured on one nmemory_capsule_retrieve call per question; NEVER answer accuracy — "
    "no answer generator or judge runs in this benchmark"
)


class PilotError(Exception):
    """Pilot configuration, dataset, execution, or scoring failure."""


@dataclass(frozen=True)
class DatasetPin:
    repository: str
    file: str
    sha256: str
    bytes: int
    license: str


@dataclass(frozen=True)
class PilotConfig:
    bench: str
    dataset: DatasetPin
    slice_first_instances: int
    granularity: str
    lane: str
    k_values: list[int]
    token_budget: int
    project_id: str
    term_extraction: str
    scoring_scope: str
    execution: str = "per_instance"

    def limit(self) -> int:
        if not self.k_values:
            raise PilotError("configuration declares no k value")
        return max(self.k_values)

    def check(self) -> None:
        if self.granularity != SESSION_GRANULARITY:
            raise PilotError(
                f"configuration declares granularity {self.granularity!r}; "
                f"this harness scores {SESSION_GRANULARITY}"
            )
        if 0 in self.k_values:
            raise PilotError("configuration declares k = 0, which retrieves nothing")
        if self.lane not in LANES:
            raise PilotError(
                f"configuration declares lane {self.lane!r}; nmemory_capsule_retrieve accepts {LANES!r}"
            )
        if self.execution not in EXECUTIONS:
            raise PilotError(
                f"configuration declares execution {self.execution!r}; "
                f"this harness accepts {EXECUTIONS!r}"
            )

    def accept_corpus(self, measured: str) -> None:
        if measured != self.dataset.sha256:
            raise PilotError(
                f"dataset digest is {measured}, not the pinned {self.dataset.sha256}"
            )


@dataclass
class InstanceCounts:
    sliced: int
    scored: int
    scored_abstention: int
    largest_category_share: float
    categories_covered: list[str]
    #: Reads 0 since the probes were admitted (N4b); kept so a report from
    #: before (30) and after diff on the same key.
    skipped_abstention: int


@dataclass
class OutcomeCounts:
    grounded: int = 0
    missing_evidence: int = 0
    abstain: int = 0


@dataclass
class ProviderUsage:
    openai_requests: int = 0
    openai_prompt_tokens: int = 0

    @classmethod
    def read(cls, usage: dict[str, Any]) -> ProviderUsage:
        return cls(
            openai_requests=int(usage.get("openai_requests", 0)),
            openai_prompt_tokens=int(usage.get("openai_prompt_tokens", 0)),
        )

    def add(self, other: ProviderUsage) -> None:
        self.openai_requests += other.openai_requests
        self.openai_prompt_tokens += other.openai_prompt_tokens


@dataclass
class Observed:
    sessions_ingested: int = 0
    capsules_captured: int = 0
    capsules_deduplicated: int = 0
    questions_trimmed_by_budget: int = 0
    questions_returning_fewer_than_limit: int = 0
    retrieve_outcomes: OutcomeCounts = field(default_factory=OutcomeCounts)
    excluded: dict[str, int] = field(default_factory=dict)
    results_flagged_instruction_taint: int = 0
    lanes_used: dict[str, int] = field(default_factory=dict)
    usage: ProviderUsage = field(default_factory=ProviderUsage)
    child_usage: ProviderUsage = field(default_factory=ProviderUsage)

    def record_recall(
        self,
        payload: dict[str, Any],
        outcome: RecallOutcome,
        limit: int,
    ) -> list[str]:
        match outcome:
            case RecallOutcome.GROUNDED:
                self.retrieve_outcomes.grounded += 1
            case RecallOutcome.MISSING_EVIDENCE:
                self.retrieve_outcomes.missing_evidence += 1
            case RecallOutcome.ABSTAIN:
                self.retrieve_outcomes.abstain += 1

        for reason, count in excluded_counts(payload):
            self.excluded[reason] = self.excluded.get(reason, 0) + count

        trimmed = payload.get("trimmed_by_budget")
        if (isinstance(trimmed, int) and trimmed > 0) or trimmed is True:
            self.questions_trimmed_by_budget += 1

        self.results_flagged_instruction_taint += tainted_results(payload)
        lane_used = payload.get("lane_used")
        if isinstance(lane_used, str):
            self.lanes_used[lane_used] = self.lanes_used.get(lane_used, 0) + 1

        usage = payload.get("embedding_usage")
        if isinstance(usage, dict):
            self.child_usage = ProviderUsage.read(usage)

        returned = result_ids(payload)
        if len(returned) < limit:
            self.questions_returning_fewer_than_limit += 1

        return returned

    def close_child(self) -> None:
        self.usage.add(self.child_usage)
        self.child_usage = ProviderUsage()


@dataclass
class Report:
    bench: str
    metric: str
    bench_version: str
    run_date_utc: str
    run_unix_seconds: int
    run_seconds: int
    dataset_sha256: str
    binary_sha256: str
    run_project_id: str
    database_mode: EvaluationMode
    config: PilotConfig
    instances: InstanceCounts
    retrieval_recall: list[RecallAtK]
    retrieval_precision: list[PrecisionAtK]
    per_question_type: list[CategoryRecall]
    abstention: score.Abstention
    observed: Observed
    #: Every measured question in dataset order, with what came back up to
    #: k_max (the retrieve limit) and the outcome the store declared. Last on
    #: purpose: the aggregates above read first, this is the raw material.
    scored: list[ScoredRow]


class Pilot:
    """One pilot run over LongMemEval dataset."""

    def __init__(
        self,
        config: PilotConfig,
        dataset: Path | str,
        binary: Path | str,
        run_dir: Path | str,
        store_dir: Path,
    ) -> None:
        self.config = config
        self.dataset = Path(dataset)
        self.binary = Path(binary)
        self.run_dir = Path(run_dir)
        self.store_dir = store_dir

    def run(self) -> Report:
        started = time.monotonic()
        self.config.check()
        limit = self.config.limit()
        dataset_sha256 = artifacts.file_sha256(self.dataset)
        self.config.accept_corpus(dataset_sha256)
        binary_sha256 = artifacts.file_sha256(self.binary)
        instances = longmemeval.load_first(
            self.dataset, self.config.slice_first_instances
        )
        database_mode = driver.evaluation_mode()

        supplied_run_id = env("NOTT_NMEMORY_EVALS_RUN_ID")
        if database_mode == EvaluationMode.REUSE and not supplied_run_id:
            raise PilotError(
                "NOTT_NMEMORY_EVALS_REUSE=1 requires an explicit NOTT_NMEMORY_EVALS_RUN_ID"
            )

        if supplied_run_id:
            if (
                not supplied_run_id
                or len(supplied_run_id) > 64
                or not all(c.isalnum() or c == "-" for c in supplied_run_id)
            ):
                raise PilotError(
                    "NOTT_NMEMORY_EVALS_RUN_ID must be 1..=64 ASCII letters, digits, or hyphens"
                )
            run_id = supplied_run_id
        else:
            now_dt = datetime.datetime.now(datetime.UTC)
            run_id = now_dt.strftime("%Y%m%dT%H%M%SZ")

        run_project_id = f"{self.config.project_id}/{run_id}"
        driver.prepare_database(self.binary, self.store_dir, database_mode)

        measured = list(enumerate(instances))
        observed = Observed()

        if self.config.execution == "phased":
            rows = self.measure_phased(measured, limit, run_project_id, observed)
        else:
            rows = self.measure_per_instance(measured, limit, run_project_id, observed)

        # The abstention probes are ingested and retrieved like every other
        # instance, then scored apart: they never enter a recall or precision
        # denominator, and `rows` keeps them all in dataset order.
        scored: list[ScoredRow] = []
        probes: list[ScoredRow] = []
        for instance, row in zip(instances, rows, strict=True):
            (probes if instance.is_abstention() else scored).append(row)
        true_abstains = sum(
            1 for r in probes if score.is_true_abstain(r.outcome, r.ranked_sessions)
        )
        abstention = score.Abstention(
            true_abstain=true_abstains, false_abstain=len(probes) - true_abstains
        )

        retrieval_recall = recall.table(scored, self.config.k_values)
        retrieval_precision = recall.precision(scored, self.config.k_values)
        per_question_type = recall.by_category(scored, self.config.k_values)
        categories_covered = [c.question_type for c in per_question_type]
        largest_share = largest_category_share(per_question_type, len(scored))

        instance_counts = InstanceCounts(
            sliced=len(instances),
            scored=len(scored),
            scored_abstention=len(probes),
            largest_category_share=largest_share,
            categories_covered=categories_covered,
            skipped_abstention=len(instances) - len(scored) - len(probes),
        )

        now_utc = datetime.datetime.now(datetime.UTC)
        run_date_utc = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        run_unix_seconds = int(now_utc.timestamp())
        run_seconds = int(time.monotonic() - started)

        return Report(
            bench=self.config.bench,
            metric=METRIC,
            bench_version=BENCH_VERSION,
            run_date_utc=run_date_utc,
            run_unix_seconds=run_unix_seconds,
            run_seconds=run_seconds,
            dataset_sha256=dataset_sha256,
            binary_sha256=binary_sha256,
            run_project_id=run_project_id,
            database_mode=database_mode,
            config=self.config,
            instances=instance_counts,
            retrieval_recall=retrieval_recall,
            retrieval_precision=retrieval_precision,
            per_question_type=per_question_type,
            abstention=abstention,
            observed=observed,
            scored=rows,
        )

    def measure_per_instance(
        self,
        measured: list[tuple[int, Instance]],
        limit: int,
        run_project_id: str,
        observed: Observed,
    ) -> list[ScoredRow]:
        scored: list[ScoredRow] = []
        for index, instance in measured:
            scored.append(
                self.measure_single(index, instance, limit, run_project_id, observed)
            )
        return scored

    def measure_single(
        self,
        index: int,
        instance: Instance,
        limit: int,
        run_project_id: str,
        observed: Observed,
    ) -> ScoredRow:
        terms = longmemeval.question_terms(instance.question)
        if not terms:
            raise PilotError(
                f"question {instance.question_id} projects no term; there is nothing to retrieve with"
            )

        instance_dir = self.run_dir / f"instance-{index:03d}"
        instance_dir.mkdir(parents=True, exist_ok=True)
        project_id = f"{run_project_id}/instance-{index:03d}"
        sessions_by_capsule = self.seed_sessions(instance, project_id, observed)

        driver_inst = Driver.spawn(
            self.binary,
            instance_dir,
            self.store_dir,
            run_project_id,
            self.config.lane != "term",
        )
        driver_inst.handshake()

        next_req = [2]
        scope_res = driver_inst.request(
            driver.take_request_id(next_req),
            "tools/call",
            self.scope_request(instance, run_project_id, project_id),
        )
        topic_id = capture_id(driver.tool_payload(scope_res), instance.question_id)

        for capsule_id in sessions_by_capsule:
            relate_res = driver_inst.request(
                driver.take_request_id(next_req),
                "tools/call",
                {
                    "name": "nmemory_relation_add",
                    "arguments": {
                        "kind": "about",
                        "from": capsule_id,
                        "to": topic_id,
                    },
                },
            )
            driver.tool_payload(relate_res)

        if self.config.lane != "term":
            driver.wait_for_semantic(self.binary, self.store_dir)

        retrieve_res = driver_inst.request(
            driver.take_request_id(next_req),
            "tools/call",
            self.retrieve_request(terms, limit, topic_id),
        )
        payload = driver.tool_payload(retrieve_res)
        driver_inst.finish()

        lane_used = payload.get("lane_used", "missing")
        if self.config.lane != "auto" and lane_used != self.config.lane:
            raise PilotError(
                f"configuration requested lane {self.config.lane!r}, but the server ran {lane_used!r}"
            )

        outcome = driver.recall_outcome(payload, instance.question_id)
        returned = observed.record_recall(payload, outcome, limit)
        observed.close_child()
        return scored_row(instance, outcome, returned, sessions_by_capsule)

    def measure_phased(
        self,
        measured: list[tuple[int, Instance]],
        limit: int,
        run_project_id: str,
        observed: Observed,
    ) -> list[ScoredRow]:
        store_dir = self.run_dir / "phased"
        store_dir.mkdir(parents=True, exist_ok=True)
        # Every haystack is in the store before the one child opens it.
        seeded = [
            self.seed_sessions(
                instance, f"{run_project_id}/instance-{index:03d}", observed
            )
            for index, instance in measured
        ]

        driver_inst = Driver.spawn(
            self.binary,
            store_dir,
            self.store_dir,
            run_project_id,
            self.config.lane != "term",
        )
        driver_inst.handshake()
        next_req = [2]

        @dataclass
        class Prepared:
            instance: Instance
            terms: list[str]
            topic_id: str
            sessions_by_capsule: dict[str, list[str]]

        prepared_list: list[Prepared] = []
        for (index, instance), sessions_by_capsule in zip(
            measured, seeded, strict=True
        ):
            terms = longmemeval.question_terms(instance.question)
            if not terms:
                raise PilotError(
                    f"question {instance.question_id} projects no term; there is nothing to retrieve with"
                )
            project_id = f"{run_project_id}/instance-{index:03d}"
            scope_res = driver_inst.request(
                driver.take_request_id(next_req),
                "tools/call",
                self.scope_request(instance, run_project_id, project_id),
            )
            topic_id = capture_id(driver.tool_payload(scope_res), instance.question_id)

            for capsule_id in sessions_by_capsule:
                relate_res = driver_inst.request(
                    driver.take_request_id(next_req),
                    "tools/call",
                    {
                        "name": "nmemory_relation_add",
                        "arguments": {
                            "kind": "about",
                            "from": capsule_id,
                            "to": topic_id,
                        },
                    },
                )
                driver.tool_payload(relate_res)

            prepared_list.append(
                Prepared(
                    instance=instance,
                    terms=terms,
                    topic_id=topic_id,
                    sessions_by_capsule=sessions_by_capsule,
                )
            )

        if self.config.lane != "term":
            driver.wait_for_semantic(self.binary, self.store_dir)

        scored: list[ScoredRow] = []
        for item in prepared_list:
            retrieve_res = driver_inst.request(
                driver.take_request_id(next_req),
                "tools/call",
                self.retrieve_request(item.terms, limit, item.topic_id),
            )
            payload = driver.tool_payload(retrieve_res)
            lane_used = payload.get("lane_used", "missing")
            if self.config.lane != "auto" and lane_used != self.config.lane:
                raise PilotError(
                    f"configuration requested lane {self.config.lane!r}, but the server ran {lane_used!r}"
                )

            outcome = driver.recall_outcome(payload, item.instance.question_id)
            returned = observed.record_recall(payload, outcome, limit)
            scored.append(
                scored_row(item.instance, outcome, returned, item.sessions_by_capsule)
            )

        driver_inst.finish()
        observed.close_child()
        return scored

    def seed_sessions(
        self, instance: Instance, project_id: str, observed: Observed
    ) -> dict[str, list[str]]:
        """Load one haystack into the benchmark store: each capsule id and its sessions.

        The sessions are the dataset's bytes, not prose an agent wrote, so they
        enter as nMEMORY's own tests seed a store: through its capture function
        `nmemory.ingest.ingest`, the one `nmemory_capsule_import` uses for external
        files, and not through `nmemory_capsule_ingest`, whose English check for
        agent captures refuses 20 of the dataset's 23,867 sessions.
        Every capsule, new or deduplicated, then gets what that tool wrote after
        each capture: kind `fact` and this session's proof, so a store REUSE kept
        scores as it did under the tool. The binary under test still relates and
        retrieves every one.
        """
        # ponytail: the workspace's `nmemory` captures, not the binary under test;
        # a capture-side change is measured only from a checkout of the same code.
        path = self.store_dir / STORE_FILE
        try:
            store = Store.open(path)
        except (RefusedError, SchemaError) as err:
            raise PilotError(f"benchmark store {path} cannot be seeded: {err}") from err
        now = datetime.datetime.now(datetime.UTC)
        sessions_by_capsule: dict[str, list[str]] = {}
        with store.db.write_boundary():
            for session in instance.sessions():
                try:
                    # The loader checks a date's shape, not the calendar.
                    valid_from = parse_rfc3339(session.valid_from)
                except ValueError as err:
                    raise PilotError(
                        f"session {session.session_id} has no real date "
                        f"{session.valid_from}: {err}"
                    ) from err
                request = IngestRequest(
                    content=session.content,
                    source=f"longmemeval-s/{self.config.dataset.file}",
                    anchor=f"session/{session.session_id}",
                    valid_from=valid_from,
                    project_id=project_id,
                    hints=False,
                )
                try:
                    outcome = ingest(store, request, IngestDefaults(project_id), now)
                except IngestError as err:
                    raise PilotError(
                        f"ingest of session {session.session_id} was refused: {err}"
                    ) from err
                # As `nmemory_capsule_ingest` did (`_persist_ingest_kinds` and
                # `_persist_ingest_epistemics`): a deduplicated capsule is a fact
                # again, and the latest session names its proof.
                if outcome.deduped:
                    observed.capsules_deduplicated += 1
                    store.set_classification(outcome.id, "fact", "project", now)
                else:
                    observed.capsules_captured += 1
                    store.set_birth_classification(outcome.id, "fact", "project", now)
                store.epistemics.set_epistemic(
                    outcome.id,
                    evidence_state=None,
                    proof_hint=self.proof_command(session),
                    stale_if=None,
                    now=now,
                )
                observed.sessions_ingested += 1
                sessions_by_capsule.setdefault(outcome.id, []).append(
                    session.session_id
                )
        return sessions_by_capsule

    def proof_command(self, session: longmemeval.Session) -> str:
        """The command that prints this session back from the pinned dataset."""
        return shlex.join(
            [
                "nmemory-evals",
                "session",
                "show",
                "--data",
                str(self.dataset.resolve()),
                "--sha256",
                self.config.dataset.sha256,
                "--question",
                session.question_id,
                "--position",
                str(session.position),
                "--session",
                session.session_id,
            ]
        )

    def scope_request(
        self, instance: Instance, run_project_id: str, project_id: str
    ) -> dict[str, Any]:
        clean_project = run_project_id.replace("/", "_").replace("-", "_")
        return {
            "name": "nmemory_capsule_ingest",
            "arguments": {
                "content": (
                    f"lme_scope_{self.config.dataset.sha256}_{clean_project}_{instance.question_id}"
                ),
                "source": "nmemory-evals:longmemeval-scope",
                "anchor": f"scope/{instance.question_id}",
                "project_id": project_id,
                "kind": "doc",
                "valid_from": "1970-01-01T00:00:00Z",
                "valid_to": "1970-01-01T00:00:01Z",
            },
        }

    def retrieve_request(
        self, terms: list[str], limit: int, topic_id: str
    ) -> dict[str, Any]:
        return {
            "name": "nmemory_capsule_retrieve",
            "arguments": {
                "terms": terms,
                "topic_id": topic_id,
                "lane": self.config.lane,
                "limit": limit,
                "token_budget": self.config.token_budget,
            },
        }


def scored_row(
    instance: Instance,
    outcome: RecallOutcome,
    returned: list[str],
    sessions_by_capsule: dict[str, list[str]],
) -> ScoredRow:
    """The persisted row of one question: each returned capsule mapped back to its sessions."""
    ranked_sessions: list[list[str]] = []
    for capsule_id in returned:
        sess_list = sessions_by_capsule.get(capsule_id)
        if sess_list is None:
            raise PilotError(
                f"recall for {instance.question_id} returned capsule {capsule_id}, "
                "which this store never ingested"
            )
        ranked_sessions.append(sess_list)
    return ScoredRow(
        question_id=instance.question_id,
        question_type=instance.question_type,
        evidence_sessions=instance.answer_session_ids,
        ranked_sessions=ranked_sessions,
        outcome=outcome,
    )


def capture_id(payload: dict[str, Any], label: str) -> str:
    outcomes = rows(payload, "outcomes")
    if not outcomes:
        raise PilotError(f"ingest of session {label} returned no capsule id")
    outcome = outcomes[0]
    status = outcome.get("status")
    if status in ("captured", "deduplicated"):
        cid = outcome.get("id")
        if cid:
            return str(cid)
        raise PilotError(f"ingest of session {label} returned no capsule id")
    raise PilotError(
        f"ingest of session {label} returned status {status or 'missing'}: "
        f"{outcome.get('error', 'no reason given')}"
    )


def rows(payload: dict[str, Any], member: str) -> list[dict[str, Any]]:
    val = payload.get(member)
    if isinstance(val, list):
        return [item for item in val if isinstance(item, dict)]
    return []


def result_ids(payload: dict[str, Any]) -> list[str]:
    return [
        str(r["id"])
        for r in rows(payload, "results")
        if "id" in r and r["id"] is not None
    ]


def largest_category_share(categories: list[CategoryRecall], scored: int) -> float:
    if not categories or scored == 0:
        return 0.0
    largest = max(c.scored for c in categories)
    return score.rate(largest, scored)


def tainted_results(payload: dict[str, Any]) -> int:
    return sum(
        1 for r in rows(payload, "results") if r.get("instruction_taint") is True
    )


def excluded_counts(payload: dict[str, Any]) -> list[tuple[str, int]]:
    excluded = payload.get("excluded")
    if not isinstance(excluded, dict):
        return []
    res: list[tuple[str, int]] = []
    for reason, count in excluded.items():
        if isinstance(count, int):
            res.append((str(reason), count))
    return res


def load_config(path: Path | str) -> PilotConfig:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if data.get("batch_embeddings"):
        raise PilotError(
            "batch_embeddings is retired: the OpenAI Batch lane is gone; "
            "remove the key to embed synchronously"
        )
    dataset_dict = data["dataset"]
    dataset = DatasetPin(
        repository=dataset_dict["repository"],
        file=dataset_dict["file"],
        sha256=dataset_dict["sha256"],
        bytes=dataset_dict["bytes"],
        license=dataset_dict["license"],
    )
    return PilotConfig(
        bench=data["bench"],
        dataset=dataset,
        slice_first_instances=data["slice_first_instances"],
        granularity=data["granularity"],
        lane=data["lane"],
        k_values=data["k_values"],
        token_budget=data["token_budget"],
        project_id=data["project_id"],
        term_extraction=data["term_extraction"],
        scoring_scope=data["scoring_scope"],
        execution=data.get("execution", "per_instance"),
    )
