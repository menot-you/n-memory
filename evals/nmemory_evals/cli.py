"""The `nmemory-evals <noun> <verb>` grammar: one literal command table.

The runners and the argparse flags live in `main`; this module owns dispatch.
Each handler parses its own flags and returns the D18 code: `0` satisfied,
`1` the tool broke, `2` a typed refusal, `3` a finding.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Final, NamedTuple

from nmemory_evals import artifacts, longmemeval, narrate, synthesis, world
from nmemory_evals import main as harness
from nmemory_evals.capture_gap import EvalRefusal
from nmemory_evals.comparison import accuracy as comparison_accuracy
from nmemory_evals.comparison import arena, locomo, precisionmembench
from nmemory_evals.driver import DriverError
from nmemory_evals.pilot import PilotError
from nmemory_evals.renamed import RENAMED, RenamedVariableError, refuse
from nmemory_evals.world import WorldConfig

EXE: Final = "nmemory-evals"


class Command(NamedTuple):
    synopsis: str
    summary: str
    handler: Callable[[list[str]], int]


def _parse(noun: str, verb: str, argv: list[str]) -> argparse.Namespace:
    return harness.build_parser().parse_args([f"{noun} {verb}", *argv])


def cmd_scenario_run(argv: list[str]) -> int:
    args = _parse("scenario", "run", argv)
    binary = args.binary or harness.default_binary()
    out = args.out or Path(f"/var/tmp/nmemory-bench/run-s{args.seed}")
    if args.k == 0:
        raise harness.BenchError("--k must be nonzero")
    if args.questions < 5:
        raise harness.BenchError("--questions must be at least 5")
    harness.run_n1(
        binary=binary,
        seed=args.seed,
        services=args.services,
        engineers=args.engineers,
        events=args.events,
        questions=args.questions,
        k=args.k,
        out=out,
    )
    return 0


def cmd_scenario_show(argv: list[str]) -> int:
    """Print one event of an n1 world: the proof each n1 capsule names."""
    args = _parse("scenario", "show", argv)
    config = WorldConfig(
        seed=args.seed,
        services=args.services,
        engineers=args.engineers,
        events=args.events,
    )
    for event in world.generate(config).events:
        if event.seq == args.seq:
            print(f"{event.at} {narrate.render(event)}")
            return 0
    print(f"nmemory-evals: refused: no event {args.seq} in that world", file=sys.stderr)
    return 2


def cmd_session_show(argv: list[str]) -> int:
    """Print one LongMemEval session: the proof each suite capsule names."""
    args = _parse("session", "show", argv)
    if not args.data.is_file():
        print(
            f"nmemory-evals: refused: no dataset file at {args.data}", file=sys.stderr
        )
        return 2
    measured = artifacts.file_sha256(args.data)
    if measured != args.sha256:
        print(
            f"nmemory-evals: refused: dataset digest is {measured}, not {args.sha256}",
            file=sys.stderr,
        )
        return 2
    session = longmemeval.find_session(
        args.data, args.question, args.position, args.session
    )
    if session is None:
        print(
            f"nmemory-evals: refused: no session {args.session} at position "
            f"{args.position} in question {args.question}",
            file=sys.stderr,
        )
        return 2
    print(f"{session.valid_from} {session.content}")
    return 0


def cmd_suite_run(argv: list[str]) -> int:
    args = _parse("suite", "run", argv)
    binary = args.binary or harness.default_binary()
    harness.run_longmemeval(
        binary=binary,
        config_path=args.config,
        data_path=args.data,
        out=args.out,
    )
    return 0


def cmd_capsule_score(argv: list[str]) -> int:
    args = _parse("capsule", "score", argv)
    harness.run_e1(args)
    return 0


def cmd_arena_compare(argv: list[str]) -> int:
    args = _parse("arena", "compare", argv)
    harness.run_compare(args)
    return 0


def cmd_proposal_score(argv: list[str]) -> int:
    args = _parse("proposal", "score", argv)
    return harness.run_extract_eval(args)


def cmd_transcript_audit(argv: list[str]) -> int:
    args = _parse("transcript", "audit", argv)
    return harness.run_capture_gap(args)


def cmd_review_measure(argv: list[str]) -> int:
    args = _parse("review", "measure", argv)
    from nmemory_evals.shadow import run_cli as run_shadow_cli

    return run_shadow_cli(args)


def cmd_artifact_verify(argv: list[str]) -> int:
    args = _parse("artifact", "verify", argv)
    harness.run_verify_bundle(args.bundle)
    return 0


def cmd_artifact_diff(argv: list[str]) -> int:
    args = _parse("artifact", "diff", argv)
    harness.run_diff(args.left, args.right)
    return 0


def cmd_arena_run(argv: list[str]) -> int:
    args = _parse("arena", "run", argv)
    if getattr(args, "benchmark", "longmemeval") == "locomo":
        cell = locomo.run_locomo_cmd(args)
    else:
        cell = comparison_accuracy.run_accuracy_cmd(args)
    print(json.dumps(cell, ensure_ascii=False, indent=1))
    return 0


def cmd_arena_probe(argv: list[str]) -> int:
    args = _parse("arena", "probe", argv)
    linha = comparison_accuracy.run_abstain_cmd(args)
    print(json.dumps(linha, ensure_ascii=False, indent=1))
    return 0


def cmd_arena_judge(argv: list[str]) -> int:
    args = _parse("arena", "judge", argv)
    # D18: `0` first · `3` the defeat sentence fired (finding) · `2` refusal.
    prereg = arena.load_preregistration(args.loss)
    if not args.results and not args.precision_report:
        raise EvalRefusal(
            "arena judge with no cells: pass --results, --precision-report, or both"
        )
    tabela: dict[arena.Cell, float] = {}
    colapsados: tuple[frozenset[int], ...] = ()
    if args.results:
        tabela |= arena.load_table(args.results)
    if args.precision_report:
        # A conflict between the two sources is a refusal, not a
        # silent overwrite: the 2nd pass of the #609 review showed the
        # same run giving exit 3 with `--results` alone and exit 0
        # after the report overwrote the cell.
        pass
    if args.precision_report:
        conhecidos = frozenset({"nMEM", *prereg.live})
        das_corridas = precisionmembench.table_from_reports(
            list(args.precision_report), conhecidos, prereg.b_grid
        )
        divergentes = sorted(
            f"{c}: --results {tabela[c]} vs report {v}"
            for c, v in das_corridas.items()
            if c in tabela and tabela[c] != v
        )
        if divergentes:
            raise EvalRefusal(
                "the two sources disagree on the same cell, and picking "
                "one was picking the result: " + "; ".join(divergentes)
            )
        tabela |= das_corridas
        colapsados = precisionmembench.collapsed_budgets(
            [
                precisionmembench.read_precision_report(p, b_grid=prereg.b_grid)
                for p in args.precision_report
            ]
        )
    verdict = arena.judge(tabela, prereg, args.expected_sha256)
    print(
        json.dumps(
            {
                "preregistration_sha256": verdict.preregistration_sha256,
                "fired": list(verdict.fired),
                "sentence": verdict.sentence,
                # Budgets that resolved the SAME configuration: they
                # count as one observation, not as several columns.
                "collapsed_budgets": [sorted(g) for g in colapsados],
            },
            ensure_ascii=False,
        )
    )
    return 3 if verdict.fired else 0


def cmd_synthesis_judge(argv: list[str]) -> int:
    args = _parse("synthesis", "judge", argv)
    # D18: `0` the synthesis enters · `3` a rule failed · `2` refusal.
    pre = synthesis.load_preregistration(args.preregistration)
    sv = synthesis.judge(
        synthesis.load_results(args.results), pre, args.expected_sha256
    )
    print(
        json.dumps(
            {
                "preregistration_sha256": sv.preregistration_sha256,
                "failed": list(sv.failed),
                "enters": sv.enters,
                "reported": dict(sv.reported),
            },
            ensure_ascii=False,
            default=str,
        )
    )
    return 3 if sv.failed else 0


COMMANDS: Final[dict[tuple[str, str], Command]] = {
    ("scenario", "run"): Command(
        "[--binary B] [--seed N] [--services N] [--engineers N] [--events N] "
        "[--questions N] [-k N] [--out DIR]",
        "the n1 synthetic world benchmark",
        cmd_scenario_run,
    ),
    ("scenario", "show"): Command(
        "--seq N [--seed N] [--services N] [--engineers N] [--events N]",
        "print one n1 world event: the proof an n1 capsule names",
        cmd_scenario_show,
    ),
    ("session", "show"): Command(
        "--data F --sha256 H --question Q --position N --session S",
        "print one LongMemEval session: the proof a suite capsule names",
        cmd_session_show,
    ),
    ("suite", "run"): Command(
        "--config F --data F [--binary B] [--out DIR]",
        "the LongMemEval benchmark",
        cmd_suite_run,
    ),
    ("capsule", "score"): Command(
        "[--binary B] [--lane term|vector|fused] [-k N] [--out DIR]",
        "the E1 41-item feedback capture and recall benchmark",
        cmd_capsule_score,
    ),
    ("arena", "compare"): Command(
        "[--binary B] [--systems S] [--run-id R] [-k N] [--out DIR]",
        "the comparative benchmark over the gold set",
        cmd_arena_compare,
    ),
    ("arena", "run"): Command(
        "--system S --budget N --corpus F --out DIR [--checkpoint F]",
        "one accuracy_B cell: seeding, window, fixed reader, fixed judge",
        cmd_arena_run,
    ),
    ("arena", "probe"): Command(
        "--system S --corpus F --out DIR",
        "the A.3 arm: _abs probes against the seeded store",
        cmd_arena_probe,
    ),
    ("arena", "judge"): Command(
        "--loss F [--results F] [--precision-report F ...] [--expected-sha256 H]",
        "the pre-registered loss verdict over a results table",
        cmd_arena_judge,
    ),
    ("synthesis", "judge"): Command(
        "--preregistration F --results F [--expected-sha256 H]",
        "the three rules of PRE-REGISTERED-SYNTHESIS.txt over one run",
        cmd_synthesis_judge,
    ),
    ("proposal", "score"): Command(
        "[--dataset F] [--proposer deterministic|llm] [--timeout-s N] [--json] [--out DIR]",
        "precision and recall of nmemory_turn_extract against the gold set",
        cmd_proposal_score,
    ),
    ("transcript", "audit"): Command(
        "[--dataset F] [--session ID] [--json] [--out DIR]",
        "the capture gap across real historical sessions",
        cmd_transcript_audit,
    ),
    ("review", "measure"): Command(
        "--shadow-url U --project P --dest-root DIR [--source-root DIR] "
        "[--per-cell N] [--budget N]",
        "U5: the curator measured in a shadow store with truth by construction",
        cmd_review_measure,
    ),
    ("artifact", "verify"): Command(
        "<BUNDLE>",
        "verify the sha256 checksums of a result bundle",
        cmd_artifact_verify,
    ),
    ("artifact", "diff"): Command(
        "--left DIR --right DIR", "diff two result bundles", cmd_artifact_diff
    ),
}

USAGE: Final = (
    f"usage: {EXE} <noun> <verb> [arguments]   ({EXE} --help lists every command)"
)


def render_help() -> str:
    """The help, generated from COMMANDS in table order."""
    lines = ["Evaluation harness for nmemory MCP and CLI.", "", USAGE, ""]
    for (noun, verb), command in COMMANDS.items():
        lines.append(f"  {EXE} {noun} {verb} {command.synopsis}".rstrip())
        lines.append(f"      {command.summary}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args in (["--help"], ["-h"], ["help"]):
        print(render_help())
        return 0
    if not args:
        print(USAGE, file=sys.stderr)
        return 2
    if args[0] == "locomo":
        try:
            refuse(os.environ, RENAMED)
            parsed = harness.build_parser().parse_args(args)
            report = locomo.run_locomo_cmd(parsed)
            print(json.dumps(report, ensure_ascii=False, indent=1))
            return 0
        except RenamedVariableError as err:
            print(f"nmemory-evals: refused: {err}", file=sys.stderr)
            return 2
        except SystemExit as e:
            return int(e.code) if isinstance(e.code, int) else 1
        except EvalRefusal as err:
            print(f"nmemory-evals: recusa: {err}", file=sys.stderr)
            return 2
        except (
            harness.BenchError,
            PilotError,
            DriverError,
            artifacts.ArtifactError,
            longmemeval.DatasetError,
        ) as err:
            print(f"nmemory-evals: {err}", file=sys.stderr)
            return 1
    command = COMMANDS.get((args[0], args[1]) if len(args) > 1 else (args[0], ""))
    if command is None:
        # The same redaction as argparse's errors: argv may carry a URL.
        named = harness._URLISH.sub("<redacted-url>", " ".join(args[:2]))
        print(f"unknown command: {EXE} {named}", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        return 2
    try:
        refuse(os.environ, RENAMED)
        return command.handler(args[2:])
    except RenamedVariableError as err:
        print(f"nmemory-evals: refused: {err}", file=sys.stderr)
        return 2
    except SystemExit as e:
        return int(e.code) if isinstance(e.code, int) else 1
    except EvalRefusal as err:
        # D18: a missing oracle, database, or input file is a TYPED REFUSAL (2).
        # Never a skip, never green, and never `1` — `1` says «the tool broke»,
        # and what happened was «the environment I would need to judge is
        # missing».
        print(f"nmemory-evals: recusa: {err}", file=sys.stderr)
        return 2
    except (
        harness.BenchError,
        PilotError,
        DriverError,
        artifacts.ArtifactError,
        longmemeval.DatasetError,
    ) as err:
        print(f"nmemory-evals: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
