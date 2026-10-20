"""The environment a test hands to a child process that imports nmemory.

mutmut marks its phases in `MUTANT_UNDER_TEST`, and a child inherits whatever the
parent carries. Two values mean different things to a child:

- a mutant name: the child must run that mutant too, or a test that measures
  behaviour through a child could never kill it. The value passes through.
- `"stats"`: the parent collects, in process, which functions each test reaches
  (`mutmut/__main__.py`, `record_trampoline_hit`). A child's hits land in the
  child's own set, which nobody reads back, and recording them loads mutmut's
  configuration afresh in the child. The value is dropped.

Limitation: because a child's hits are never read back, a function reached only
inside a child shows "no tests" in mutmut's statistics, and mutmut runs none of
its mutants.
"""

import os
from collections.abc import Mapping

_MUTANT = "MUTANT_UNDER_TEST"


def child_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return a copy of `env` (the process environment when None) fit for a child.

    `MUTANT_UNDER_TEST` is dropped only when it is `"stats"`; any other value, and
    every other variable, is kept as it is.
    """
    result = dict(os.environ if env is None else env)
    if result.get(_MUTANT) == "stats":
        del result[_MUTANT]
    return result
