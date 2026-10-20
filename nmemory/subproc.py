"""The only `subprocess.run` in `nmemory`: no command runs without a timeout.

A child process without a timeout does not fail — it hangs, and takes down with it the turn that
called it. Measured in this tree: nine `subprocess.run`, two without any `timeout=` and
five with the number written by hand at the call site, where nobody finds it
to change it. Here there is ONE place where the timeout exists and a vocabulary that says what
each one is for; callers name the timeout instead of writing the number, and callers
with no reason to choose inherit `DEFAULT_TIMEOUT_S`.

`subprocess.TimeoutExpired` is a `SubprocessError` and bubbles up to the caller: this
module sets the timeout, it does not decide what to do when it expires.
"""

from __future__ import annotations

import subprocess
from typing import Any, Final, Literal, overload

#: The timeout for callers with no reason to choose another.
DEFAULT_TIMEOUT_S: float = 30.0

#: A git query in the local tree — `rev-parse`, `log`, `show`.
GIT_QUERY_S: Final = 10.0
#: Find the repository root: answer immediately, or it is of no use.
REPO_ROOT_S: Final = 3.0
#: Run an executable from the repository itself to probe it.
PROBE_S: Final = 3.0
#: The curator pass over the tree before judging.
CURATOR_S: Final = 5.0
#: A health check served to a caller expecting an immediate response.
HEALTH_S: Final = 2.0
#: A call to `gh`, which crosses the network and vendor authentication.
NETWORK_S: Final = 30.0


@overload
def run_with_timeout(
    cmd: list[str],
    *,
    text: Literal[True],
    timeout: float = DEFAULT_TIMEOUT_S,
    check: bool = False,
    **kwargs: Any,
) -> subprocess.CompletedProcess[str]: ...


@overload
def run_with_timeout(
    cmd: list[str],
    *,
    text: Literal[False] = False,
    encoding: None = None,
    errors: None = None,
    universal_newlines: Literal[False] | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    check: bool = False,
    **kwargs: Any,
) -> subprocess.CompletedProcess[bytes]: ...


@overload
def run_with_timeout(
    cmd: list[str],
    *,
    timeout: float = DEFAULT_TIMEOUT_S,
    check: bool = False,
    **kwargs: Any,
) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]: ...


def run_with_timeout(
    cmd: list[str],
    *,
    timeout: float = DEFAULT_TIMEOUT_S,
    check: bool = False,
    **kwargs: Any,
) -> subprocess.CompletedProcess[Any]:
    """Runs `cmd` with a timeout. This is the only place in the package that calls `subprocess.run`."""
    if not (timeout > 0):
        raise ValueError(f"timeout must be positive, got {timeout!r}")
    return subprocess.run(cmd, timeout=timeout, check=check, **kwargs)
