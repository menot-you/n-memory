"""Tests for background embedding worker recovery across transient database/operational failures."""

from __future__ import annotations

import threading
from typing import Any

import pytest
from nott_database import DatabaseError
from storefile import fresh_store_path

from nmemory.embedding_index import EmbeddingWorker, spawn_embedding_worker
from nmemory.store import Store

TEST_DB = ""


@pytest.fixture(autouse=True)
def fresh_store() -> None:
    global TEST_DB
    TEST_DB = str(fresh_store_path())


def test_embedding_worker_survives_transient_database_error() -> None:
    """Worker thread survives DatabaseError on first cycle and continues subsequent cycles."""
    store = Store.open(TEST_DB)

    calls = 0
    cycle_ran = threading.Event()

    def mock_cycle(self: Any) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise DatabaseError("synthetic transient database failure")
        cycle_ran.set()
        return 0

    original_cycle = EmbeddingWorker.cycle
    try:
        EmbeddingWorker.cycle = mock_cycle  # type: ignore[method-assign]
        w, t = spawn_embedding_worker(store)
        try:
            # Wait for second cycle to run
            success = cycle_ran.wait(timeout=3.0)
            assert success, "Expected second cycle to execute after first cycle failed"
            assert calls >= 2, f"Expected at least 2 cycles, got {calls}"
            assert t.is_alive(), (
                "Worker thread should be alive after transient DatabaseError"
            )
        finally:
            w.stop()
            t.join(timeout=2.0)
            assert not t.is_alive(), (
                "Worker thread should terminate cleanly when stopped"
            )
    finally:
        EmbeddingWorker.cycle = original_cycle  # type: ignore[method-assign]


def test_embedding_worker_programming_error_is_fatal() -> None:
    """Programming defects (e.g. AttributeError) are not swallowed and terminate the thread."""
    store = Store.open(TEST_DB)

    def buggy_cycle(self: Any) -> int:
        raise AttributeError("synthetic programming bug")

    # The synthetic bug must die INSIDE this test. Unowned, it surfaces as a
    # PytestUnhandledThreadExceptionWarning in whichever report is being
    # written when the thread dies — measured 2026-09-07 in every body-gate
    # run. Recording it through threading.excepthook both fences the warning
    # and asserts, stronger than before, that the thread died OF the bug.
    surfaced: list[BaseException] = []

    def record(args: threading.ExceptHookArgs) -> None:
        if args.exc_value is not None:
            surfaced.append(args.exc_value)

    original_cycle = EmbeddingWorker.cycle
    original_excepthook = threading.excepthook
    threading.excepthook = record
    try:
        EmbeddingWorker.cycle = buggy_cycle  # type: ignore[method-assign]
        w, t = spawn_embedding_worker(store)
        try:
            t.join(timeout=2.0)
            assert not t.is_alive(), (
                "Worker thread should have terminated on programming error"
            )
            assert len(surfaced) == 1, surfaced
            assert isinstance(surfaced[0], AttributeError)
            assert str(surfaced[0]) == "synthetic programming bug"
        finally:
            w.stop()
    finally:
        EmbeddingWorker.cycle = original_cycle  # type: ignore[method-assign]
        threading.excepthook = original_excepthook
