"""The `system1.ask` double that a dispatch with declared `class` requires.

With `class` mandatory on the wire, `Supervisor.execute` calls System1
(`classify.resolve_execution_class`), and System1 talks to the System1 service. A
faker measuring SOMETHING ELSE — kind admission, served effort, read
cage — cannot become dependent on the network: a test that makes network calls is a
broken test. This fixture replaces the module wrapper that the contract pins.

The pattern follows `tests/test_effort_class_policy.py::_answering`, which measures
classification for real. Here it is `autouse` and ALWAYS responds with the declared
class: with observed equal to declared no divergence warning arises, and the
refusal measured by these fakers reaches the test without any suffix.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest

from nspawn import system1

#: The class that these fakers declare: mechanical work and route probes.
#: None of their rosters includes `[classes]`, so it neither sets a ceiling nor
#: reorders the pool — provider is pinned — and the measured point remains intact.
DECLARED_CLASS = "mechanical_fanout"


def answering(
    value: str = DECLARED_CLASS,
) -> Callable[..., dict[str, system1.Answer]]:
    """A `system1.ask` that answers `value` without leaving the machine."""

    def ask(
        state: object,
        questions: dict[str, object],
        model: object = None,
        timeout: float | None = None,
        scope: str | None = None,
    ) -> dict[str, system1.Answer]:
        ident = next(iter(questions))
        return {
            ident: system1.Answer(
                id=ident,
                type="choice",
                value=value,
                probabilities={value: 1.0},
                confidence=1.0,
                legend=None,
            )
        }

    return ask


@pytest.fixture(autouse=True)
def system1_never_reaches_the_network(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Importa-me num modulo cujos despachos atravessam `Supervisor.execute`."""
    monkeypatch.setattr(system1, "ask", answering())
    yield
