"""System1 — the self-hosted System One model (Laya), behind nSPAWN's three verbs.

THE TRANSPORT NO LONGER LIVES HERE. It moved down to `libs/system1` (`nott_system1`) when
nMEMORY became the second in-tree consumer — two consumers make a lib (D15). What remains in this module is the only thing belonging to nSPAWN: where
the configuration comes from.

THE THREE VERBS PASS THROUGH THIS MODULE'S `ask`, and that is a contract, not a style choice. A
contract test substitutes `system1.ask` to avoid talking to the service (`tests/test_mcp_contract.py`
`_system1_duplo`); wrappers that called the lib's `ask` would bypass that double and
cause the suite to make real network calls — measured on 2026-09-17, two `422`s from the live
service in a test run.

RESOLUTION BELONGS HERE, AND MUST BE APPLIED. The lib receives `endpoint`, `model`, and `timeout`
as required arguments, and it is here that they are resolved against the owner's roster. A
bare re-export of the verbs would leave resolution unapplied without anything failing — an
independent review caught exactly that in the previous design, which is why these wrappers
exist instead of a `from nott_system1 import *`.

THE CREDENTIAL NEVER APPEARS: it comes from the environment, read inside the lib, and is
not an argument to any tool.

ONE QUESTION MAY TAKE ITS OWN ROUTE: the execution-class question passes
`scope="EXECUTION_CLASS"`, and `NOTT_SYSTEM1_EXECUTION_CLASS_{ENDPOINT,MODEL,API_KEY}`,
when set, win over the base route and the roster for that question only.
"""

from __future__ import annotations

from typing import Any

from nott_system1 import (
    AUTH_ENV_PADRAO,
    MAX_QUESTIONS,
    MODELO_PADRAO,
    TIMEOUT_PADRAO,
    Answer,
    Route,
    System1Unavailable,
    env,
)
from nott_system1 import ask as _ask
from nott_system1 import pergunta as _pergunta
from nott_system1 import route as _route

__all__ = [
    "MAX_QUESTIONS",
    "MODELO_PADRAO",
    "TIMEOUT_SECONDS",
    "Answer",
    "System1Unavailable",
    "ask",
    "choice",
    "noul",
    "resolve_auth_env",
    "resolve_endpoint",
    "resolve_model",
    "resolve_timeout",
    "roster_do_host",
    "score",
]

TIMEOUT_SECONDS = TIMEOUT_PADRAO


_ROSTER_LIDO: list[Any] = []


def roster_do_host() -> Any:
    """The host roster, read ONCE.

    Reading the file on every question put an `open()` in the hot path of a batch
    of 64 questions. An nSPAWN process serves one configuration; if it changes,
    the server is restarted -- which is how the rest of its configuration is
    reloaded.
    """
    if not _ROSTER_LIDO:
        from .owner_roster import Roster, RosterError

        try:
            caminho = Roster.default_path()
            _ROSTER_LIDO.append(Roster.read(caminho) if caminho else None)
        except RosterError, OSError:
            # An unreadable roster must not silence System1: the environment
            # continues to respond, and the roster error appears, with its
            # cause, in whatever resolves it for a dispatch. The two classes are
            # what `Roster.read` declares; anything else bubbles up, as it should.
            _ROSTER_LIDO.append(None)
    return _ROSTER_LIDO[0]


def _question_route(scope: str | None) -> Route | None:
    """The question's own route, or None when it takes the base route."""
    if scope is None:
        return None
    escolhida = _route(scope)
    # Without its own route the lib answers the base route, which holds the base key.
    return None if escolhida.auth_env == AUTH_ENV_PADRAO else escolhida


def resolve_endpoint(roster: Any = None, scope: str | None = None) -> str:
    """The active endpoint: the question's own route, environment, then roster; empty
    when none says one.

    There is no factory route: an empty endpoint is refused by the transport as an
    operational `System1Unavailable`, never sent to a public service.

    THE ENVIRONMENT TAKES PRECEDENCE because it points to a double inside a test; a roster
    taking precedence would make any test hostage to machine configuration. The caller
    never enters into this choice — an endpoint chosen by the caller is exfiltration
    by another name.
    """
    propria = _question_route(scope)
    if propria is not None:
        return propria.endpoint or ""
    do_ambiente = env("NOTT_SYSTEM1_ENDPOINT")
    if do_ambiente:
        return do_ambiente
    do_roster = getattr(getattr(roster, "system1", None), "endpoint", None)
    return do_roster or ""


def resolve_model(
    roster: Any = None, pedido: str | None = None, scope: str | None = None
) -> str:
    """The active model: caller request, the question's own route, environment,
    roster, factory."""
    if pedido:
        return pedido
    propria = _question_route(scope)
    if propria is not None:
        return propria.model or MODELO_PADRAO
    do_ambiente = env("NOTT_SYSTEM1_MODEL")
    if do_ambiente:
        return do_ambiente
    do_roster = getattr(getattr(roster, "system1", None), "model", None)
    return do_roster or MODELO_PADRAO


def resolve_timeout(roster: Any = None) -> float:
    do_roster = getattr(getattr(roster, "system1", None), "timeout_seconds", None)
    return float(do_roster) if do_roster else TIMEOUT_SECONDS


def resolve_auth_env(scope: str | None = None) -> str:
    """The NAME of the variable that holds the key of the question's route."""
    return _route(scope).auth_env


def _config(
    model: str | None, timeout: float | None = None, scope: str | None = None
) -> dict[str, Any]:
    roster = roster_do_host()
    return {
        "endpoint": resolve_endpoint(roster, scope),
        "auth_env": resolve_auth_env(scope),
        "model": resolve_model(roster, model, scope),
        # A caller timeout is for its PATH (classification runs inside
        # the stdio loop and cannot wait minutes); without it the roster governs.
        "timeout": resolve_timeout(roster) if timeout is None else timeout,
    }


def ask(
    state: Any,
    questions: dict[str, dict[str, Any]],
    model: str | None = None,
    timeout: float | None = None,
    scope: str | None = None,
) -> dict[str, Answer]:
    """One call, multiple questions, typed answers — with roster config."""
    return _ask(state, questions, **_config(model, timeout, scope))


def noul(
    state: Any, questions: dict[str, str | dict[str, Any]], model: str | None = None
) -> dict[str, Answer]:
    """Yes/no questions where PROBABILITY is the product."""
    return ask(state, {i: _pergunta("noul", q) for i, q in questions.items()}, model)


def choice(
    state: Any,
    questions: dict[str, dict[str, Any]],
    model: str | None = None,
    timeout: float | None = None,
    scope: str | None = None,
) -> dict[str, Answer]:
    """One of a known set of options WITHOUT ordering among them."""
    # `timeout` and `scope` are only passed when present: the `ask` doubles that the
    # suite installs have the three-verb signature, and always passing them broke them.
    extra: dict[str, Any] = {} if timeout is None else {"timeout": timeout}
    if scope is not None:
        extra["scope"] = scope
    return ask(
        state, {i: _pergunta("choice", q) for i, q in questions.items()}, model, **extra
    )


def score(
    state: Any, questions: dict[str, dict[str, Any]], model: str | None = None
) -> dict[str, Answer]:
    """A position on an ORDERED spectrum that you define."""
    return ask(state, {i: _pergunta("score", q) for i, q in questions.items()}, model)
