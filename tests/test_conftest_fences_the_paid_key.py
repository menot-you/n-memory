"""The FIFTH fence of `conftest`: the paid OpenAI key.

The `conftest` of this suite exists to fence "the LIVE state of the owner" and already fences
four things: the live store, SigNoz, the live configuration and — since
2026-09-18, for the IDENTICAL reason to this — the paid System1 credential, because
"any test running `cycle()` without resting the switch PAID real calls
to the external service".

`OPENAI_API_KEY` was missing from that list, and the effect was MEASURED on 2026-09-18 while
running `tests/`: each `nmemory_capsule_retrieve` in the suite opened the vector lane, read the
key from the environment (`embedding.py:195`) and hit
`https://api.openai.com/v1/embeddings`, with `HTTP 429` in the log of 998 tests. The
`429` is rate limiting, NOT revocation: `GET /v1/models` with the same key
answered `200`. The owner disabled the key by quota, and the suite continued
calling.

The fence is not `monkeypatch.delenv` per test — there were six of those scattered around and
they prove the problem, they don't solve it: they cover only the test that remembered. The
fence is a single one, in `conftest`, in the same place as the other four.

Whoever WANTS embeddings in a test injects `custom_hook` (the pattern that
`test_scope_fences_applied` uses) or passes `api_key=` manually. Neither of the two
paths passes through the environment, so neither is affected.
"""

from __future__ import annotations

import os
import pathlib

import pytest
from child_env import child_env

from nmemory.embedding import EmbeddingClient
from nmemory.space import DIMENSION, EmbeddingError, SpaceFingerprint


def test_o_ambiente_da_suite_nao_carrega_a_chave() -> None:
    """The variable is not in the environment while the suite runs."""
    assert os.environ.get("OPENAI_API_KEY") is None


def test_o_cliente_por_omissao_nao_fica_configurado() -> None:
    """The ROUTE, not the variable: the real constructor reads the real environment.

    `EmbeddingClient()` without arguments is exactly what the vector lane
    constructs. If the key leaks into the environment, this object becomes configured and
    the next call goes out to the network.
    """
    assert EmbeddingClient().is_configured() is False


def test_embed_query_recusa_em_vez_de_sair_para_a_rede() -> None:
    """The edge of the route: local refusal, with no request made.

    This is the falsifier that matters. The two above could pass with a
    fence that only clears the variable AFTER someone already constructed the
    client; this runs the method that makes the request and requires that it stops before.
    """
    client = EmbeddingClient()

    # The message names the key of the ACTIVE provider, not a fixed one: saying
    # "OPENAI_API_KEY" with the default in Gemini would direct readers to look for the
    # wrong variable. The falsifier remains the same — stop before the
    # request — which is why the zero-requests assertion stays exactly where it is.
    esperada = {
        "gemini": "GEMINI_API_KEY",
        "openai": "OPENAI_API_KEY",
    }[SpaceFingerprint.current().provider]

    with pytest.raises(
        EmbeddingError, match=f"{esperada} environment variable is not set"
    ):
        client.embed_query("a probe that must never reach the provider")

    assert client.usage_stats().openai_requests == 0


def test_a_cerca_tira_uma_chave_QUE_ESTA_no_ambiente() -> None:
    """The only falsifier that bites in ANY environment, and it was needed.

    Independent review (fugu, 2026-09-18) measured that the three tests above are
    VACUOUS in the gate box: there `OPENAI_API_KEY` does not exist and no workflow
    injects it, so they pass with and without the fence. They only had teeth because the
    key was in the environment of THIS session — which is proof that depends on
    who runs it.

    This runs the suite in a SUBPROCESS with an injected sentinel. The `conftest`
    fence must remove it; without the fence, the inner test sees the sentinel
    and fails. Does not touch network: only reads the environment.
    """
    import subprocess
    import sys

    env = child_env()
    env["OPENAI_API_KEY"] = "sentinel-the-fence-must-remove"
    resultado = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:randomly",
            "tests/test_conftest_fences_the_paid_key.py::test_o_ambiente_da_suite_nao_carrega_a_chave",
        ],
        cwd=pathlib.Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        check=False,
        text=True,
        timeout=300,
    )

    assert resultado.returncode == 0, (
        "the conftest fence did not remove the injected key:\n"
        f"{resultado.stdout[-2000:]}\n{resultado.stderr[-2000:]}"
    )


def test_um_hook_continua_a_funcionar_sob_a_cerca() -> None:
    """Control, and only that — not a falsifier.

    The review noted, correctly, that the previous docstring exaggerated: no
    plausible edit to `conftest` breaks a client with `custom_hook`, because
    that path never touches the environment. Retained as control that the fence
    closes the ENVIRONMENT and not the capability, declared as such.
    """
    vector = [1.0] + [0.0] * (DIMENSION - 1)
    client = EmbeddingClient(custom_hook=lambda text: list(vector))

    assert client.is_configured() is True
    assert client.embed_query("sonda com hook") == vector
