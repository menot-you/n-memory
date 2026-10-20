"""Subprocesses in `nmemory` run with a timeout originating from a single place.

The place is `nmemory.subproc.run_with_timeout`: whoever calls brings their timeout (or the
helper default), and a command that sleeps dies instead of hanging the call.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Any, NoReturn

import pytest

from nmemory import staleness, subproc

DORME = "import time; time.sleep(30)"


def test_helper_mata_subprocesso_que_dorme() -> None:
    inicio = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        subproc.run_with_timeout(
            [sys.executable, "-c", DORME],
            timeout=0.5,
            capture_output=True,
        )
    assert time.monotonic() - inicio < 10.0


def test_helper_tem_prazo_por_omissao() -> None:
    inicio = time.monotonic()
    assert 0 < subproc.DEFAULT_TIMEOUT_S < 600
    with pytest.raises(subprocess.TimeoutExpired):
        subproc.run_with_timeout(
            [sys.executable, "-c", DORME],
            capture_output=True,
            timeout=0.5,
        )
    assert time.monotonic() - inicio < 10.0


def test_staleness_corre_pelo_helper(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No `staleness` site escapes the helper: all see the timeout expire."""
    chamadas: list[list[str]] = []

    def dorme(cmd: list[str], **kwargs: Any) -> NoReturn:
        chamadas.append(cmd)
        raise subprocess.TimeoutExpired(cmd, float(kwargs.get("timeout", 0.0)))

    monkeypatch.setattr(staleness, "run_with_timeout", dorme)

    assert staleness.repository_name(tmp_path) is None
    assert staleness.remote_refs_readable(tmp_path) is False
    assert len(chamadas) == 2
