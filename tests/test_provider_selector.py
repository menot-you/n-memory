"""Owner decision (2026-09-18): curator gateway is chosen by prefix.

`nspawn-gateway.env` writes Z.ai, MiniMax, and Kimi with the same form
`<PREFIX>_BASE_URL/_API_KEY/_DEFAULT_MODEL/_FAST_MODEL`; switching providers is
one line in `.env`, not manual export of Anthropic format.
"""

from __future__ import annotations

import pytest

from nmemory.provider import PROVIDER, has_provider, provider_env, provider_name

ZAI = {"ZAI_API_KEY": "z", "ZAI_BASE_URL": "https://z", "ZAI_DEFAULT_MODEL": "glm"}
MINIMAX = {
    "MINIMAX_API_KEY": "m",
    "MINIMAX_BASE_URL": "https://m/anthropic",
    "MINIMAX_DEFAULT_MODEL": "MiniMax-M3",
    "MINIMAX_FAST_MODEL": "MiniMax-fast",
}


def test_ausente_e_zai() -> None:
    assert provider_name({}) == "zai"
    env = provider_env({**ZAI, **MINIMAX})
    assert env["ANTHROPIC_BASE_URL"] == "https://z"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "z"
    assert env["ANTHROPIC_MODEL"] == "glm"


def test_minimax_escolhido_mapeia_o_seu_prefixo() -> None:
    base = {**ZAI, **MINIMAX, PROVIDER: "minimax", "CLAUDE_CODE_OAUTH_TOKEN": "o"}
    assert has_provider(base)
    env = provider_env(base)
    assert env["ANTHROPIC_BASE_URL"] == "https://m/anthropic"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "m"
    assert env["ANTHROPIC_MODEL"] == "MiniMax-M3"
    assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "MiniMax-fast"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env  # the owner's account does not travel


def test_escolhido_sem_as_suas_variaveis_nao_e_provedor() -> None:
    """Choosing MiniMax with only Z.ai in env does not blindly fall back to Z.ai."""
    env = {**ZAI, PROVIDER: "minimax"}
    assert not has_provider(env)
    assert "ANTHROPIC_BASE_URL" not in provider_env(env)


def test_kimi_sem_modelo_declarado_nao_fixa_modelo() -> None:
    env = provider_env(
        {"KIMI_API_KEY": "k", "KIMI_BASE_URL": "https://k", PROVIDER: "kimi"}
    )
    assert env["ANTHROPIC_AUTH_TOKEN"] == "k"
    assert "ANTHROPIC_MODEL" not in env


def test_nome_fora_do_vocabulario_e_erro_tipado() -> None:
    with pytest.raises(ValueError, match="minimax"):
        provider_name({PROVIDER: "codex"})
    assert not has_provider({**ZAI, PROVIDER: "codex"})
    # A bad selector turns the judge off; an Anthropic key does not rescue it.
    assert not has_provider({PROVIDER: "claude", "ANTHROPIC_API_KEY": "k"})


def test_a_forma_anthropic_exportada_a_mao_continua_a_mandar() -> None:
    env = provider_env(
        {
            **MINIMAX,
            PROVIDER: "minimax",
            "ANTHROPIC_BASE_URL": "https://mine",
            "ANTHROPIC_AUTH_TOKEN": "t",
        }
    )
    assert env["ANTHROPIC_BASE_URL"] == "https://mine"
