"""L2 provider-ranking contract.

Ranking is pure ordering over an already-eligible pool: no wall lookups, no
``Kind`` filtering, no mutation of the input. Eligibility is the picker's job
(``pick_execution`` in ``picker.py``); this module only answers "in what
order".

The execution-class vocabulary is owned here as plain strings. Any string
outside this vocabulary, including ``None``, falls to the name-stable fallback.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from .owner_roster import UNCLASSIFIED as _floor
from .owner_roster import Provider

#: Judgment, planning, and review all rank by descending ``strength``.
JUDGMENT = "judgment"
PLANNING = "planning"
REVIEW = "review"
#: Mechanical fan-out ranks by descending ``thrift``.
MECHANICAL_FANOUT = "mechanical_fanout"
#: Design work ranks by descending ``design``.
DESIGN = "design"
#: Verification excludes the producer, then ranks by descending ``strength``.
VERIFICATION = "verification"
#: Deterministic work carries no preference; ordering is name-stable.
DETERMINISTIC = "deterministic"
#: Implementation ranks by descending ``strength``. Declared last because this
#: order is the order System1 is asked in, and Laya's gold set asks it last.
IMPLEMENTATION = "implementation"

#: One axis accessor per ranked class. A class absent from this table
#: (``DETERMINISTIC``, ``None``, or any unrecognized string) is unclassified.
_AXES: dict[str, Callable[[Provider], int | None]] = {
    JUDGMENT: lambda provider: provider.strength,
    PLANNING: lambda provider: provider.strength,
    REVIEW: lambda provider: provider.strength,
    IMPLEMENTATION: lambda provider: provider.strength,
    VERIFICATION: lambda provider: provider.strength,
    MECHANICAL_FANOUT: lambda provider: provider.thrift,
    DESIGN: lambda provider: provider.design,
}


def _by_name(provider: Provider) -> str:
    return provider.name


def rank_providers(
    providers: Sequence[Provider],
    execution_class: str | None,
    *,
    producer: str | None = None,
) -> list[Provider]:
    """Rank providers by ``strength``, ``thrift`` or ``design`` for the class.

    Verification removes ``producer`` from the pool by name before ranking;
    every other class ranks the pool as given. Within a ranked axis, a
    provider that never declared it sorts strictly after every provider that
    did — ``None`` is undeclared, never a zero and never a one — and ties at
    either level break on provider name for a deterministic result.
    """
    pool = list(providers)
    if execution_class == VERIFICATION:
        pool = [provider for provider in pool if provider.name != producer]

    # The floor ranks by thrift, like `mechanical_fanout`. It is the class a
    # dispatch falls to when the System1 cannot say what the work is, and the
    # owner's rule for that fall is "fail closed at the lowest ceiling".
    # Name-stable made the fall arbitrary — the alphabetically first provider
    # is not the thriftiest, so the fall could land on a DEARER route than the
    # declared class would have. Found by two independent reviews, 2026-09-21.
    #
    # It sits HERE and not in `_AXES`, because that table is the vocabulary
    # `EXECUTION_CLASSES` is built from and `unclassified` is not sayable on
    # the wire. For the same reason the import is aliased LOWERCASE: `mcp`
    # builds that enum by sweeping every uppercase string in this module's
    # namespace, so an imported `UNCLASSIFIED` would land on the wire just by
    # being named. `test_mcp_l6` catches it, and did.
    name = MECHANICAL_FANOUT if execution_class == _floor else execution_class
    axis = _AXES.get(name) if name is not None else None
    if axis is None:
        return sorted(pool, key=_by_name)

    declared: list[tuple[int, Provider]] = []
    undeclared: list[Provider] = []
    for provider in pool:
        value = axis(provider)
        if value is None:
            undeclared.append(provider)
        else:
            declared.append((value, provider))
    declared.sort(key=lambda item: (-item[0], item[1].name))
    undeclared.sort(key=_by_name)
    return [provider for _, provider in declared] + undeclared
