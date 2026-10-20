"""Who serves the model to nMEMORY workers — and which account funds the run.

Since 2026-09-18 the gateway is selected via `NOTT_NMEMORY_CURATOR_PROVIDER`
(zai | minimax | kimi, zai by default); the text below describes the original
Z.ai recipe, which is the same format with another prefix.

OWNER LAW (2026-09-15, translated from Portuguese): "I don't want sonnet,
it burns my quota. use zai". The
recipe he provided exports the Anthropic form over the Z.ai variables:

    ANTHROPIC_AUTH_TOKEN=$ZAI_API_KEY ANTHROPIC_BASE_URL=$ZAI_BASE_URL
    ANTHROPIC_MODEL=$ZAI_DEFAULT_MODEL ANTHROPIC_DEFAULT_OPUS_MODEL=$ZAI_DEFAULT_MODEL
    ANTHROPIC_DEFAULT_SONNET_MODEL=$ZAI_DEFAULT_MODEL
    ANTHROPIC_DEFAULT_HAIKU_MODEL=$ZAI_FAST_MODEL ... claude

THIS host's environment carries `ZAI_*` rather than the Anthropic form (measured 2026-09-16:
`ANTHROPIC_BASE_URL` and `ANTHROPIC_AUTH_TOKEN` empty, `ZAI_API_KEY` and
`ZAI_BASE_URL` present) — the recipe is typed on the command line of each
session. A resident worker does not have that command line, which is why the
mapping lives HERE: the client applies the recipe to the child's environment instead of
depending on manual setup by whoever launched the server.

Two consequences that explain why this module exists:

1. `--model` is NOT passed. The recipe maps aliases via env; a
   fixed `claude-sonnet-5` against the Z.ai endpoint requests a model it does not
   serve, and every call returns an error.
2. `CLAUDE_CODE_OAUTH_TOKEN` is STRIPPED from the child's env across ALL accepted
   branches other than explicit opt-in. Alongside it, the run could bill to the
   owner's claude.ai account — exactly what the law forbids, and in the custom key
   branch the endpoint IS Anthropic's, where OAuth would be accepted (revision
   220001fa). An OAuth ALONE does not configure a background worker; only with
   `NOTT_NMEMORY_CURATOR_ALLOW_CLAUDE_QUOTA=1`, which exists so the owner can
   request it when desired, and is the only branch where the token survives.

3. In the Z.ai branch, a custom `ANTHROPIC_API_KEY` is also stripped: the request goes
   to a THIRD-PARTY endpoint, and the owner's paid key must not travel with it
   (revision 220001fa). The `ANTHROPIC_AUTH_TOKEN` that remains is Z.ai's, set
   by this function.

Stripping variables DOES NOT neutralize the login stored in `~/.claude/.credentials.json`:
the child inherits `HOME`, and `--setting-sources ""` covers settings, not credentials. The
fence for this is a private `CLAUDE_CONFIG_DIR`, placed by the caller who has
the disposable directory — see `CuratorClient.judge`.

No credential value is ever read into logs, error messages, or nMEMORY: this
module manipulates variable names, never printing them.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nspawn.owner_roster import Provider

from .config import (
    CONFIG_PATH_ENV,
    ConfigError,
    CuratorRoute,
    config_path,
    curator_route,
)

#: Explicit owner opt-in to spend claude.ai quota on a background worker.
ALLOW_CLAUDE_QUOTA = "NOTT_NMEMORY_CURATOR_ALLOW_CLAUDE_QUOTA"

#: Values recognized as YES for a boolean switch. Owner law (2026-09-18): standard
#: is `1`/`0`; "on" is non-standard and is no longer accepted.
SIM = frozenset({"1", "true", "yes", "sim"})


def ligado(
    nome: str, env: Mapping[str, str] | None = None, *, default: str = "1"
) -> bool:
    """An environment switch: when absent defaults to `default` (`1` = on, except
    those existing for explicit opt-in, which pass `default="0"`)."""
    amb = os.environ if env is None else env
    return amb.get(nome, default).strip().lower() in SIM


#: Custom key + endpoint, in Anthropic form, exported manually.
_ANTHROPIC_KEYS = ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")

#: Which Anthropic-compatible gateway serves the worker. Owner decision
#: (2026-09-18): a prefix selector, because `nspawn-gateway.env` writes all
#: three with the same shape `<PREFIX>_BASE_URL/_API_KEY/_DEFAULT_MODEL/_FAST_MODEL`.
#: Absent defaults to `zai` (the law of 2026-09-15); switching is a single line in `.env`.
PROVIDER = "NOTT_NMEMORY_CURATOR_PROVIDER"
PROVIDERS: Mapping[str, str] = {"zai": "ZAI", "minimax": "MINIMAX", "kimi": "KIMI"}


def provider_name(env: Mapping[str, str] | None = None) -> str:
    """The chosen name, validated against the closed set; a name outside it is
    a typed error with the vocabulary, never a silent worker."""
    amb = os.environ if env is None else env
    configured = _configured_route(amb)
    if configured is not None:
        return configured.provider
    nome = amb.get(PROVIDER, "zai").strip().lower() or "zai"
    if nome not in PROVIDERS:
        raise ValueError(f"{PROVIDER}={nome!r} is not one of {sorted(PROVIDERS)}")
    return nome


def _configured_route(env: Mapping[str, str]) -> CuratorRoute | None:
    path = env.get(CONFIG_PATH_ENV)
    return curator_route(Path(path) if path else config_path())


def selected_curator_route(env: Mapping[str, str] | None = None) -> CuratorRoute:
    """The file owns routing; compatibility env is used only without its table."""
    ambient = os.environ if env is None else env
    configured = _configured_route(ambient)
    return (
        configured if configured is not None else CuratorRoute(provider_name(ambient))
    )


def _native_provider(route: CuratorRoute) -> Provider:
    try:
        from nspawn.errors import NSpawnError
        from nspawn.owner_roster import Roster
    except ModuleNotFoundError:
        raise ConfigError("native curator requires the NOTT nSPAWN package") from None
    try:
        path = Roster.default_path()
        if path is None:
            raise ConfigError("curator codex account requires the owning nSPAWN roster")
        roster = Roster.read(path)
        provider = roster.select(route.account)
    except NSpawnError:
        raise ConfigError("curator codex account roster is invalid") from None
    if (
        provider is None
        or provider.kind != "codex"
        or route.account not in provider.accounts
    ):
        raise ConfigError("curator codex account is not declared on a codex provider")
    return provider


def validate_curator_route(route: CuratorRoute) -> None:
    """Selection proves a declared alias; it never creates a home or probes a client."""
    if route.provider == "codex":
        _native_provider(route)


def _native_available(route: CuratorRoute) -> bool:
    try:
        from nspawn.errors import NSpawnError
    except ModuleNotFoundError:
        return False
    try:
        provider = _native_provider(route)
        account = provider.resolve_native_account(route.account)
        if account.home is None or provider.bin is None:
            return False
        home = account.home.resolve(strict=True)
        directory = home.stat()
        credential = (home / "auth.json").lstat()
        binary = provider.bin.resolve(strict=True)
        executable = binary.stat()
        return (
            stat.S_ISDIR(directory.st_mode)
            and directory.st_uid == os.geteuid()
            and not directory.st_mode & 0o022
            and stat.S_ISREG(credential.st_mode)
            and credential.st_uid == os.geteuid()
            and stat.S_IMODE(credential.st_mode) == 0o600
            and credential.st_nlink == 1
            and stat.S_ISREG(executable.st_mode)
            and not executable.st_mode & 0o022
            and os.access(binary, os.X_OK)
        )
    except ConfigError, NSpawnError, OSError, RuntimeError:
        return False


def _gateway(env: dict[str, str]) -> str | None:
    """The selected gateway prefix when key AND endpoint are present.
    A key without an endpoint is not a provider; an invalid name is not either."""
    try:
        pre = PROVIDERS[provider_name(env)]
    except ValueError, ConfigError, KeyError:
        return None
    if env.get(f"{pre}_API_KEY") and env.get(f"{pre}_BASE_URL"):
        return pre
    return None


def _own_endpoint(env: dict[str, str]) -> bool:
    return bool(env.get("ANTHROPIC_BASE_URL")) and any(
        env.get(key) for key in _ANTHROPIC_KEYS
    )


def has_provider(env: dict[str, str] | None = None) -> bool:
    """There is an account other than the owner's quota, or explicit opt-in to use it."""
    e = dict(os.environ) if env is None else env
    try:
        route = selected_curator_route(e)
    except ValueError, ConfigError:
        return False  # a misspelt selector is no provider, never a fallback account
    if route.provider == "codex":
        return _native_available(route)
    if _gateway(e) or _own_endpoint(e):
        return True
    if any(e.get(key) for key in _ANTHROPIC_KEYS):
        return True  # custom key without endpoint: this is paid API, not quota
    return bool(e.get("CLAUDE_CODE_OAUTH_TOKEN")) and ligado(
        ALLOW_CLAUDE_QUOTA, e, default="0"
    )


def provider_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """The owner's recipe applied: parent env + Anthropic form of the gateway
    selected via `NOTT_NMEMORY_CURATOR_PROVIDER` (zai by default).

    Manual Anthropic exports OVERRIDE — this mapping does not overwrite them.
    Without an own provider, the env exits as it entered (and `has_provider`
    already indicated no run should occur).
    """
    out = dict(os.environ) if env is None else dict(env)
    configured = _configured_route(out)
    if configured is not None and configured.provider == "codex":
        # The existing native account supplies auth and requested model/effort.
        # Never project gateway credentials or another provider's profile into it.
        for name in (
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_BASE_URL",
            "ANTHROPIC_MODEL",
            "ANTHROPIC_DEFAULT_OPUS_MODEL",
            "ANTHROPIC_DEFAULT_SONNET_MODEL",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL",
            "CLAUDE_CODE_SUBAGENT_MODEL",
            "CLAUDE_CODE_EFFORT_LEVEL",
            "CLAUDE_CODE_OAUTH_TOKEN",
        ):
            out.pop(name, None)
        return out
    pre = _gateway(out)
    opt_in_only = not (pre or _own_endpoint(out)) and not any(
        out.get(key) for key in _ANTHROPIC_KEYS
    )
    if not opt_in_only:
        # The owner's account must not fund a worker run: §2 in header.
        out.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    if _own_endpoint(out) or pre is None:
        return out
    out["ANTHROPIC_BASE_URL"] = out[f"{pre}_BASE_URL"]
    out["ANTHROPIC_AUTH_TOKEN"] = out[f"{pre}_API_KEY"]
    # §3: the paid key does not travel to a third party.
    out.pop("ANTHROPIC_API_KEY", None)
    default = out.get(f"{pre}_DEFAULT_MODEL")
    fast = out.get(f"{pre}_FAST_MODEL", default)
    if default:
        out["ANTHROPIC_MODEL"] = default
        out["ANTHROPIC_DEFAULT_OPUS_MODEL"] = default
        out["ANTHROPIC_DEFAULT_SONNET_MODEL"] = default
        out["CLAUDE_CODE_SUBAGENT_MODEL"] = default
    if fast:
        out["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = fast
    return out
