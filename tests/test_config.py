"""`~/.nott/memory.toml`: the typed [system1] and [github] tables.

The environment variable names the PATH to the file, never a value. The store
is not configured here: it is one SQLite file under `NOTT_DATABASE_DIR`, and a
`[store]` table left by the retired PostgreSQL store is ignored, not refused.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from nott_system1 import MODELO_PADRAO, TIMEOUT_PADRAO
from storefile import sample_capsule_id

from nmemory.config import (
    DEFAULT_CONFIG_PATH,
    ConfigError,
    System1Config,
    github_table,
    resolve_system1_config,
)


def test_default_path_is_under_the_nott_home() -> None:
    assert DEFAULT_CONFIG_PATH.name == "memory.toml"
    assert DEFAULT_CONFIG_PATH.parent.name == ".nott"


def test_a_retired_store_table_is_ignored_not_refused(tmp_path: Path) -> None:
    """Every live `memory.toml` still carries `[store]`; it must keep loading."""
    cfg = tmp_path / "memory.toml"
    cfg.write_text(
        '[store]\nurl = "a-retired-database-url"\n\n[system1]\nmodel = "system1-do-arquivo"\n'
    )
    assert resolve_system1_config(cfg).model == "system1-do-arquivo"


def test_an_unknown_table_is_still_refused(tmp_path: Path) -> None:
    cfg = tmp_path / "memory.toml"
    cfg.write_text('[embeddings]\nmodel = "x"\n')
    with pytest.raises(ConfigError) as excinfo:
        resolve_system1_config(cfg)
    assert "embeddings" in str(excinfo.value)


SYSTEM1 = (
    "[system1]\n"
    'endpoint = "http://laya.invalid:18031/v1/systemone"\n'
    'model = "system1-do-arquivo"\n'
    "timeout_seconds = 90.0\n"
)


def _sem_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOTT_SYSTEM1_ENDPOINT", raising=False)
    monkeypatch.delenv("NOTT_SYSTEM1_MODEL", raising=False)


def test_the_system1_table_is_read_whole(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _sem_env(monkeypatch)
    cfg = tmp_path / "memory.toml"
    cfg.write_text(SYSTEM1)
    system1 = resolve_system1_config(cfg)
    assert system1.endpoint == "http://laya.invalid:18031/v1/systemone"
    assert system1.model == "system1-do-arquivo"
    assert system1.timeout_seconds == 90.0


def test_a_missing_file_falls_back_to_the_library_defaults(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no file and no [system1] there is no defect: the factory model and timeout
    respond, the endpoint stays empty (no public default) and the curator question is off."""
    _sem_env(monkeypatch)
    ausente = resolve_system1_config(tmp_path / "ausente.toml")
    sem_tabela_path = tmp_path / "memory.toml"
    sem_tabela_path.write_text("")
    sem_tabela = resolve_system1_config(sem_tabela_path)
    assert ausente == sem_tabela
    assert ausente == System1Config("", MODELO_PADRAO, TIMEOUT_PADRAO, curator=False)


def test_the_file_beats_the_environment_for_system1_too(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The OPPOSITE of nSPAWN, and on purpose: here the VALUE variable is the source
    proven to age (measured 2026-08-28), so the file rules."""
    cfg = tmp_path / "memory.toml"
    cfg.write_text(SYSTEM1)
    monkeypatch.setenv("NOTT_SYSTEM1_ENDPOINT", "https://velho.example/v1")
    monkeypatch.setenv("NOTT_SYSTEM1_MODEL", "system1-velho")
    system1 = resolve_system1_config(cfg)
    assert system1.endpoint == "http://laya.invalid:18031/v1/systemone"
    assert system1.model == "system1-do-arquivo"


def test_the_environment_still_answers_where_the_file_is_silent(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The env sits between the file and the factory — and that is how a test points to a double."""
    cfg = tmp_path / "memory.toml"
    cfg.write_text("")
    monkeypatch.setenv("NOTT_SYSTEM1_ENDPOINT", "https://duplo.example/v1")
    monkeypatch.delenv("NOTT_SYSTEM1_MODEL", raising=False)
    system1 = resolve_system1_config(cfg)
    assert system1.endpoint == "https://duplo.example/v1"
    assert system1.model == MODELO_PADRAO


def test_no_path_resolves_by_the_same_route_as_the_rest_of_the_module(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`path=None` does not invent a path: it is everyone's NOTT_NMEMORY_CONFIG."""
    _sem_env(monkeypatch)
    cfg = tmp_path / "memory.toml"
    cfg.write_text(SYSTEM1)
    monkeypatch.setenv("NOTT_NMEMORY_CONFIG", str(cfg))
    assert resolve_system1_config().model == "system1-do-arquivo"


def test_a_credential_in_the_file_is_refused_without_printing_it(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CREDENTIALS DO NOT LIVE IN THE FILE. The allowlist refuses them, and the refusal names the
    key — never the value, which would end up in a log."""
    segredo = "value-that-must-never-appear-in-the-message"
    cfg = tmp_path / "memory.toml"
    cfg.write_text(f'[system1]\napi_key = "{segredo}"\n')
    with pytest.raises(ConfigError) as excinfo:
        resolve_system1_config(cfg)
    mensagem = str(excinfo.value)
    assert "api_key" in mensagem and "[system1]" in mensagem
    assert segredo not in mensagem


def test_an_unknown_key_inside_system1_is_refused(tmp_path) -> None:
    cfg = tmp_path / "memory.toml"
    cfg.write_text('[system1]\nmodl = "typo"\n')
    with pytest.raises(ConfigError) as excinfo:
        resolve_system1_config(cfg)
    assert "modl" in str(excinfo.value)


def test_a_non_string_model_is_refused(tmp_path) -> None:
    """Wrong type is INVALID configuration: refusal, not a silent fallback to factory."""
    cfg = tmp_path / "memory.toml"
    cfg.write_text("[system1]\nmodel = 5\n")
    with pytest.raises(ConfigError):
        resolve_system1_config(cfg)


def test_the_old_table_name_is_refused_by_name(tmp_path) -> None:
    """A file that still says [jev] is neither read as [system1] nor dropped as unknown."""
    cfg = tmp_path / "memory.toml"
    cfg.write_text('[jev]\nmodel = "x"\n')
    with pytest.raises(ConfigError, match=r"\[jev\] was renamed to \[system1\]"):
        resolve_system1_config(cfg)


def test_the_curator_question_is_opt_in(tmp_path) -> None:
    cfg = tmp_path / "memory.toml"
    cfg.write_text("[system1]\ncurator = true\n")
    assert resolve_system1_config(cfg).curator is True
    cfg.write_text("[system1]\ncurator = false\n")
    assert resolve_system1_config(cfg).curator is False


def test_a_non_boolean_curator_is_refused(tmp_path) -> None:
    cfg = tmp_path / "memory.toml"
    cfg.write_text('[system1]\ncurator = "yes"\n')
    with pytest.raises(ConfigError, match="expected boolean"):
        resolve_system1_config(cfg)


def test_a_non_numeric_timeout_is_refused(tmp_path) -> None:
    cfg = tmp_path / "memory.toml"
    cfg.write_text('[system1]\ntimeout_seconds = "90"\n')
    with pytest.raises(ConfigError):
        resolve_system1_config(cfg)


def test_a_tabela_github_e_lida_do_ficheiro_do_host(tmp_path) -> None:
    """The github-sync fence lives here because this path is ABSOLUTE.

    The repository's (`.nott/nmemory/config.toml`) is relative to the CWD
    and only loads `if exists()`: a process started in the wrong directory
    does not find the config, gains no fence, and silently proposes the
    whole graph.
    """
    cfg = tmp_path / "memory.toml"
    cfg.write_text(
        '[github]\nrepo = "org/r"\nlabel = "l"\nproject = "p"\nefforts = ["'
        + sample_capsule_id(1)
        + '", "'
        + sample_capsule_id(2)
        + '"]\n'
    )
    tbl = github_table(cfg)
    assert tbl["repo"] == "org/r"
    assert tbl["efforts"] == ["cap-1", sample_capsule_id(2)]


def test_uma_chave_desconhecida_em_github_e_recusada(tmp_path) -> None:
    cfg = tmp_path / "memory.toml"
    cfg.write_text('[github]\nrepos = "org/r"\n')
    with pytest.raises(ConfigError) as excinfo:
        github_table(cfg)
    assert "repos" in str(excinfo.value)
    assert "[github]" in str(excinfo.value)


def test_um_ficheiro_sem_github_nao_e_defeito(tmp_path) -> None:
    cfg = tmp_path / "memory.toml"
    cfg.write_text("")
    assert github_table(cfg) == {}


def test_um_repo_nao_string_em_github_e_recusado(tmp_path) -> None:
    cfg = tmp_path / "memory.toml"
    cfg.write_text("[github]\nrepo = 7\n")
    with pytest.raises(ConfigError, match="esperava string"):
        github_table(cfg)


def _rota(monkeypatch: pytest.MonkeyPatch, scope: str, **values: str) -> None:
    for part in ("ENDPOINT", "MODEL", "API_KEY"):
        name = f"NOTT_SYSTEM1_{scope}_{part}"
        if part in values:
            monkeypatch.setenv(name, values[part])
        else:
            monkeypatch.delenv(name, raising=False)


def test_a_question_with_its_own_route_wins_over_the_file(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:

    _sem_env(monkeypatch)
    cfg = tmp_path / "memory.toml"
    cfg.write_text(SYSTEM1)
    _rota(
        monkeypatch,
        "TAINT",
        ENDPOINT="https://taint.invalid/v1",
        MODEL="taint-model",
        API_KEY="x",
    )
    _rota(monkeypatch, "KIND")
    own = resolve_system1_config(cfg, scope="TAINT")
    assert (own.endpoint, own.model, own.auth_env) == (
        "https://taint.invalid/v1",
        "taint-model",
        "NOTT_SYSTEM1_TAINT_API_KEY",
    )
    assert own.timeout_seconds == 90.0
    base = resolve_system1_config(cfg, scope="KIND")
    assert (base.endpoint, base.model, base.auth_env) == (
        "http://laya.invalid:18031/v1/systemone",
        "system1-do-arquivo",
        "NOTT_SYSTEM1_API_KEY",
    )
    assert resolve_system1_config(cfg) == base


def test_without_a_file_route_the_base_environment_decides(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    "The live table carries no endpoint or model: the base env decides."
    monkeypatch.setenv("NOTT_SYSTEM1_ENDPOINT", "https://base.invalid/v1")
    monkeypatch.setenv("NOTT_SYSTEM1_MODEL", "base-model")
    _rota(monkeypatch, "STAGED")
    cfg = tmp_path / "memory.toml"
    cfg.write_text("[system1]\ntimeout_seconds = 30\n")
    got = resolve_system1_config(cfg, scope="STAGED")
    assert (got.endpoint, got.model, got.auth_env) == (
        "https://base.invalid/v1",
        "base-model",
        "NOTT_SYSTEM1_API_KEY",
    )
