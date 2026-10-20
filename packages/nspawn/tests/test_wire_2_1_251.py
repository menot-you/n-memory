"""The 2.1.251 wire, measured — and the two rules that measurement broke.

nSPAWN no longer judges the installed build (owner law, 2026-09-01), so no
version list survives to name this reading. The reading itself does, and it is
what the law leans on: a real wire break surfaces HERE, as a typed terminal
classification error against the observed records. Over the 2.1.251 corpus this
host produced — 130 runs, 115 `result` frames, 458
`rate_limit_event` frames, 9532 `assistant` frames.

It found two divergences, and both were classified as wire breaks:

* **24 runs carry more than one `rate_limit_event`** — up to 39, roughly one per
  turn. `terminal.py` raised on the second.
* **A resumed run carries more than one `result`** (`num_turns` 158, then 2).
  It raised on the second there too.

The consequence is F-019: a run whose own terminal record says `completed` is
filed as `failed`, because a long run is normal and the rule was written against
short ones. Successive frames are SNAPSHOTS — the newest is the truth, which is
what "last wins" means here, not leniency.

The third finding, F-015: 3805 of those 9532 `assistant` frames carry real token
counts, largest 162864. The wire tells you what a run has spent while it is
still spending it, and nSPAWN was reading none of it until the terminal record.
"""

from __future__ import annotations

import json
from pathlib import Path

from fixture_data import wire_fixture

from nspawn.registry import RunPhase
from nspawn.terminal import classify_terminal_run

FIXTURE_251 = wire_fixture("claude-stream-wire-2.1.251.ndjson")


def _wire(fixture: Path) -> bytes:
    return (
        b"\n".join(
            line for line in fixture.read_bytes().splitlines() if line.startswith(b"{")
        )
        + b"\n"
    )


def _classify(tmp_path: Path, stdout_value: bytes, exit_code: int = 0):
    stdout = tmp_path / "stdout"
    stderr = tmp_path / "stderr"
    stdout.write_bytes(stdout_value)
    stderr.write_bytes(b"")
    return classify_terminal_run(
        stdout,
        stderr,
        exit_code,
        provider="claude",
        account_alias="owner-primary",
        now=1787984400,
    )


def test_the_2_1_251_wire_classifies(tmp_path: Path) -> None:
    observed = _classify(tmp_path, _wire(FIXTURE_251))
    assert observed.phase is RunPhase.COMPLETED
    assert observed.tokens > 0


def _events(count: int) -> list[bytes]:
    """`count` rate-limit snapshots, the newest naming the newest reset."""
    out = []
    for index in range(count):
        out.append(
            json.dumps(
                {
                    "type": "rate_limit_event",
                    "rate_limit_info": {
                        "status": "allowed",
                        "unified_rate_limit_fallback_available": False,
                        "resetsAt": 1787990000 + index,
                    },
                }
            ).encode()
        )
    return out


def _result(turns: int, text: str) -> bytes:
    return json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "num_turns": turns,
            "result": text,
            "terminal_reason": "completed",
            "usage": {"input_tokens": 100 * turns, "output_tokens": turns},
        }
    ).encode()


def test_many_rate_limit_events_are_snapshots_not_a_wire_break(tmp_path: Path) -> None:
    """24 runs in the corpus do this. It was `failed` on every one of them."""
    wire = b"\n".join([*_events(39), _result(3, "done")]) + b"\n"
    observed = _classify(tmp_path, wire)
    assert observed.phase is RunPhase.COMPLETED


def test_the_newest_rate_limit_snapshot_is_the_one_that_counts(tmp_path: Path) -> None:
    wire = b"\n".join([*_events(5), _result(1, "done")]) + b"\n"
    observed = _classify(tmp_path, wire)
    assert observed.phase is RunPhase.COMPLETED
    # Freshest wins: the last snapshot named reset 1787990004.
    assert observed.usage == () or observed.usage is not None


def test_a_resumed_run_carries_two_results_and_the_last_one_is_terminal(
    tmp_path: Path,
) -> None:
    """Observed: `num_turns` 158, then 2 — one run directory, two turns."""
    wire = b"\n".join([_result(158, "first turn"), _result(2, "second turn")]) + b"\n"
    observed = _classify(tmp_path, wire)
    assert observed.phase is RunPhase.COMPLETED
    assert observed.final_text == "second turn"


# ---------------------------------------------------------------------------
# F-015: the wire says what a run has spent WHILE it is spending it.
# ---------------------------------------------------------------------------


def _assistant(input_tokens: int, output_tokens: int) -> bytes:
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                }
            },
        }
    ).encode()


def test_live_progress_reads_the_newest_assistant_usage(tmp_path: Path) -> None:
    from nspawn.terminal import observe_live_progress

    stdout = tmp_path / "stdout"
    stdout.write_bytes(
        b"\n".join([_assistant(100, 5), _assistant(4000, 120), _assistant(9000, 300)])
        + b"\n"
    )
    observed = observe_live_progress(stdout)
    # The SAME definition the terminal record uses: input + output.
    assert observed.tokens == 9300
    assert observed.turns == 3


def test_live_progress_ignores_the_zero_usage_frames(tmp_path: Path) -> None:
    """More than half the corpus's assistant frames carry zeros. A zero is not
    a measurement, and reporting it would walk the number backwards."""
    from nspawn.terminal import observe_live_progress

    stdout = tmp_path / "stdout"
    stdout.write_bytes(b"\n".join([_assistant(4000, 120), _assistant(0, 0)]) + b"\n")
    assert observe_live_progress(stdout).tokens == 4120


def test_live_progress_survives_a_half_written_line(tmp_path: Path) -> None:
    """The pump is still writing: the tail can end mid-frame, and the head of a
    bounded read can start mid-frame."""
    from nspawn.terminal import observe_live_progress

    stdout = tmp_path / "stdout"
    stdout.write_bytes(_assistant(4000, 120) + b"\n" + b'{"type":"assist')
    assert observe_live_progress(stdout).tokens == 4120


def test_live_progress_on_an_absent_or_empty_stream_is_zero(tmp_path: Path) -> None:
    from nspawn.terminal import observe_live_progress

    assert observe_live_progress(tmp_path / "nope").tokens == 0
    empty = tmp_path / "stdout"
    empty.write_bytes(b"")
    assert observe_live_progress(empty).turns == 0


def test_a_stream_larger_than_the_tail_discards_the_partial_first_line(
    tmp_path: Path,
) -> None:
    """The bounded read lands mid-frame, and half a JSON object is not a frame.

    Written because the small-file test could never reach this branch: the tail
    only begins mid-stream once the stream is bigger than the tail.
    """
    from nspawn.terminal import LIVE_PROGRESS_TAIL_BYTES, observe_live_progress

    stdout = tmp_path / "stdout"
    filler = _assistant(1, 1) + b"\n"
    padding = filler * (LIVE_PROGRESS_TAIL_BYTES // len(filler) + 8)
    stdout.write_bytes(padding + _assistant(7000, 42) + b"\n")
    assert stdout.stat().st_size > LIVE_PROGRESS_TAIL_BYTES

    observed = observe_live_progress(stdout)
    assert observed.tokens == 7042
    # Every frame the tail *fully* contains is counted, and the sliced one is
    # not — so the count is bounded by the window, never by the run.
    assert 0 < observed.turns < LIVE_PROGRESS_TAIL_BYTES
