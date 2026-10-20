"""L4 bounded redacted output-pump: ordering, budget atomicity, and refusal proof.

Every refusal test proves TWO things at once: the typed exception fires, and
no byte of the offending line reached disk or the shared budget — a pump that
appended first and refused second would pass a naive "it raised" check while
still leaking bytes past its declared ceiling.
"""

from __future__ import annotations

import io
import json
import os
import stat
import threading
import time
from typing import BinaryIO

import pytest
from fixture_data import synthetic_token
from hypothesis import given, settings
from hypothesis import strategies as st

from nspawn.errors import NSpawnError
from nspawn.pump import (
    MAX_RUN_OUTPUT_BYTES,
    OutputPump,
    OutputStream,
    PumpLimitExceeded,
    RunOutputBudget,
    _open_private,
)
from nspawn.redact import REDACTION_MARKER, SecretNeedles

SENTINEL = synthetic_token("test_pump:1")


def test_json_escaped_credentials_are_redacted_in_stderr(tmp_path):
    raw = json.dumps({"error": SENTINEL}).replace("sk-ant", r"\u0073k-ant")
    pump = OutputPump(
        io.BytesIO((raw + "\n").encode()),
        tmp_path,
        OutputStream.STDERR,
        SecretNeedles.from_credential(SENTINEL.encode()),
        RunOutputBudget(),
    ).start()
    pump.join(timeout=2)
    result = json.loads(pump.path.read_text())
    assert result == {"error": REDACTION_MARKER}


def _feed_pipe(payload: bytes, chunk_size: int) -> BinaryIO:
    """A read-end fed from a background thread in ``chunk_size`` pieces.

    A real OS pipe, not an in-memory double: it forces the pump's reader
    across genuinely separate ``readline`` calls instead of however
    ``io.BytesIO`` happens to satisfy one, so a test can prove reassembly
    rather than assume it.
    """
    read_fd, write_fd = os.pipe()
    reader = os.fdopen(read_fd, "rb")
    writer = os.fdopen(write_fd, "wb")

    def feed() -> None:
        for start in range(0, len(payload), chunk_size):
            writer.write(payload[start : start + chunk_size])
            writer.flush()
        writer.close()

    threading.Thread(target=feed, daemon=True).start()
    return reader


# --- OutputStream -----------------------------------------------------------


def test_output_stream_values_are_the_file_names() -> None:
    assert OutputStream.STDOUT.value == "stdout"
    assert OutputStream.STDERR.value == "stderr"


# --- PumpLimitExceeded -------------------------------------------------------


def test_pump_limit_exceeded_is_a_typed_nspawn_error_carrying_its_message() -> None:
    error = PumpLimitExceeded("boom")
    assert isinstance(error, NSpawnError)
    assert error.message == "boom"
    assert str(error) == "boom"


# --- RunOutputBudget ----------------------------------------------------------


@pytest.mark.parametrize("maximum_bytes", [0, -1, -1000])
def test_a_non_positive_maximum_is_refused_at_construction(maximum_bytes: int) -> None:
    with pytest.raises(ValueError, match="^maximum_bytes must be positive$"):
        RunOutputBudget(maximum_bytes=maximum_bytes)


def test_a_maximum_of_exactly_one_is_accepted(tmp_path) -> None:
    # Distinguishes the `<= 0` boundary from an off-by-one `<= 1`.
    budget = RunOutputBudget(maximum_bytes=1)
    budget.consume(1)
    with pytest.raises(PumpLimitExceeded):
        budget.consume(1)


def test_default_maximum_matches_the_declared_run_ceiling() -> None:
    budget = RunOutputBudget()
    budget.consume(MAX_RUN_OUTPUT_BYTES)
    with pytest.raises(PumpLimitExceeded):
        budget.consume(1)


def test_consume_refuses_a_negative_count() -> None:
    budget = RunOutputBudget()
    with pytest.raises(ValueError, match="^count must be non-negative$"):
        budget.consume(-1)


def test_consume_accepts_a_zero_count_as_a_no_op() -> None:
    # Distinguishes `< 0` from an off-by-one `<= 0` or `< 1`.
    budget = RunOutputBudget(maximum_bytes=5)
    budget.consume(0)
    budget.consume(5)


def test_consume_at_the_exact_boundary_is_accepted_and_the_next_byte_is_refused() -> (
    None
):
    budget = RunOutputBudget(maximum_bytes=10)
    budget.consume(10)
    with pytest.raises(PumpLimitExceeded):
        budget.consume(1)


def test_a_refused_consume_does_not_partially_reserve_bytes() -> None:
    budget = RunOutputBudget(maximum_bytes=10)
    budget.consume(5)
    with pytest.raises(PumpLimitExceeded):
        budget.consume(6)
    budget.consume(5)


def test_the_overflow_refusal_message_states_the_exact_attempted_and_maximum_bytes() -> (
    None
):
    budget = RunOutputBudget(maximum_bytes=5)
    with pytest.raises(PumpLimitExceeded) as exc_info:
        budget.consume(6)
    assert str(exc_info.value) == "run output budget exceeded: 6 > 5 bytes"


def test_run_output_budget_is_exact_under_concurrent_racing_consumers() -> None:
    """A buggy read-then-write race would let more than half the threads win."""
    threads_count = 200
    budget = RunOutputBudget(maximum_bytes=threads_count // 2)
    barrier = threading.Barrier(threads_count)
    lock = threading.Lock()
    successes = 0

    def worker() -> None:
        nonlocal successes
        barrier.wait()
        try:
            budget.consume(1)
        except PumpLimitExceeded:
            return
        with lock:
            successes += 1

    threads = [threading.Thread(target=worker) for _ in range(threads_count)]
    for one in threads:
        one.start()
    for one in threads:
        one.join(timeout=5)

    assert successes == threads_count // 2


# --- OutputPump: construction -------------------------------------------------


@pytest.mark.parametrize("max_line_bytes", [0, -1])
def test_a_non_positive_max_line_bytes_is_refused_at_construction(
    tmp_path, max_line_bytes
) -> None:
    with pytest.raises(ValueError, match="^max_line_bytes must be positive$"):
        OutputPump(
            io.BytesIO(b""),
            tmp_path,
            OutputStream.STDOUT,
            SecretNeedles.none(),
            RunOutputBudget(),
            max_line_bytes=max_line_bytes,
        )


@pytest.mark.parametrize(
    ("stream", "expected_name"),
    [(OutputStream.STDOUT, "stdout"), (OutputStream.STDERR, "stderr")],
)
def test_path_is_the_stream_name_under_the_run_directory(
    tmp_path, stream, expected_name
) -> None:
    pump = OutputPump(
        io.BytesIO(b""), tmp_path, stream, SecretNeedles.none(), RunOutputBudget()
    )
    assert pump.path == tmp_path / expected_name


def test_join_without_start_is_a_harmless_no_op(tmp_path) -> None:
    pump = OutputPump(
        io.BytesIO(b""),
        tmp_path,
        OutputStream.STDOUT,
        SecretNeedles.none(),
        RunOutputBudget(),
    )
    pump.join(timeout=0.01)
    assert pump.bytes_written == 0


def test_the_worker_thread_is_a_daemon_thread(tmp_path) -> None:
    # A non-daemon worker would hold the interpreter open on a still-blocked
    # live pipe; nothing about this pump's declared contract should do that.
    pump = OutputPump(
        io.BytesIO(b""),
        tmp_path,
        OutputStream.STDOUT,
        SecretNeedles.none(),
        RunOutputBudget(),
    )
    pump.start()
    assert pump._thread is not None
    assert pump._thread.daemon is True
    pump.join(timeout=5)


def test_join_honors_its_own_timeout_even_while_the_worker_stays_blocked(
    tmp_path,
) -> None:
    read_fd, write_fd = os.pipe()
    reader = os.fdopen(read_fd, "rb")
    writer = os.fdopen(write_fd, "wb")  # kept open: the worker blocks waiting for more
    pump = OutputPump(
        reader, tmp_path, OutputStream.STDOUT, SecretNeedles.none(), RunOutputBudget()
    )
    pump.start()
    try:
        started = time.monotonic()
        pump.join(timeout=0.2)
        elapsed = time.monotonic() - started
        assert elapsed < 5.0
    finally:
        writer.close()
    pump.join(timeout=5)


# --- OutputPump: happy path ----------------------------------------------------


def test_empty_input_still_creates_the_file(tmp_path) -> None:
    pump = OutputPump(
        io.BytesIO(b""),
        tmp_path,
        OutputStream.STDOUT,
        SecretNeedles.none(),
        RunOutputBudget(),
    )
    pump.start()
    pump.join(timeout=5)
    assert pump.bytes_written == 0
    assert pump.path.read_bytes() == b""


def test_a_final_line_without_a_terminator_is_still_captured(tmp_path) -> None:
    payload = b"first\nsecond-no-newline"
    pump = OutputPump(
        io.BytesIO(payload),
        tmp_path,
        OutputStream.STDOUT,
        SecretNeedles.none(),
        RunOutputBudget(),
    )
    pump.start()
    pump.join(timeout=5)
    assert pump.path.read_bytes() == payload
    assert pump.bytes_written == len(payload)


def test_a_line_assembled_across_many_small_reads_is_still_correct(tmp_path) -> None:
    payload = b"hello world\n"
    reader = _feed_pipe(payload, chunk_size=3)
    pump = OutputPump(
        reader, tmp_path, OutputStream.STDOUT, SecretNeedles.none(), RunOutputBudget()
    )
    pump.start()
    pump.join(timeout=5)
    assert pump.path.read_bytes() == payload


def test_the_persisted_file_is_mode_0600(tmp_path) -> None:
    pump = OutputPump(
        io.BytesIO(b"hi\n"),
        tmp_path,
        OutputStream.STDOUT,
        SecretNeedles.none(),
        RunOutputBudget(),
    )
    pump.start()
    pump.join(timeout=5)
    assert stat.S_IMODE(pump.path.stat().st_mode) == 0o600


def test_open_private_requests_mode_0600_on_its_own_open_call(
    tmp_path, monkeypatch
) -> None:
    # fchmod is belt-and-suspenders against the ambient umask; the mode
    # argument to os.open itself must ALSO be exactly 0600, closing the brief
    # window between creation and that fchmod call. Neutralizing fchmod here
    # isolates that argument instead of letting it paper over a wrong one.
    monkeypatch.setattr(os, "fchmod", lambda fd, mode: None)
    path = tmp_path / "target"
    handle = _open_private(path)
    handle.close()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_open_private_refuses_a_symlinked_target_rather_than_following_it(
    tmp_path,
) -> None:
    real_target = tmp_path / "elsewhere"
    real_target.write_bytes(b"do not touch")
    link = tmp_path / "linked"
    link.symlink_to(real_target)
    with pytest.raises(OSError):
        _open_private(link)
    assert real_target.read_bytes() == b"do not touch"


def test_a_pre_existing_target_file_is_refused_not_silently_overwritten(
    tmp_path,
) -> None:
    (tmp_path / "stdout").write_bytes(b"already here")
    pump = OutputPump(
        io.BytesIO(b"new\n"),
        tmp_path,
        OutputStream.STDOUT,
        SecretNeedles.none(),
        RunOutputBudget(),
    )
    pump.start()
    with pytest.raises(FileExistsError):
        pump.join(timeout=5)
    assert (tmp_path / "stdout").read_bytes() == b"already here"


def test_a_missing_run_directory_surfaces_as_a_typed_os_error_via_join(
    tmp_path,
) -> None:
    missing = tmp_path / "does-not-exist"
    pump = OutputPump(
        io.BytesIO(b"line\n"),
        missing,
        OutputStream.STDOUT,
        SecretNeedles.none(),
        RunOutputBudget(),
    )
    pump.start()
    with pytest.raises(OSError):
        pump.join(timeout=5)


def test_two_pumps_share_one_budget_without_overrun(tmp_path) -> None:
    out_payload = b"12345\n"
    err_payload = b"1234567890123\n"
    budget = RunOutputBudget(maximum_bytes=len(out_payload) + len(err_payload))
    out_pump = OutputPump(
        io.BytesIO(out_payload),
        tmp_path,
        OutputStream.STDOUT,
        SecretNeedles.none(),
        budget,
    )
    err_pump = OutputPump(
        io.BytesIO(err_payload),
        tmp_path,
        OutputStream.STDERR,
        SecretNeedles.none(),
        budget,
    )
    out_pump.start()
    err_pump.start()
    out_pump.join(timeout=5)
    err_pump.join(timeout=5)
    assert out_pump.bytes_written + err_pump.bytes_written == len(out_payload) + len(
        err_payload
    )
    assert out_pump.path.read_bytes() == out_payload
    assert err_pump.path.read_bytes() == err_payload


def test_bytes_written_is_observable_while_the_worker_is_still_running(
    tmp_path,
) -> None:
    read_fd, write_fd = os.pipe()
    reader = os.fdopen(read_fd, "rb")
    writer = os.fdopen(write_fd, "wb")
    pump = OutputPump(
        reader, tmp_path, OutputStream.STDOUT, SecretNeedles.none(), RunOutputBudget()
    )
    pump.start()
    try:
        writer.write(b"first line\n")
        writer.flush()
        deadline = time.monotonic() + 5
        while pump.bytes_written == 0 and time.monotonic() < deadline:
            time.sleep(0.005)
        assert pump.bytes_written == len(b"first line\n")
    finally:
        writer.close()
    pump.join(timeout=5)
    assert pump.path.read_bytes() == b"first line\n"


# --- OutputPump: refusals -------------------------------------------------------


def test_a_line_longer_than_the_bound_is_refused_before_any_byte_is_appended(
    tmp_path,
) -> None:
    payload = b"x" * 10 + b"\n"
    pump = OutputPump(
        io.BytesIO(payload),
        tmp_path,
        OutputStream.STDOUT,
        SecretNeedles.none(),
        RunOutputBudget(),
        max_line_bytes=4,
    )
    pump.start()
    with pytest.raises(PumpLimitExceeded):
        pump.join(timeout=5)
    assert pump.bytes_written == 0
    assert pump.path.read_bytes() == b""


def test_an_unterminated_line_that_never_fits_is_refused_without_waiting_for_eof(
    tmp_path,
) -> None:
    payload = b"x" * 10  # no newline anywhere: must not block until EOF to refuse
    pump = OutputPump(
        io.BytesIO(payload),
        tmp_path,
        OutputStream.STDOUT,
        SecretNeedles.none(),
        RunOutputBudget(),
        max_line_bytes=4,
    )
    pump.start()
    with pytest.raises(PumpLimitExceeded):
        pump.join(timeout=5)
    assert pump.bytes_written == 0


def test_an_oversized_line_is_refused_without_waiting_for_more_than_the_bound(
    tmp_path,
) -> None:
    # The reader is fed EXACTLY max_line_bytes + 1 bytes, then left open with
    # no terminator and no EOF. A refusal that (incorrectly) waited for a
    # bigger read, or for EOF, would hang here instead of firing on this
    # exact byte count.
    max_line_bytes = 4
    read_fd, write_fd = os.pipe()
    reader = os.fdopen(read_fd, "rb")
    writer = os.fdopen(write_fd, "wb")
    pump = OutputPump(
        reader,
        tmp_path,
        OutputStream.STDOUT,
        SecretNeedles.none(),
        RunOutputBudget(),
        max_line_bytes=max_line_bytes,
    )
    pump.start()
    try:
        writer.write(b"x" * (max_line_bytes + 1))
        writer.flush()
        with pytest.raises(PumpLimitExceeded):
            pump.join(timeout=5)
    finally:
        writer.close()


def test_a_terminator_landing_past_the_bound_is_still_refused(tmp_path) -> None:
    # The newline IS inside the capped read (5 bytes), but the line it closes
    # is still one byte over max_line_bytes=4.
    payload = b"1234\n"
    pump = OutputPump(
        io.BytesIO(payload),
        tmp_path,
        OutputStream.STDOUT,
        SecretNeedles.none(),
        RunOutputBudget(),
        max_line_bytes=4,
    )
    pump.start()
    with pytest.raises(PumpLimitExceeded):
        pump.join(timeout=5)
    assert pump.bytes_written == 0


def test_a_line_at_exactly_the_bound_is_accepted(tmp_path) -> None:
    payload = b"1234\n"  # 5 bytes, including terminator, at max_line_bytes=5
    pump = OutputPump(
        io.BytesIO(payload),
        tmp_path,
        OutputStream.STDOUT,
        SecretNeedles.none(),
        RunOutputBudget(),
        max_line_bytes=5,
    )
    pump.start()
    pump.join(timeout=5)
    assert pump.path.read_bytes() == payload


def test_a_line_length_refusal_never_touches_the_shared_budget(tmp_path) -> None:
    budget = RunOutputBudget(maximum_bytes=5)
    oversized = OutputPump(
        io.BytesIO(b"x" * 10 + b"\n"),
        tmp_path,
        OutputStream.STDOUT,
        SecretNeedles.none(),
        budget,
        max_line_bytes=4,
    )
    oversized.start()
    with pytest.raises(PumpLimitExceeded):
        oversized.join(timeout=5)
    budget.consume(5)  # every byte of the shared ceiling must still be free


def test_the_line_overflow_refusal_message_states_the_declared_bound(tmp_path) -> None:
    pump = OutputPump(
        io.BytesIO(b"12345\n"),
        tmp_path,
        OutputStream.STDOUT,
        SecretNeedles.none(),
        RunOutputBudget(),
        max_line_bytes=4,
    )
    pump.start()
    with pytest.raises(PumpLimitExceeded) as exc_info:
        pump.join(timeout=5)
    assert str(exc_info.value) == (
        "stdout line 1 exceeded the 4 byte line ceiling: "
        "5 bytes read with no terminator"
    )


def test_the_line_overflow_refusal_names_the_stream_the_line_and_both_sizes(
    tmp_path,
) -> None:
    # THE FALSIFIER FOR THE DIAGNOSIS ITSELF. A real run died on this refusal
    # after 690 good events and reached the operator as the bare class name
    # `PumpLimitExceeded`: nothing said it was a line, which line, how big, or
    # against what ceiling, and the only stderr on record named the broken pipe
    # DOWNSTREAM of it. Three good lines precede the offender here so the
    # ordinal cannot be satisfied by a constant.
    pump = OutputPump(
        io.BytesIO(b"a\nb\nc\n" + b"x" * 9 + b"\n"),
        tmp_path,
        OutputStream.STDERR,
        SecretNeedles.none(),
        RunOutputBudget(),
        max_line_bytes=4,
    )
    pump.start()
    with pytest.raises(PumpLimitExceeded) as exc_info:
        pump.join(timeout=5)
    message = str(exc_info.value)
    assert "stderr" in message, "the refusal must name WHICH stream overflowed"
    assert "line 4" in message, "the refusal must name WHICH line overflowed"
    assert "5 bytes read" in message, "the refusal must state the measured bytes"
    assert "4 byte line ceiling" in message, "the refusal must state the ceiling"
    assert pump.path.read_bytes() == b"a\nb\nc\n"


def test_aggregate_budget_overflow_refuses_before_appending_the_offending_line(
    tmp_path,
) -> None:
    budget = RunOutputBudget(maximum_bytes=6)
    payload = b"12345\n" + b"more\n"  # first line exactly fills the budget
    pump = OutputPump(
        io.BytesIO(payload), tmp_path, OutputStream.STDOUT, SecretNeedles.none(), budget
    )
    pump.start()
    with pytest.raises(PumpLimitExceeded):
        pump.join(timeout=5)
    assert pump.bytes_written == 6
    assert pump.path.read_bytes() == b"12345\n"


# --- OutputPump: redaction -------------------------------------------------------


def test_a_secret_in_a_line_never_reaches_the_file(tmp_path) -> None:
    needles = SecretNeedles((SENTINEL,))
    payload = f"token={SENTINEL}\n".encode()
    pump = OutputPump(
        io.BytesIO(payload), tmp_path, OutputStream.STDOUT, needles, RunOutputBudget()
    )
    pump.start()
    pump.join(timeout=5)
    written = pump.path.read_bytes()
    assert SENTINEL.encode() not in written
    assert REDACTION_MARKER.encode() in written


def test_a_secret_split_across_two_reads_within_one_line_is_still_redacted(
    tmp_path,
) -> None:
    needles = SecretNeedles((SENTINEL,))
    payload = f"leak {SENTINEL} here\n".encode()
    split_at = payload.index(SENTINEL.encode()) + len(SENTINEL) // 2
    reader = _feed_pipe(payload, chunk_size=split_at)
    pump = OutputPump(reader, tmp_path, OutputStream.STDOUT, needles, RunOutputBudget())
    pump.start()
    pump.join(timeout=5)
    written = pump.path.read_bytes()
    assert SENTINEL.encode() not in written
    assert REDACTION_MARKER.encode() in written


def test_an_empty_needle_set_leaves_content_byte_identical(tmp_path) -> None:
    payload = b"nothing secret here\n"
    pump = OutputPump(
        io.BytesIO(payload),
        tmp_path,
        OutputStream.STDOUT,
        SecretNeedles.none(),
        RunOutputBudget(),
    )
    pump.start()
    pump.join(timeout=5)
    assert pump.path.read_bytes() == payload


# --- Deterministic fuzz corpus: no unhandled exception, ever ---------------------

_FUZZ_MAX_LINE_BYTES = 256

#: A committed, deterministic corpus — versioned by being in this file, and
#: seedless by construction: every case is enumerated, not sampled.
_FUZZ_CORPUS: tuple[bytes, ...] = (
    b"",
    b"\n",
    b"\n\n\n",
    b"no newline at all",
    b"\x00\x01\x02\xff\xfe\n",
    "café ☃ snowman\n".encode(),
    b"\xff\xfe\xfd invalid utf-8 lead bytes\n",
    b"x" * (_FUZZ_MAX_LINE_BYTES - 1) + b"\n",  # exactly at the boundary: accepted
    b"x" * (_FUZZ_MAX_LINE_BYTES + 1) + b"\n",  # one byte over: refused
    b"a\nb\nc\n" * 100,
    (b"y" * 40 + b"\n") * 50,
    b"\r\n" * 10,
    b"unterminated-final-line-" + b"z" * 30,
    b"z" * 10_000,  # never terminates, far past the bound: refused without EOF
)


def test_deterministic_fuzz_corpus_reaches_only_clean_completion_or_a_typed_refusal(
    tmp_path_factory,
) -> None:
    # Content fidelity for arbitrary (possibly invalid-UTF-8) bytes is not the
    # claim here — decode-then-redact-then-reencode is representation-
    # independent by design (see redact.py) and is not required to be
    # byte-identical for invalid input. The claim under test is narrower and
    # absolute: every one of these adversarial payloads ends in a clean
    # completion or a typed `PumpLimitExceeded`, never anything else.
    accepted = 0
    refused = 0
    for payload in _FUZZ_CORPUS:
        run_dir = tmp_path_factory.mktemp("fuzz")
        pump = OutputPump(
            io.BytesIO(payload),
            run_dir,
            OutputStream.STDOUT,
            SecretNeedles.none(),
            RunOutputBudget(),
            max_line_bytes=_FUZZ_MAX_LINE_BYTES,
        )
        pump.start()
        try:
            pump.join(timeout=10)
        except PumpLimitExceeded:
            refused += 1
            continue
        accepted += 1
        assert pump.path.exists()

    assert accepted > 0
    assert refused > 0


# --- Property tests --------------------------------------------------------------


@settings(max_examples=50, deadline=None)
@given(
    lines=st.lists(
        st.text(max_size=50).filter(lambda s: "\n" not in s),
        max_size=20,
    )
)
def test_property_a_clean_run_persists_exactly_the_input_and_reports_its_size(
    tmp_path_factory, lines: list[str]
) -> None:
    # Text, not arbitrary binary: valid UTF-8 round-trips byte-identically
    # through decode-then-redact-then-reencode, which is the invariant this
    # property actually targets. Invalid-UTF-8 fidelity is covered, and
    # explicitly NOT required, by the deterministic fuzz corpus above.
    payload = "".join(line + "\n" for line in lines).encode("utf-8")
    run_dir = tmp_path_factory.mktemp("pump")
    pump = OutputPump(
        io.BytesIO(payload),
        run_dir,
        OutputStream.STDOUT,
        SecretNeedles.none(),
        RunOutputBudget(),
    )
    pump.start()
    pump.join(timeout=10)
    assert pump.bytes_written == len(payload)
    assert pump.path.read_bytes() == payload


@settings(max_examples=100, deadline=None)
@given(
    text=st.text(max_size=300),
    max_line_bytes=st.integers(min_value=1, max_value=64),
)
def test_property_no_persisted_line_ever_exceeds_its_declared_bound(
    tmp_path_factory, text: str, max_line_bytes: int
) -> None:
    # `\n` never appears inside a UTF-8 continuation byte, so splitting the
    # encoded payload on it can never fragment a multi-byte character —
    # every line decodes cleanly, keeping this a meaningful byte-identity
    # check rather than one only true by accident of `errors="replace"`.
    payload = text.encode("utf-8")
    run_dir = tmp_path_factory.mktemp("pump")
    pump = OutputPump(
        io.BytesIO(payload),
        run_dir,
        OutputStream.STDOUT,
        SecretNeedles.none(),
        RunOutputBudget(),
        max_line_bytes=max_line_bytes,
    )
    pump.start()
    try:
        pump.join(timeout=10)
    except PumpLimitExceeded:
        return
    written = pump.path.read_bytes()
    assert written == payload
    for line in written.splitlines(keepends=True):
        assert len(line) <= max_line_bytes
