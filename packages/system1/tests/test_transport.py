"""Test request construction, response parsing, and typed failures.

Each test replaces the private opener to avoid external requests.
"""

from __future__ import annotations

import http.client
import urllib.error
from typing import Any

import pytest

import nott_system1
from nott_system1 import System1Unavailable, ask, pergunta


def noul(state: Any, questions: dict[str, str], **config: Any) -> object:
    """The yes/no form nSPAWN's wrapper sends, straight through the transport."""
    return ask(state, {i: pergunta("noul", q) for i, q in questions.items()}, **config)


#: Configuration comes in by argument; the lib has nowhere to pull it from on its own.
CONFIG: dict[str, Any] = {
    "endpoint": "https://duplo.invalid/v1/systemone",
    "model": "laya-nott",
    "timeout": 1.0,
}


def test_the_http_error_body_never_carries_the_state(monkeypatch: Any) -> None:
    """An error body may quote the request, and the request carries the `state` — owner
    content. Only the HTTP code and reason go out."""
    segredo_do_owner = "a lei do NOTT, inteira, e o caminho da campanha"

    def rebenta(*a: Any, **k: Any) -> None:
        raise urllib.error.HTTPError(
            url="https://duplo.invalid/v1/systemone",
            code=400,
            msg="Bad Request",
            hdrs=None,
            fp=None,
        )  # type: ignore[arg-type]

    monkeypatch.setenv("NOTT_SYSTEM1_API_KEY", "irrelevant-to-this-test")
    monkeypatch.setattr(nott_system1._OPENER, "open", rebenta)
    with pytest.raises(System1Unavailable) as erro:
        noul(segredo_do_owner, {"q": "?"}, **CONFIG)
    assert segredo_do_owner not in str(erro.value)
    assert "400" in str(erro.value)


def test_a_missing_key_is_named_as_configuration_not_as_an_answer(
    monkeypatch: Any,
) -> None:
    monkeypatch.delenv("NOTT_SYSTEM1_API_KEY", raising=False)
    with pytest.raises(System1Unavailable) as erro:
        noul("x", {"q": "?"}, **CONFIG)
    assert "NOTT_SYSTEM1_API_KEY is not configured" in str(erro.value)


def test_the_batch_has_a_ceiling(monkeypatch: Any) -> None:
    """A batch with no ceiling turns a caller's mistake into a huge request."""
    monkeypatch.setenv("NOTT_SYSTEM1_API_KEY", "irrelevante")
    with pytest.raises(System1Unavailable) as erro:
        noul(
            "x",
            {f"q{i}": "?" for i in range(nott_system1.MAX_QUESTIONS + 1)},
            **CONFIG,
        )
    assert "ceiling" in str(erro.value)


def test_the_key_never_reaches_the_request_body(monkeypatch: Any) -> None:
    """The credential goes in the header and nowhere else."""
    visto: dict[str, Any] = {}

    class Resposta:
        def __enter__(self) -> Any:
            return self

        def __exit__(self, *a: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"answers":{}}'

    def espia(pedido: Any, timeout: float = 0) -> Any:
        visto["corpo"] = pedido.data.decode()
        visto["url"] = pedido.full_url
        visto["timeout"] = timeout
        visto["cabecalhos"] = dict(pedido.headers)
        return Resposta()

    monkeypatch.setenv("NOTT_SYSTEM1_API_KEY", "chave-de-teste-abcdef")
    monkeypatch.setattr(nott_system1._OPENER, "open", espia)
    noul("estado", {"q": "?"}, **CONFIG)
    assert "chave-de-teste-abcdef" not in visto["corpo"]
    assert any("chave-de-teste-abcdef" in str(v) for v in visto["cabecalhos"].values())


def test_the_key_comes_from_the_variable_the_caller_names(monkeypatch: Any) -> None:
    """nSPAWN and nMEMORY ask different services with different keys.

    The caller names the variable; the default stays `NOTT_SYSTEM1_API_KEY`.
    """
    visto: dict[str, Any] = {}

    class Resposta:
        def __enter__(self) -> Any:
            return self

        def __exit__(self, *a: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"answers":{}}'

    def espia(pedido: Any, timeout: float = 0) -> Any:
        visto["authorization"] = pedido.get_header("Authorization")
        return Resposta()

    monkeypatch.setenv("NOTT_SYSTEM1_API_KEY", "default-key")
    monkeypatch.setenv("OTHER_SYSTEM1_KEY", "named-key")
    monkeypatch.setattr(nott_system1._OPENER, "open", espia)
    noul("estado", {"q": "?"}, **CONFIG, auth_env="OTHER_SYSTEM1_KEY")
    assert visto["authorization"] == "Bearer named-key"
    noul("estado", {"q": "?"}, **CONFIG)
    assert visto["authorization"] == "Bearer default-key"
    monkeypatch.delenv("OTHER_SYSTEM1_KEY")
    with pytest.raises(System1Unavailable, match="OTHER_SYSTEM1_KEY is not configured"):
        noul("estado", {"q": "?"}, **CONFIG, auth_env="OTHER_SYSTEM1_KEY")


def test_the_caller_config_is_what_reaches_the_wire(monkeypatch: Any) -> None:
    """The lib does NOT have its own configuration to apply behind the scenes.

    If a default from this lib beat the argument, the caller's resolution — the roster in
    nSPAWN, the file in nMEMORY — would go unapplied without anything failing.
    """
    visto: dict[str, Any] = {}

    class Resposta:
        def __enter__(self) -> Any:
            return self

        def __exit__(self, *a: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"answers":{}}'

    def espia(pedido: Any, timeout: float = 0) -> Any:
        visto["url"] = pedido.full_url
        visto["corpo"] = pedido.data.decode()
        visto["timeout"] = timeout
        return Resposta()

    monkeypatch.setenv("NOTT_SYSTEM1_API_KEY", "irrelevante")
    monkeypatch.setattr(nott_system1._OPENER, "open", espia)
    noul(
        "estado",
        {"q": "?"},
        endpoint="https://outro.invalid/v1",
        model="modelo-do-chamador",
        timeout=7.5,
    )
    assert visto["url"] == "https://outro.invalid/v1"
    assert '"model": "modelo-do-chamador"' in visto["corpo"]
    assert visto["timeout"] == 7.5


@pytest.mark.parametrize("endpoint", ["", "   "])
def test_an_unconfigured_endpoint_is_operational_and_asks_nobody(
    monkeypatch: Any, endpoint: str
) -> None:
    """There is no public default to fall back to: nothing leaves the box."""

    def rede(*a: Any, **k: Any) -> None:
        raise AssertionError("the network was reached")

    monkeypatch.setenv("NOTT_SYSTEM1_API_KEY", "irrelevante")
    monkeypatch.setattr(nott_system1._OPENER, "open", rede)
    with pytest.raises(System1Unavailable, match="no System1 endpoint"):
        noul("estado", {"q": "?"}, **{**CONFIG, "endpoint": endpoint})


#: Every variable a scope route reads, so a test starts from a clean environment.
def _clear_routes(monkeypatch: Any) -> None:
    for scope in (None, *nott_system1.SCOPES):
        for name in nott_system1.route_variables(scope):
            monkeypatch.delenv(name, raising=False)


def test_a_question_without_its_own_route_takes_the_base(monkeypatch: Any) -> None:

    _clear_routes(monkeypatch)
    monkeypatch.setenv("NOTT_SYSTEM1_ENDPOINT", "https://base.invalid/v1")
    monkeypatch.setenv("NOTT_SYSTEM1_MODEL", "base-model")
    assert nott_system1.route("TAINT") == nott_system1.Route(
        endpoint="https://base.invalid/v1",
        model="base-model",
        auth_env="NOTT_SYSTEM1_API_KEY",
    )
    assert nott_system1.route(None) == nott_system1.route("TAINT")


def test_a_question_with_its_own_route_takes_all_three_from_it(
    monkeypatch: Any,
) -> None:
    _clear_routes(monkeypatch)
    monkeypatch.setenv("NOTT_SYSTEM1_ENDPOINT", "https://base.invalid/v1")
    monkeypatch.setenv("NOTT_SYSTEM1_MODEL", "base-model")
    monkeypatch.setenv("NOTT_SYSTEM1_KIND_ENDPOINT", "https://kind.invalid/v1")
    monkeypatch.setenv("NOTT_SYSTEM1_KIND_MODEL", "kind-model")
    monkeypatch.setenv("NOTT_SYSTEM1_KIND_API_KEY", "kind-key")
    assert nott_system1.route("KIND") == nott_system1.Route(
        endpoint="https://kind.invalid/v1",
        model="kind-model",
        auth_env="NOTT_SYSTEM1_KIND_API_KEY",
    )
    assert nott_system1.route("STAGED").auth_env == "NOTT_SYSTEM1_API_KEY"


@pytest.mark.parametrize("missing", ["ENDPOINT", "MODEL", "API_KEY"])
def test_a_partial_question_route_is_refused_by_name(
    monkeypatch: Any, missing: str
) -> None:
    """A scope endpoint with the base key would send one service's key to another."""
    _clear_routes(monkeypatch)
    for part in ("ENDPOINT", "MODEL", "API_KEY"):
        if part != missing:
            monkeypatch.setenv(f"NOTT_SYSTEM1_CURATOR_{part}", "secret-value-x")
    with pytest.raises(System1Unavailable) as erro:
        nott_system1.route("CURATOR")
    assert f"NOTT_SYSTEM1_CURATOR_{missing}" in str(erro.value)
    assert "secret-value-x" not in str(erro.value)


def test_an_unknown_question_is_refused() -> None:
    with pytest.raises(ValueError, match="NOT_A_SCOPE"):
        nott_system1.route("NOT_A_SCOPE")


class _Corpo:
    def __init__(self, corpo: bytes) -> None:
        self.corpo = corpo

    def __enter__(self) -> Any:
        return self

    def __exit__(self, *a: object) -> None:
        return None

    def read(self) -> bytes:
        return self.corpo


def _rebenta(erro: BaseException) -> Any:
    def request_open(*a: Any, **k: Any) -> Any:
        raise erro

    return request_open


@pytest.mark.parametrize(
    "request_open",
    [
        # `getresponse()` is not wrapped in `URLError`: a restart mid-request.
        _rebenta(http.client.RemoteDisconnected("closed")),
        _rebenta(ConnectionResetError("reset")),
        lambda *a, **k: _Corpo(b"\xff\xfe"),
        lambda *a, **k: _Corpo(b"[1, 2]"),
        lambda *a, **k: _Corpo(b'{"answers": ["q"]}'),
        lambda *a, **k: _Corpo(b'{"answers": {"q": 1}}'),
        lambda *a, **k: _Corpo(b'{"answers": {"q": {"type": ["noul"]}}}'),
        lambda *a, **k: _Corpo(b'{"answers": {"q": {"type": {"noul": 1}}}}'),
    ],
)
def test_a_broken_transport_or_body_is_unavailable_not_a_crash(
    monkeypatch: Any, request_open: Any
) -> None:
    """A caller catches `System1Unavailable` to keep its own verdict; any other
    exception from here crosses that fence and fails the caller's whole call."""
    monkeypatch.setenv("NOTT_SYSTEM1_API_KEY", "irrelevante")
    monkeypatch.setattr(nott_system1._OPENER, "open", request_open)
    with pytest.raises(System1Unavailable, match="no usable answer"):
        noul("estado", {"q": "?"}, **CONFIG)


def test_an_endpoint_without_a_scheme_is_unavailable_not_a_crash(
    monkeypatch: Any,
) -> None:
    """A typo in the endpoint (`http//...`) is the configuration failing, not a crash
    that loses the caller's capture."""
    monkeypatch.setenv("NOTT_SYSTEM1_API_KEY", "irrelevante")
    with pytest.raises(System1Unavailable, match="no usable answer"):
        noul("estado", {"q": "?"}, **{**CONFIG, "endpoint": "http//laya.invalid/v1"})
