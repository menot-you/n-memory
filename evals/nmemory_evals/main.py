"""Runner orchestration and argparse flags for nmemory-evals; `cli` owns dispatch."""

from __future__ import annotations

import argparse
import json
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

from nmemory_evals import (
    artifacts,
    comparison,
    driver,
    e1,
    llm_proposer,
    narrate,
    pilot,
    qa,
    score,
    world,
)
from nmemory_evals.artifacts import BENCH_VERSION, ConfigArtifact, file_sha256
from nmemory_evals.capture_gap import (
    EvalRefusal,
    GoldCaptureItem,
    load_capture_gold_set,
    open_live_store,
    render_capture_gap_markdown,
    run_capture_gap_audit,
    store_verifier,
)
from nmemory_evals.extract_eval import (
    evaluate_extractor,
    evaluate_spans,
    render_extract_eval_markdown,
    render_span_eval_markdown,
)
from nmemory_evals.llm_proposer import ClaudeProposer
from nmemory_evals.path import directory_open, file_replace
from nmemory_evals.renamed import env
from nmemory_evals.world import WorldConfig

DEFAULT_BINARY: str = "nmemory"


class BenchError(Exception):
    """Benchmark CLI or orchestration error."""


def default_binary() -> Path:
    """The port, unless the caller names another SUT with `NOTT_NMEMORY_BIN`.

    The `nmemory` console script (`[project.scripts]` in `mcps/nmemory`)
    is the port installed in the workspace venv. There is no more resolution
    through the oracle: the migration closed and the upstream left.
    """
    explicit = env("NOTT_NMEMORY_BIN")
    if explicit is not None:
        return Path(explicit)
    which = shutil.which(DEFAULT_BINARY)
    if which:
        return Path(which)
    raise BenchError(
        f"{DEFAULT_BINARY!r} is not on PATH and NOTT_NMEMORY_BIN was not declared: "
        "the SUT cannot be resolved"
    )


def is_var_tmp_descendant(path: Path) -> bool:
    """Check lexical and resolved paths for a subdirectory of /var/tmp."""
    parts = path.parts
    if len(parts) >= 4 and parts[0] == "/" and parts[1] == "var" and parts[2] == "tmp":
        tail = parts[3:]
        root = Path("/var/tmp")
    elif (
        len(parts) >= 5
        and parts[0] == "/"
        and parts[1] == "private"
        and parts[2] == "var"
        and parts[3] == "tmp"
    ):
        tail = parts[4:]
        root = Path("/private/var/tmp")
    else:
        return False
    if not tail or any(p in (".", "..") for p in tail):
        return False
    try:
        resolved, root = path.resolve(), root.resolve()
    except OSError:
        return False
    return resolved != root and resolved.is_relative_to(root)


def prepare_output(path: Path) -> Path:
    if not is_var_tmp_descendant(path):
        raise BenchError(f"output path must be a new descendant of /var/tmp: {path}")
    if path.exists() or path.is_symlink():
        raise BenchError(f"output path already exists: {path}")
    try:
        with directory_open(path, create=True, fresh=True):
            pass
    except OSError as err:
        raise BenchError(f"failed to prepare output path {path}: {err}") from err
    return path


def verify_binary_unchanged(path: Path, expected_sha256: str) -> None:
    if file_sha256(path) != expected_sha256:
        raise BenchError(
            f"binary under test changed while the benchmark was running: {path}"
        )


def benchmark_store_dir() -> Path:
    """The benchmark's store directory: `NOTT_DATABASE_DIR`, set by whoever runs the bench.

    The driver refuses anything but a disposable directory under /var/tmp, so
    an unset or live value never reaches a child.
    """
    raw = (env("NOTT_DATABASE_DIR") or "").strip()
    if not raw:
        raise BenchError(
            "NOTT_DATABASE_DIR must name the benchmark's disposable store directory "
            "under /var/tmp"
        )
    return Path(raw)


def run_n1(
    binary: Path,
    seed: int,
    services: int,
    engineers: int,
    events: int,
    questions: int,
    k: int,
    out: Path,
) -> Path:
    if not binary.is_file():
        raise BenchError(f"binary under test is not a file: {binary}")
    binary_path = binary.resolve()
    binary_sha256 = file_sha256(binary_path)

    world_config = WorldConfig(
        seed=seed,
        services=services,
        engineers=engineers,
        events=events,
    )
    w = world.generate(world_config)
    cases = qa.build_cases(w, questions, narrate.render, world_config)
    oow_questions = qa.out_of_world_count(questions)
    run_dir = prepare_output(out)
    store_dir = benchmark_store_dir()

    run = driver.exercise(binary_path, run_dir, store_dir, cases, k)
    verify_binary_unchanged(binary_path, binary_sha256)

    scores = score.calculate(seed, k, run.questions, run.observations)
    config = ConfigArtifact(
        bench="n1",
        seed=seed,
        binary_path=str(binary_path),
        binary_sha256=binary_sha256,
        services=services,
        engineers=engineers,
        events=events,
        questions=questions,
        oow_questions=oow_questions,
        k=k,
        bench_version=BENCH_VERSION,
    )
    artifacts.write_bundle(
        run_dir,
        config,
        w,
        run.questions,
        run.transcript,
        scores,
    )
    scores_file = run_dir / "scores.json"
    print(scores_file)
    return scores_file


def run_longmemeval(
    binary: Path,
    config_path: Path,
    data_path: Path,
    out: Path,
) -> Path:
    if not binary.is_file():
        raise BenchError(f"binary under test is not a file: {binary}")
    binary_path = binary.resolve()
    binary_sha256 = file_sha256(binary_path)

    config = pilot.load_config(config_path)
    store_dir = benchmark_store_dir()
    run_dir = prepare_output(out)

    runner = pilot.Pilot(
        config=config,
        dataset=data_path,
        binary=binary_path,
        run_dir=run_dir,
        store_dir=store_dir,
    )
    report = runner.run()
    verify_binary_unchanged(binary_path, binary_sha256)

    results_file = run_dir / "results.json"
    artifacts.write_report(results_file, report)
    print(results_file)
    return results_file


def run_e1(args: argparse.Namespace) -> Path:
    binary = args.binary or default_binary()
    store_dir = benchmark_store_dir()
    report = e1.run_e1_driver(
        binary=binary,
        store_dir=store_dir,
        lane=args.lane,
        k=args.k,
    )
    data = report.to_dict()
    if args.out is not None:
        out_dir: Path = Path(args.out)
        if not is_var_tmp_descendant(out_dir):
            raise BenchError(
                f"output path must be a new descendant of /var/tmp: {out_dir}"
            )
        res_file = out_dir / "e1-results.json"
        _report_write(
            out_dir,
            {res_file.name: json.dumps(data, indent=2, ensure_ascii=False) + "\n"},
        )
        print(f"E1 results written to {res_file}")
        return res_file
    else:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return Path("/dev/stdout")


def _out_directory(args: argparse.Namespace) -> Path | None:
    """`--out`, refused unless under /var/tmp. Both evals ask before they read
    anything, so a bad path is never paid for with a store or a transcript."""
    if getattr(args, "out", None) is None:
        return None
    out_dir = Path(args.out)
    if not is_var_tmp_descendant(out_dir):
        raise BenchError(f"output path must be a new descendant of /var/tmp: {out_dir}")
    return out_dir


def _emit(args: argparse.Namespace, data: dict[str, object], md_text: str) -> None:
    """Writes the report where the flag says: `--out` (JSON + markdown),
    `--json` to stdout, or markdown to stdout. One emitter for both evals."""
    out_dir = _out_directory(args)
    if out_dir is not None:
        res_file = out_dir / "extract-eval-results.json"
        md_file = out_dir / "EXTRACT-EVAL.md"
        _report_write(
            out_dir,
            {
                res_file.name: json.dumps(data, indent=2, ensure_ascii=False) + chr(10),
                md_file.name: md_text,
            },
        )
        print(f"Extract-eval report written to {md_file} and {res_file}")
        print(chr(10) + md_text)
    elif getattr(args, "json", False):
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print(md_text)


def _report_write(directory: Path, files: dict[str, str]) -> None:
    """Write the report through one directory descriptor."""
    try:
        with directory_open(directory, create=True) as descriptor:
            for name, text in files.items():
                file_replace(descriptor, name, [text])
    except OSError as err:
        raise BenchError(f"report write refused: {err}") from err


def _run_span_eval(args: argparse.Namespace, items: list[GoldCaptureItem]) -> int:
    """`extract-eval --proposer {deterministic,llm}`: the span metric (N5b).

    `llm` builds the `ClaudeProposer` BEFORE reading any transcript: without
    `claude` in the PATH it is a refusal (2) before any cost. The report
    carries the `deterministic` block and, with `llm`, the `llm` block and the
    `receipt`. Exit: `2` for a missing dataset, transcript, or binary and for
    a non-verbatim proposal (all `EvalRefusal`, translated in `main`); `0`
    when it measured. The DoD bar is read from the numbers — there is no `3`
    here, because a low recall is a MEASUREMENT, not a finding.
    """
    proposer: ClaudeProposer | None = None
    if args.proposer == "llm":
        # By module, not by name: the e2e replaces `claude_transport` and the
        # real transport never runs inside a test.
        proposer = ClaudeProposer(
            timeout_s=args.timeout_s, transport=llm_proposer.claude_transport()
        )
    report = evaluate_spans(items, proposer)
    data: dict[str, object] = report.to_dict()
    if proposer is not None:
        data["receipt"] = proposer.receipt.to_dict()
    _emit(args, data, render_span_eval_markdown(report))
    return 0


def run_extract_eval(args: argparse.Namespace) -> int:
    """The U3 eval. Returns the D18 code the result deserves.

    D18 aggregation in total order, `1 > 2 > 3 > 0`:

    `2` when at least one session refused — missing transcript, or no gold set
    statement anchored verbatim in its turns. A missing anchor is a missing
    oracle: precision over a corpus without positives does not measure the
    extractor, and a judge that returned `0.0%` would be inventing the verdict.
    `3` when it measured and the extractor fired over a non-persistable turn —
    a false positive IS the finding, because «precision is not optional»
    (PLAN.md:154-155). `2` also when the whole gold set is missing, and when
    NO session was measured: vacuity is a refusal, never green.
    """
    _out_directory(args)
    items = load_capture_gold_set(getattr(args, "dataset", None))
    if getattr(args, "proposer", None) is not None:
        return _run_span_eval(args, items)
    report = evaluate_extractor(items)
    _emit(args, report.to_dict(), render_extract_eval_markdown(report))

    if report.sessions_refused or not report.sessions_measured:
        # VACUITY is also 2. A gold set where no item declares
        # `transcript_path` produces no session at all: without this guard,
        # zero measured sessions gave `fp == 0` and therefore exit 0 — green
        # for having measured nothing, which is exactly what D18 calls a
        # refusal.
        return 2
    return 3 if report.metrics.fp else 0


def run_capture_gap(args: argparse.Namespace) -> int:
    """The U2 measurer. Returns the D18 code the result deserves.

    `0` green · `2` refusal (store, transcript, or dataset missing, raised as
    `EvalRefusal` and translated in `main`) · `3` findings — at least one human
    `captured` label that the verification against the store refused.
    """
    out_dir = _out_directory(args)
    items = load_capture_gold_set(getattr(args, "dataset", None))
    session = getattr(args, "session", None)
    if session is not None:
        items = [it for it in items if it.session_id == session]
        if not items:
            raise EvalRefusal(f"no gold set item for session {session!r}")

    report = run_capture_gap_audit(items, store_verifier(open_live_store()))
    md_text = render_capture_gap_markdown(report)

    if out_dir is not None:
        res_file = out_dir / "capture-gap-results.json"
        md_file = out_dir / "CAPTURE-GAP.md"
        _report_write(
            out_dir,
            {
                res_file.name: json.dumps(
                    report.to_dict(), indent=2, ensure_ascii=False
                )
                + chr(10),
                md_file.name: md_text,
            },
        )
        print(f"Capture-gap report written to {md_file} and {res_file}")
        print(chr(10) + md_text)
    elif getattr(args, "json", False):
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    else:
        print(md_text)

    return 3 if report.invalid_items else 0


def run_compare(args: argparse.Namespace) -> Path:
    binary = args.binary or default_binary()
    store_dir = benchmark_store_dir()
    scratch_dir = Path("/var/tmp/nmemory-bench/compare-run")
    scratch_dir.mkdir(parents=True, exist_ok=True)

    requested = [
        sys_name.strip().lower()
        for sys_name in args.systems.split(",")
        if sys_name.strip()
    ]
    # Two runs on the SAME vendor instance cannot share a store: the second
    # would read what the first wrote and recall would come out perfect by
    # contamination. What tells them apart is the instant, which differs on
    # every run.
    run_id = args.run_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")

    adapters: list[comparison.BaseMemoryAdapter] = []
    for name in requested:
        if name == "nmemory":
            adapters.append(
                comparison.NMemoryAdapter(
                    binary=binary, store_dir=store_dir, run_dir=scratch_dir
                )
            )
        else:
            # N1: the simulated competitors died, and that is why a third
            # party only comes back over HTTP with a receipt from its own
            # server. The constructor refuses naming the missing variable —
            # LOCK 3 is «all or nothing», and «unknown memory system» would
            # send the operator looking for an adapter that exists.
            try:
                adapters.append(comparison.build_http_adapter(name, run_id=run_id))
            except EvalRefusal as refusal:
                raise BenchError(str(refusal)) from refusal

    report = comparison.run_comparison_benchmark(adapters=adapters, k=args.k)
    md_table = comparison.render_markdown_matrix(report)

    if args.out is not None:
        out_dir: Path = Path(args.out)
        if not is_var_tmp_descendant(out_dir):
            raise BenchError(
                f"output path must be a new descendant of /var/tmp: {out_dir}"
            )
        res_json = out_dir / "comparison-results.json"
        res_md = out_dir / "COMPARISON.md"
        _report_write(
            out_dir,
            {
                res_json.name: json.dumps(
                    report.to_dict(), indent=2, ensure_ascii=False
                )
                + "\n",
                res_md.name: md_table + "\n",
            },
        )
        print(f"Comparison report written to {res_md} and {res_json}")
        return res_md
    else:
        print(md_table)
        return Path("/dev/stdout")


def run_verify_bundle(bundle_dir: Path) -> None:
    bundle_json_path = bundle_dir / "bundle.json"
    if bundle_json_path.is_file():
        with open(bundle_json_path, "r", encoding="utf-8") as f:
            bundle_data = json.load(f)

        files = bundle_data.get("files", {})
        if not files:
            raise BenchError(f"bundle.json in {bundle_dir} contains no file entries")

        for filename, expected_hash in files.items():
            target = bundle_dir / filename
            if not target.is_file():
                raise BenchError(f"bundle verification failed: missing file {filename}")
            actual_hash = file_sha256(target)
            if actual_hash != expected_hash:
                raise BenchError(
                    f"bundle verification failed for {filename}: expected {expected_hash}, got {actual_hash}"
                )
        print(
            f"Bundle at {bundle_dir} verified: all {len(files)} files match sha256 checksums."
        )
        return

    # Check canonical bundle structure
    required_files = [
        "config.json",
        "world.json",
        "qa.json",
        "transcript.jsonl",
        "scores.json",
    ]
    missing = [f for f in required_files if not (bundle_dir / f).is_file()]
    if missing:
        raise BenchError(
            f"bundle verification failed: missing required files: {missing}"
        )

    for fname in ("config.json", "world.json", "qa.json", "scores.json"):
        target = bundle_dir / fname
        try:
            with open(target, "r", encoding="utf-8") as f:
                json.load(f)
        except Exception as err:
            raise BenchError(
                f"bundle verification failed for {fname}: malformed JSON: {err}"
            ) from err

    # Check transcript.jsonl
    transcript_path = bundle_dir / "transcript.jsonl"
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            for line in f:
                line_str = line.strip()
                if line_str:
                    json.loads(line_str)
    except Exception as err:
        raise BenchError(
            f"bundle verification failed for transcript.jsonl: {err}"
        ) from err

    print(
        f"Bundle at {bundle_dir} verified: all {len(required_files)} canonical files are valid."
    )


def run_diff(left_dir: Path, right_dir: Path) -> None:
    left_scores = left_dir / "scores.json"
    right_scores = right_dir / "scores.json"
    if left_scores.is_file() and right_scores.is_file():
        with open(left_scores, "r", encoding="utf-8") as f:
            left = json.load(f)
        with open(right_scores, "r", encoding="utf-8") as f:
            right = json.load(f)
        print(f"Diff between {left_dir} and {right_dir}:")
        for key in ("hit_rate", "abstain_rate", "exact_precision", "rank_score"):
            l_val = left.get(key)
            r_val = right.get(key)
            print(f"  {key:20s} left={l_val} right={r_val}")
        return

    left_results = left_dir / "results.json"
    right_results = right_dir / "results.json"
    if left_results.is_file() and right_results.is_file():
        with open(left_results, "r", encoding="utf-8") as f:
            left = json.load(f)
        with open(right_results, "r", encoding="utf-8") as f:
            right = json.load(f)
        print(f"LongMemEval diff between {left_dir} and {right_dir}:")
        print(f"  Left  scored: {left.get('instances', {}).get('scored')}")
        print(f"  Right scored: {right.get('instances', {}).get('scored')}")
        return

    raise BenchError(
        "no comparable scores.json or results.json found in both bundle directories"
    )


_URLISH = re.compile(r"\S*://\S*")


class _RedactingParser(argparse.ArgumentParser):
    """Redacts any URL the `argparse` would echo back to stderr.

    Hiding the flag only closes the position the guard knows about. The review
    proved the same leak in three positions no per-flag guard reaches: before
    the subcommand (`invalid choice: '<the database URL>'`), in a
    subcommand that never had the flag, and as a loose positional. All three
    exit through `error()` — this is the single point every argparse message
    passes through, and that is why the redaction lives here.
    `add_subparsers` inherits `parser_class=type(self)`, so the subparsers
    redact without being registered.
    """

    def error(self, message: str) -> NoReturn:
        super().error(_URLISH.sub("<redacted-url>", message))


class _RejectDatabaseUrl(argparse.Action):
    """Consumes the value and refuses WITHOUT echoing it.

    Removing the flag was not enough: `argparse` answers `unrecognized
    arguments: --database-url <the database URL>` and prints the
    whole value to stderr, which goes into the run's log. The flag exists
    again only to SWALLOW the value and die quietly — `nargs=1` is what
    guarantees the URL does not leak through as a positional for argparse to
    reprint.
    """

    def __init__(self, option_strings: list[str], dest: str, **kwargs: object) -> None:
        super().__init__(option_strings, dest, nargs=1, help=argparse.SUPPRESS)

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: object,
        option_string: str | None = None,
    ) -> None:
        parser.error(
            f"{option_string} was removed: the benchmark store is the directory "
            "NOTT_DATABASE_DIR names."
        )


def build_parser() -> argparse.ArgumentParser:
    parser = _RedactingParser(
        prog="nmemory-evals",
        description="Evaluation harness for nmemory MCP and CLI.",
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # n1 subcommand
    n1_p = subparsers.add_parser(
        "scenario run", help="Run n1 synthetic world benchmark"
    )
    n1_p.add_argument(
        "--binary", type=Path, default=None, help="Path to nmemory binary under test"
    )
    n1_p.add_argument(
        "--seed", type=int, default=42, help="World generation seed (default: 42)"
    )
    n1_p.add_argument(
        "--services", type=int, default=8, help="Number of services (default: 8)"
    )
    n1_p.add_argument(
        "--engineers", type=int, default=5, help="Number of engineers (default: 5)"
    )
    n1_p.add_argument(
        "--events", type=int, default=120, help="Number of events (default: 120)"
    )
    n1_p.add_argument(
        "--questions", type=int, default=40, help="Number of questions (default: 40)"
    )
    n1_p.add_argument(
        "-k", "--k", type=int, default=3, help="Recall limit k (default: 3)"
    )
    n1_p.add_argument(
        "--out", type=Path, default=None, help="Output directory under /var/tmp"
    )

    event_p = subparsers.add_parser(
        "scenario show", help="Print one n1 world event, as the benchmark ingests it"
    )
    event_p.add_argument("--seed", type=int, default=42)
    event_p.add_argument("--services", type=int, default=8)
    event_p.add_argument("--engineers", type=int, default=5)
    event_p.add_argument("--events", type=int, default=120)
    event_p.add_argument(
        "--seq", type=int, required=True, help="The event's sequence number"
    )

    session_p = subparsers.add_parser(
        "session show",
        help="Print one LongMemEval session, as the suite ingests it",
    )
    session_p.add_argument("--data", type=Path, required=True)
    session_p.add_argument(
        "--sha256", required=True, help="The dataset digest the suite pinned"
    )
    session_p.add_argument("--question", required=True)
    session_p.add_argument(
        "--position", type=int, required=True, help="Its index in the haystack"
    )
    session_p.add_argument("--session", required=True)

    # LongMemEval suite
    lme_p = subparsers.add_parser("suite run", help="Run LongMemEval benchmark")
    lme_p.add_argument(
        "--binary",
        type=Path,
        default=None,
        help="Path to nmemory binary under test",
    )
    lme_p.add_argument(
        "--config", type=Path, required=True, help="Path to pilot config JSON"
    )
    lme_p.add_argument(
        "--data",
        "--dataset",
        type=Path,
        dest="data",
        required=True,
        help="Path to LongMemEval dataset JSON",
    )
    lme_p.add_argument(
        "--out",
        type=Path,
        default=Path("/var/tmp/nmemory-bench/longmemeval"),
        help="Output directory",
    )
    # e1
    e1_p = subparsers.add_parser(
        "capsule score", help="Run E1 41-item feedback capture & recall benchmark"
    )
    e1_p.add_argument("--database-url", action=_RejectDatabaseUrl)
    e1_p.add_argument(
        "--binary", type=Path, default=None, help="Path to nmemory binary under test"
    )
    e1_p.add_argument(
        "--lane",
        type=str,
        default="term",
        choices=["term", "vector", "fused"],
        help="Retrieval lane",
    )
    e1_p.add_argument("-k", type=int, default=5, help="Recall cut-off limit")
    e1_p.add_argument("--out", type=Path, default=None, help="Output directory")

    # compare
    compare_p = subparsers.add_parser(
        "arena compare",
        help="Run comparative benchmark over Gold Set (nmemory; third-party products need a receipt-bearing adapter)",
    )
    compare_p.add_argument("--database-url", action=_RejectDatabaseUrl)
    compare_p.add_argument(
        "--run-id",
        default=None,
        help=(
            "Identifier that separates this run's memories inside each vendor "
            "(session in Zep, user_id in mem0). Defaults to the UTC timestamp."
        ),
    )
    compare_p.add_argument(
        "--binary", type=Path, default=None, help="Path to nmemory binary under test"
    )
    compare_p.add_argument(
        "--systems",
        type=str,
        default="nmemory",
        help="Comma-separated list of memory systems to evaluate",
    )
    compare_p.add_argument(
        "-k", type=int, default=5, help="Recall cut-off limit (default: 5)"
    )
    compare_p.add_argument(
        "--out", type=Path, default=None, help="Output directory under /var/tmp"
    )

    ext_p = subparsers.add_parser(
        "proposal score",
        help="Evaluate precision and recall of deterministic nmemory_turn_extract against gold set",
    )
    ext_p.add_argument(
        "--dataset",
        type=str,
        default=None,
        help="Path to custom capture gold set JSON",
    )
    ext_p.add_argument(
        "--out",
        type=str,
        default=None,
        help="Output directory under /var/tmp for results",
    )
    ext_p.add_argument(
        "--json",
        action="store_true",
        help="Output raw JSON instead of markdown",
    )
    ext_p.add_argument(
        "--proposer",
        choices=["deterministic", "llm"],
        default=None,
        help=(
            "Span recall against the anchored gold set: deterministic extractor "
            "alone, or llm (claude -p) beside it in the same run"
        ),
    )
    ext_p.add_argument(
        "--timeout-s",
        type=int,
        default=300,
        help="Per-batch timeout for the llm proposer (default: 300; the largest batch measured 193s)",
    )

    gap_p = subparsers.add_parser(
        "transcript audit",
        help="Audit capture gap across real historical sessions against gold dataset",
    )
    gap_p.add_argument(
        "--dataset",
        type=str,
        default=None,
        help="Path to custom capture gold set JSON",
    )
    gap_p.add_argument(
        "--out",
        type=str,
        default=None,
        help="Output directory under /var/tmp for results",
    )
    gap_p.add_argument(
        "--session",
        type=str,
        default=None,
        help="Report a single session's own numbers (e.g. 6da118b7)",
    )
    gap_p.add_argument(
        "--json",
        action="store_true",
        help="Output raw JSON instead of markdown",
    )

    shadow_p = subparsers.add_parser(
        "review measure",
        help="U5: measures (b) in a shadow store with truth by construction",
    )
    shadow_p.add_argument(
        "--shadow-store",
        required=True,
        type=Path,
        help="the shadow's disposable store file (under /var/tmp; replaced)",
    )
    shadow_p.add_argument(
        "--project", required=True, help="scope of the slice and of the curator"
    )
    shadow_p.add_argument(
        "--source-root", default=".", help="tree the anchors are copied from"
    )
    shadow_p.add_argument(
        "--dest-root", required=True, help="where the fixture tree is materialized"
    )
    shadow_p.add_argument(
        "--per-cell", type=int, default=5, help="cap per (kind, cell)"
    )
    shadow_p.add_argument(
        "--budget", type=int, default=50, help="curator judgments in this run"
    )

    vb_p = subparsers.add_parser(
        "artifact verify", help="Verify sha256 checksums of an evaluation bundle"
    )
    vb_p.add_argument("bundle", type=Path, help="Bundle directory to verify")

    av_p = subparsers.add_parser(
        "arena judge",
        help='Compute the pre-registered loss verdict ("nMEMORY is NOT the first") '
        "from PRE-REGISTERED-LOSS.txt and a results table",
    )
    av_p.add_argument(
        "--loss", type=Path, required=True, help="PRE-REGISTERED-LOSS.txt"
    )
    av_p.add_argument(
        "--results",
        type=Path,
        default=None,
        help='JSON list of {"system", "metric", "budget", "value"} cells',
    )
    av_p.add_argument(
        "--precision-report",
        type=Path,
        action="append",
        default=None,
        metavar="RELATORIO",
        help=(
            "PrecisionMemBench harness report with the `nott_budget` block "
            "(repeatable). The `precision_A` cells come from the reader, with "
            "its refusals, instead of a hand-written table"
        ),
    )
    av_p.add_argument(
        "--expected-sha256",
        default=None,
        help="sha256 of the pre-registration text from the first run; a different text refuses",
    )

    sv_p = subparsers.add_parser(
        "synthesis judge",
        help="Compute the three rules of PRE-REGISTERED-SYNTHESIS.txt over one run",
    )
    sv_p.add_argument(
        "--preregistration",
        type=Path,
        required=True,
        help="PRE-REGISTERED-SYNTHESIS.txt",
    )
    sv_p.add_argument(
        "--results",
        type=Path,
        required=True,
        help="JSON with the run's measurements (cost, recall, pairs, fidelity)",
    )
    sv_p.add_argument(
        "--expected-sha256",
        default=None,
        help="sha256 of the first run's text; a different text refuses",
    )

    acc_p = subparsers.add_parser(
        "arena run",
        help="One accuracy_B cell: seeding → window → fixed reader → fixed judge",
    )
    acc_p.add_argument(
        "--benchmark",
        choices=["longmemeval", "locomo"],
        default="longmemeval",
    )
    acc_p.add_argument(
        "--system", required=True, choices=["nmemory", "zep", "letta", "mem0"]
    )
    acc_p.add_argument("--budget", type=int, required=True)
    acc_p.add_argument("--corpus", type=Path, required=True)
    acc_p.add_argument("--expected-sha256", type=str, default=None)
    acc_p.add_argument("--expected-conversations", type=int, default=10)
    acc_p.add_argument("--expected-total", type=int, default=1986)
    acc_p.add_argument("--out", type=Path, required=True)
    acc_p.add_argument("--checkpoint", type=Path, default=None)

    loc_p = subparsers.add_parser(
        "locomo",
        help="Run the official LoCoMo benchmark across all 5 categories",
    )
    loc_p.add_argument(
        "--system", required=True, choices=["nmemory", "zep", "letta", "mem0"]
    )
    loc_p.add_argument("--budget", type=int, default=4096)
    loc_p.add_argument("--corpus", type=Path, required=True)
    loc_p.add_argument("--expected-sha256", type=str, default=None)
    loc_p.add_argument("--expected-conversations", type=int, default=10)
    loc_p.add_argument("--expected-total", type=int, default=1986)
    loc_p.add_argument("--out", type=Path, required=True)
    loc_p.add_argument("--checkpoint", type=Path, default=None)

    abs_p = subparsers.add_parser(
        "arena probe",
        help="The A.3 arm: _abs probes against the seeded store",
    )
    abs_p.add_argument(
        "--system", required=True, choices=["nmemory", "zep", "letta", "mem0"]
    )
    abs_p.add_argument("--corpus", type=Path, required=True)
    abs_p.add_argument("--out", type=Path, required=True)

    diff_p = subparsers.add_parser(
        "artifact diff", help="Diff two evaluation result bundles"
    )
    diff_p.add_argument(
        "--left", type=Path, required=True, help="Left bundle directory"
    )
    diff_p.add_argument(
        "--right", type=Path, required=True, help="Right bundle directory"
    )

    return parser
