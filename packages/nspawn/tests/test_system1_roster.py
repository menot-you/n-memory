"""System1 declares itself in the owner roster, like everything else in nSPAWN.

Before this it lived ONLY in environment variables: an endpoint and model that did not
appear in the only file the owner reads to know what nSPAWN does.
"""

from __future__ import annotations

import pytest

from nspawn import system1
from nspawn.owner_roster import Roster, RosterError


def _roster(tmp_path, corpo: str):
    caminho = tmp_path / "spawn.toml"
    caminho.write_text(corpo, encoding="utf-8")
    return caminho


def test_the_roster_declares_the_system1_endpoint_and_model(tmp_path):
    caminho = _roster(
        tmp_path,
        '[system1]\nendpoint = "https://exemplo.invalid/v1/systemone"\n'
        'model = "system1-2"\ntimeout_seconds = 9.5\n',
    )
    table = Roster.read(caminho).system1
    assert table.endpoint == "https://exemplo.invalid/v1/systemone"
    assert table.model == "system1-2"
    assert table.timeout_seconds == 9.5


def test_a_roster_without_a_system1_table_declares_no_endpoint(tmp_path, monkeypatch):
    """Absent table is NOT DECLARED, not "same as default".

    The difference matters: `None` lets environment decide; a default recorded in
    the roster would take precedence over it. And there is no public route to fall
    back to: the endpoint stays empty, which the transport refuses as operational.
    """
    monkeypatch.delenv("NOTT_SYSTEM1_ENDPOINT", raising=False)
    monkeypatch.delenv("NOTT_SYSTEM1_MODEL", raising=False)
    roster = Roster.read(_roster(tmp_path, '[defaults]\nmodel = "x"\n'))
    assert roster.system1.endpoint is None
    assert roster.system1.model is None
    assert system1.resolve_endpoint(roster) == ""
    assert system1.resolve_model(roster) == system1.MODELO_PADRAO


def test_the_old_table_name_is_refused_by_name(tmp_path):
    """Unknown tables are ignored in the roster, so the old name would lose its endpoint
    in silence; it is refused by that name instead."""
    caminho = _roster(tmp_path, '[jev]\nendpoint = "https://roster.invalid/v1"\n')
    with pytest.raises(RosterError, match=r"\[jev\] was renamed to \[system1\]"):
        Roster.read(caminho)


@pytest.mark.parametrize("chave", ["key", "api_key", "token", "authorization"])
def test_a_credential_in_the_roster_is_refused_by_name(tmp_path, chave):
    """The roster is a readable config file; the key does not live here."""
    caminho = _roster(tmp_path, f'[system1]\n{chave} = "seja-o-que-for"\n')
    with pytest.raises(RosterError):
        Roster.read(caminho)


def test_the_environment_still_wins_over_the_roster(tmp_path, monkeypatch):
    """The variable exists to point to a double in tests; it continues to govern."""
    monkeypatch.setenv("NOTT_SYSTEM1_ENDPOINT", "https://duplo.invalid/v1")
    caminho = _roster(tmp_path, '[system1]\nendpoint = "https://roster.invalid/v1"\n')
    assert system1.resolve_endpoint(Roster.read(caminho)) == "https://duplo.invalid/v1"


def test_without_the_environment_the_roster_decides(tmp_path, monkeypatch):
    monkeypatch.delenv("NOTT_SYSTEM1_ENDPOINT", raising=False)
    caminho = _roster(tmp_path, '[system1]\nendpoint = "https://roster.invalid/v1"\n')
    assert system1.resolve_endpoint(Roster.read(caminho)) == "https://roster.invalid/v1"


def _espia_do_fio(monkeypatch):
    """Replace the wire; return what each request carried."""
    import nott_system1

    vistos: list[dict[str, str]] = []

    class Resposta:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def read(self):
            return b'{"answers":{}}'

    def espia(pedido, timeout=0):
        vistos.append(
            {
                "url": pedido.full_url,
                "authorization": pedido.get_header("Authorization"),
                "body": pedido.data.decode(),
            }
        )
        return Resposta()

    monkeypatch.setattr(nott_system1._OPENER, "open", espia)
    return vistos


_PERGUNTA = {"q": {"instructions": "?", "criteria": {"a": "a"}}}


def test_the_execution_class_question_takes_its_own_route(tmp_path, monkeypatch):

    roster = Roster.read(
        _roster(tmp_path, '[system1]\nendpoint = "https://roster.invalid/v1"\n')
    )
    monkeypatch.setattr(system1, "_ROSTER_LIDO", [roster])
    monkeypatch.setenv("NOTT_SYSTEM1_ENDPOINT", "https://base.invalid/v1")
    monkeypatch.setenv("NOTT_SYSTEM1_MODEL", "base-model")
    monkeypatch.setenv("NOTT_SYSTEM1_API_KEY", "base-key")
    monkeypatch.setenv(
        "NOTT_SYSTEM1_EXECUTION_CLASS_ENDPOINT", "https://own.invalid/v1"
    )
    monkeypatch.setenv("NOTT_SYSTEM1_EXECUTION_CLASS_MODEL", "own-model")
    monkeypatch.setenv("NOTT_SYSTEM1_EXECUTION_CLASS_API_KEY", "own-key")
    vistos = _espia_do_fio(monkeypatch)

    system1.choice("estado", _PERGUNTA, scope="EXECUTION_CLASS")
    system1.choice("estado", _PERGUNTA)

    assert vistos[0]["url"] == "https://own.invalid/v1"
    assert vistos[0]["authorization"] == "Bearer own-key"
    assert '"model": "own-model"' in vistos[0]["body"]
    assert vistos[1]["url"] == "https://base.invalid/v1"
    assert vistos[1]["authorization"] == "Bearer base-key"
    assert '"model": "base-model"' in vistos[1]["body"]


def test_without_its_own_route_the_question_takes_the_base(tmp_path, monkeypatch):
    roster = Roster.read(
        _roster(tmp_path, '[system1]\nendpoint = "https://roster.invalid/v1"\n')
    )
    monkeypatch.setattr(system1, "_ROSTER_LIDO", [roster])
    for parte in ("ENDPOINT", "MODEL", "API_KEY"):
        monkeypatch.delenv(f"NOTT_SYSTEM1_EXECUTION_CLASS_{parte}", raising=False)
    monkeypatch.delenv("NOTT_SYSTEM1_ENDPOINT", raising=False)
    monkeypatch.delenv("NOTT_SYSTEM1_MODEL", raising=False)
    monkeypatch.setenv("NOTT_SYSTEM1_API_KEY", "base-key")
    vistos = _espia_do_fio(monkeypatch)

    system1.choice("estado", _PERGUNTA, scope="EXECUTION_CLASS")

    assert vistos[0]["url"] == "https://roster.invalid/v1"
    assert vistos[0]["authorization"] == "Bearer base-key"
    assert system1.resolve_model(roster, scope="EXECUTION_CLASS") == (
        system1.MODELO_PADRAO
    )


def test_a_partial_execution_class_route_asks_nobody(tmp_path, monkeypatch):
    """Its endpoint with the base key would send one service's key to another."""
    monkeypatch.setattr(system1, "_ROSTER_LIDO", [None])
    monkeypatch.setenv("NOTT_SYSTEM1_API_KEY", "base-key")
    monkeypatch.setenv(
        "NOTT_SYSTEM1_EXECUTION_CLASS_ENDPOINT", "https://own.invalid/v1"
    )
    monkeypatch.setenv("NOTT_SYSTEM1_EXECUTION_CLASS_MODEL", "own-model")
    monkeypatch.delenv("NOTT_SYSTEM1_EXECUTION_CLASS_API_KEY", raising=False)
    vistos = _espia_do_fio(monkeypatch)

    with pytest.raises(system1.System1Unavailable, match="EXECUTION_CLASS_API_KEY"):
        system1.choice("estado", _PERGUNTA, scope="EXECUTION_CLASS")
    assert vistos == []


def test_a_partial_route_reaches_the_classifier_as_unavailable(monkeypatch):
    """The refusal surfaces on the dispatch path, with the missing name, asking nobody."""
    from nspawn import classify

    monkeypatch.setattr(system1, "_ROSTER_LIDO", [None])
    monkeypatch.setenv("NOTT_SYSTEM1_API_KEY", "base-key")
    monkeypatch.setenv(
        "NOTT_SYSTEM1_EXECUTION_CLASS_ENDPOINT", "https://own.invalid/v1"
    )
    monkeypatch.setenv("NOTT_SYSTEM1_EXECUTION_CLASS_MODEL", "own-model")
    monkeypatch.delenv("NOTT_SYSTEM1_EXECUTION_CLASS_API_KEY", raising=False)
    vistos = _espia_do_fio(monkeypatch)

    observed, confidence, reason = classify._observe("run the suite", Roster())

    assert (observed, confidence) == (classify.UNCLASSIFIED, None)
    assert reason is not None and "EXECUTION_CLASS_API_KEY" in reason
    assert vistos == []


_RAW_VERIFICATION = {
    "review": 0.30,
    "verification": 0.40,
    "design": 0.03,
    "deterministic": 0.03,
    "implementation": 0.03,
    "judgment": 0.03,
    "mechanical_fanout": 0.03,
    "planning": 0.03,
    "nenhuma": 0.02,
}


@pytest.mark.parametrize(
    "own_model,base_model,expected",
    [
        ("subq-snap-preview", "laya-nott", "review"),
        ("laya-nott", "subq-snap-preview", "verification"),
    ],
)
def test_the_bias_follows_the_model_of_the_question_route(
    monkeypatch, own_model, base_model, expected
):

    from nspawn import classify

    monkeypatch.setattr(system1, "_ROSTER_LIDO", [None])
    monkeypatch.setenv("NOTT_SYSTEM1_MODEL", base_model)
    monkeypatch.setenv(
        "NOTT_SYSTEM1_EXECUTION_CLASS_ENDPOINT", "https://own.invalid/v1"
    )
    monkeypatch.setenv("NOTT_SYSTEM1_EXECUTION_CLASS_MODEL", own_model)
    monkeypatch.setenv("NOTT_SYSTEM1_EXECUTION_CLASS_API_KEY", "own-key")

    def responde(state, questions, model=None, timeout=None, scope=None):
        assert scope == "EXECUTION_CLASS"
        ident = next(iter(questions))
        return {
            ident: system1.Answer(
                id=ident,
                type="choice",
                value="verification",
                probabilities=_RAW_VERIFICATION,
                confidence=0.6,
            )
        }

    monkeypatch.setattr(system1, "ask", responde)
    observed, _, _ = classify._observe(
        "check the candidate against acceptance", Roster()
    )
    assert observed == expected
