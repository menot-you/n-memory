"""The owner's provider roster, read from ``~/.nott/spawn.toml``.

Port of ``capabilities/nspawn/src/owner_roster.rs`` at pin ``d8e0be5ff``.

One file, extended, never a second one: the roster lives beside the Slack
door's own tables, and unknown tables and keys are IGNORED rather than
refused so the door's configuration is never a parse error here. Secrets are
NAMED, never carried — ``token_file`` is a path this module stats, and
``auth_env`` is the NAME of a variable expanded at spawn. Neither a token nor
an API key is ever read into these structs.

A malformed file never takes the organ down: every failure is a typed
``RosterError`` value naming the file, surfacing at the spawn that needed it.
"""

from __future__ import annotations

import contextlib
import copy
import fcntl
import json
import os
import re
import stat
import sys
import time
import tomllib
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from secrets import token_hex
from typing import Any, Final, Literal

from . import claude_catalog, config
from .errors import NERR_1104, NERR_1105, NERR_1124, NSpawnError

KINDS = ("claude", "codex", "agy")

#: The family aliases Claude Code resolves to its current model of that family.
CLAUDE_MODEL_ALIASES = frozenset({"opus", "sonnet", "haiku"})


def claude_family(model: str) -> str | None:
    """The family a Claude alias names, with or without Claude Code's `[1m]` suffix."""
    family = model.removesuffix("[1m]")
    return family if family in CLAUDE_MODEL_ALIASES else None


def model_env_build(
    model: str, fast: str | None, binary: str | Path | None, env: dict[str, str]
) -> None:
    """Clamp higher tiers. An unclassified explicit ID keeps the full clamp."""
    alias = claude_family(model)
    family = alias
    if family is None and not model.startswith("claude-"):
        # Compatible endpoints keep their opaque model and fast-model values.
        env["ANTHROPIC_MODEL"] = model
        env["CLAUDE_CODE_SUBAGENT_MODEL"] = model
        for tier, value in (
            ("OPUS", model),
            ("SONNET", model),
            ("HAIKU", fast or model),
        ):
            name = f"ANTHROPIC_DEFAULT_{tier}_MODEL"
            if claude_family(value) is None:
                env[name] = value
            else:
                env.pop(name, None)
        return
    models: dict[str, str] = {}
    if family is not None:
        models = claude_catalog.catalog_read(binary)
    elif model.startswith("claude-"):
        try:
            models = claude_catalog.catalog_read(binary)
        except claude_catalog.ModelCatalogError:
            pass  # Explicit IDs remain usable with a conservative full clamp.
        base = model.removesuffix("[1m]")
        family = next(
            (
                tier
                for tier in ("haiku", "sonnet", "opus", "fable")
                if models.get(tier) == base
            ),
            None,
        )

    def resolve(tier: str) -> str:
        resolved = models.get(tier)
        if (
            resolved is None
            or re.fullmatch(rf"claude-{tier}-[a-zA-Z0-9.-]+", resolved) is None
        ):
            raise claude_catalog.ModelCatalogError(
                "Claude client catalogue cannot resolve the requested family"
            )
        return resolved

    target = model
    if alias is not None:
        target = resolve(alias) + ("[1m]" if model.endswith("[1m]") else "")
    elif family is not None:
        resolve(family)
    families = ("haiku", "sonnet", "opus", "fable")
    tiers = {
        tier: target
        if family is None or families.index(tier) >= families.index(family)
        else resolve(tier)
        for tier in families
    }
    env.pop("CLAUDE_CODE_SUBAGENT_MODEL", None)
    env.pop("ANTHROPIC_SMALL_FAST_MODEL", None)
    if family is None or family == "haiku":
        env["CLAUDE_CODE_SUBAGENT_MODEL"] = target
    env.update(
        {
            "ANTHROPIC_MODEL": target,
            **{
                f"ANTHROPIC_DEFAULT_{tier.upper()}_MODEL": value
                for tier, value in tiers.items()
            },
        }
    )


#: What nSPAWN has IMPLEMENTED for each kind, never what a roster claims: `custody`
#: is a native launch under process custody and its sandbox, `write` a native write
#: run, `resume` a recorded session continued, `review` a read-only review. A
#: capability enters here with the code and proof that serve it. `write` is a caged
#: native writer in its own slice (codex: its sandbox profile; claude: the client's
#: Bash sandbox, `claude_restricted`), so it carries its own custody; `custody`
#: names a caged READ-ONLY run. `review` without
#: `custody` is read-only by POLICY only (the child is told, not caged): never read
#: it as a sandbox promise. A claude route that reaches its model through another
#: endpoint cannot be caged, and `provider_report` lists it without `custody` or
#: `write`.
CAPABILITIES: dict[str, tuple[str, ...]] = {
    "claude": ("custody", "resume", "review", "write"),
    "codex": ("custody", "resume", "review", "write"),
    "agy": ("resume", "review"),
}

#: The workflow roles `[roles]` may bind to a declared provider.
ROLES = ("writer", "reviewer")

#: The roles `[role_profile]` may give a model and effort, each checked against
#: the class its runs take: the writer works as `judgment`, the reviewer as
#: `review`. Parsing a writer profile does not deliver it: a workflow writer
#: still carries the provider line's model and effort until the harness
#: forwards the profile.
PROFILED_ROLES = ("writer", "reviewer")

#: The class whose ceiling bounds each profiled role's effort.
PROFILE_CLASSES = {"writer": "judgment", "reviewer": "review"}

#: The classes `[preference]` may order: the two whose run witnesses work that
#: another agent made, so they never run as that agent.
PREFERENCE_CLASSES = ("review", "verification")

#: The name Claude Code reads to know which endpoint it speaks to. Same
#: spelling as `child.ANTHROPIC_BASE_URL_KEY`; not imported from there because
#: `child` imports this module and the cycle is not worth it.
ANTHROPIC_BASE_URL_KEY = "ANTHROPIC_BASE_URL"
DIRECT_KINDS = ("codex", "agy")

#: Spellings this parser once served, keyed by where they were written, each
#: naming what replaces it. REFUSED BY NAME rather than ignored: the file's
#: rule is that an unknown key is the door's business, and under that rule a
#: retired key reads as accepted and silently changes nothing — the owner's
#: `claude_bin` would keep pointing at a binary nobody spawns. This is a
#: NAMED list, never deny-unknown-fields: everything outside it stays ignored.
#: The second spelling of the auto-compact window. The client reads both and they
#: drifted in silence, so the roster writes ONE and nSPAWN derives the
#: other in `build_child_env`.
DERIVED_ENV_KEYS: dict[str, str] = {
    "CLAUDE_AUTO_COMPACT_WINDOW": "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
}

RETIRED_KEYS: dict[str, str] = {
    "defaults.claude_bin": "[bins].claude",
    "provider.bin": "[bins].<kind>",
    "provider.token_file": "secrets",
    "provider.speed_tiers": "efforts",
    "defaults.plugin_root": "defaults.plugins",
}


class RosterError(NSpawnError):
    """Why a roster could not be used. The file is always named."""

    def __init__(self, message: str) -> None:
        self.message = message
        Exception.__init__(self, message)

    @staticmethod
    def unreadable(path: Path, source: OSError) -> RosterError:
        return RosterError(f"the provider roster at {path} could not be read: {source}")

    @staticmethod
    def unwritable(path: Path, source: OSError) -> RosterError:
        return RosterError(
            f"the provider roster at {path} could not be written: {source}"
        )

    @staticmethod
    def malformed(path: Path, message: str) -> RosterError:
        return RosterError(f"the provider roster at {path} is malformed: {message}")

    @staticmethod
    def duplicate_name(path: Path, name: str) -> RosterError:
        return RosterError(
            f"the provider roster at {path} declares {name} twice; "
            "a selector cannot resolve it"
        )

    @staticmethod
    def ambiguous_alias(path: Path, alias: str, owners: list[str]) -> RosterError:
        return RosterError(
            f"the provider roster at {path} discovers the account alias {alias!r} "
            f"under both {' and '.join(owners)}; a bare alias must resolve to one provider"
        )

    @staticmethod
    def system_prompt_not_once(path: Path, who: str, reason: str) -> RosterError:
        return RosterError(
            f"the provider roster at {path} gives {who} {reason}; declare the prompt "
            "once (defaults.system_prompt or args, not both), the flag and its path "
            "as separate items"
        )

    @staticmethod
    def ambiguous_default(path: Path, kind: str, count: int) -> RosterError:
        return RosterError(
            f"the provider roster at {path} marks {count} {kind} providers default; "
            "exactly one may be"
        )


@dataclass(frozen=True)
class NativeAccount:
    """A native CLI home and optional credential name; never credential bytes.

    ONE type carries both shapes rather than a sibling class per kind: the
    picker resolves every account through ``resolve_native_account``, and a
    second type would fork that return value at every caller to ask which it
    got. Which FIELDS an account may carry is decided where ``kind`` is known,
    in ``_native_accounts``.
    """

    #: Claude uses this directory as CLAUDE_CONFIG_DIR, without changing HOME.
    #: None retains the inherited Claude configuration. Codex and AGY require
    #: their CODEX_HOME or HOME directory here.
    home: Path | None
    sqlite_home: Path | None = None
    auth_env: str | None = None
    model_provider: str | None = None
    #: The endpoint for THIS account. A DIRECT kind does not declare it: its native
    #: executable resolves its own destination from the vendor's home.
    base_url: str | None = None
    #: Replaces the provider's `model` ONLY for this account. The same logical
    #: model has different IDs in vLLM (`qwen3.8-27b`) and ollama
    #: (`qwen3.8:27b`), and a provider can only declare one of them.
    model: str | None = None
    #: How many concurrent runs this account is worth, for the picker to spread
    #: load. Never zero: an account that nobody can run is absent from the
    #: roster, not declared with zero capacity.
    capacity: int = 1
    #: argv that brings up a dormant route. TUPLE because this dataclass is
    #: frozen; argv and not string because there is no shell interpreting
    #: this, hence no expansion, quotes, or chaining to inherit.
    bootstrap: tuple[str, ...] = ()
    #: Environment for THIS machine, applied AFTER `[provider.env]` because it is
    #: more specific than it. The context window is the case that required it:
    #: the three qwen machines serve 245760, 65536 and 32768 tokens, and a
    #: provider can only declare one number. Passing 200000 to m1 pushed the
    #: user message outside the window and the client returned
    #: `500 no user query found in messages`.
    env: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name, value in (("home", self.home), ("sqlite_home", self.sqlite_home)):
            if value is None:
                continue
            if (
                not isinstance(value, Path)
                or "$" in str(value)
                or not (value.is_absolute() or str(value).startswith("~/"))
            ):
                raise ValueError(
                    f"{name} must be an absolute or ~/ path without placeholders"
                )
        if (
            self.auth_env is not None
            and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.auth_env) is None
        ):
            raise ValueError("auth_env must be an environment variable name")
        if self.model_provider is not None and not self.model_provider.strip():
            raise ValueError("model_provider must be nonempty")
        if (
            self.base_url is not None
            and re.fullmatch(r"https?://\S+", self.base_url) is None
        ):
            raise ValueError("base_url must be an http:// or https:// endpoint")
        if self.model is not None and not self.model.strip():
            raise ValueError("model must be nonempty")
        if isinstance(self.capacity, bool) or self.capacity < 1:
            raise ValueError("capacity must be a positive integer")
        for argument in self.bootstrap:
            if not argument.strip() or any(ch in argument for ch in "\0\n\r"):
                raise ValueError(
                    "bootstrap arguments must each be a nonempty single line"
                )


def claude_config_dir(
    account: NativeAccount | None = None, ambient: Mapping[str, str] | None = None
) -> Path:
    """Return the declared Claude directory or the inherited configuration directory."""
    environment = os.environ if ambient is None else ambient
    if account is not None and account.home is not None:
        directory = str(account.home)
        if directory.startswith("~/"):
            directory = str(
                Path(environment.get("HOME", str(Path.home()))) / directory[2:]
            )
    else:
        directory = environment.get(
            "CLAUDE_CONFIG_DIR",
            str(Path(environment.get("HOME", str(Path.home()))) / ".claude"),
        )
    return Path(directory)


@dataclass
class Provider:
    """One provider the owner declared. Every field except the name is optional."""

    name: str
    kind: str = "claude"
    secrets: Path | None = None
    # ponytail: `token_file` and `speed_tiers` are no longer SPELLED in a
    # roster (see RETIRED_KEYS) but other modules still read the fields. They
    # can go once no roster in the wild carries either key.
    token_file: Path | None = None
    auth_env: str | None = None
    oauth_token_file: Path | None = None
    bin: Path | None = None
    env: dict[str, str] = field(default_factory=dict)
    args: list[str] = field(default_factory=list)
    model: str | None = None
    #: The fast lane, optional. Absent falls back to `model` — the catalog
    #: does not say which of its ids is the quick one, so a provider that
    #: discovers its models declares nothing here and haiku takes `model`
    #: (an alias `model` leaves haiku to Claude Code's current one, child.py).
    model_fast: str | None = None
    models: list[str] = field(default_factory=list)
    effort: str | None = None
    efforts: list[str] = field(default_factory=list)
    speed_tiers: list[str] = field(default_factory=list)
    default: bool = False
    #: Routing natures the OWNER declared, never derived from a model name.
    #: ``None`` is "undeclared", not a zero and not a one.
    strength: int | None = None
    thrift: int | None = None
    design: int | None = None
    accounts: dict[str, NativeAccount] = field(default_factory=dict)
    excluded_accounts: list[str] = field(default_factory=list)
    #: The roster spells `accounts`, even empty: an explicit list, never a
    #: secrets directory's discovery, says which accounts there are and in
    #: which order.
    declared: bool = False
    #: The vendor's own error codes that mean the provider's balance or
    #: package is spent, as the CLI renders them in `[<code>]`. Declared, never
    #: guessed: a 429 carrying one of them walls the provider for the hour.
    exhausted_codes: frozenset[str] = frozenset()
    model_catalog: dict[str, str] | claude_catalog.ModelCatalogError | None = None
    #: Only a caller that names this provider (or one of its accounts) gets
    #: it. An unnamed dispatch never lands here, walled fallback included.
    named_only: bool = False
    #: Fill the first account in declared order before the next one opens.
    fill_first: bool = False
    #: The native balance reader `quota()` asks, by name. Only "colab".
    quota_source: str | None = None

    def adds_by_login(self) -> bool:
        """Whether the owner can log a new account in himself and register it: a
        native kind on its own new home, or a token kept in the secrets directory.
        An endpoint account (`base_url`) has no login."""
        return self.kind in DIRECT_KINDS or (
            self.secrets is not None
            and not any(a.base_url for a in self.accounts.values())
        )

    def secrets_dir(self) -> Path | None:
        if self.secrets is None:
            return None
        return expand_home(self.secrets)

    def discovered_accounts(self) -> list[str]:
        """Sorted native aliases, or legacy accounts in the secrets directory.

        DECLARED, NEVER DISCOVERED is the rule for ``models``/``efforts``;
        this directory IS the account store, so its names are facts.
        """
        if self.accounts or self.declared:
            return sorted(self.accounts)
        directory = self.secrets_dir()
        if directory is None:
            return []
        try:
            entries = list(os.scandir(directory))
        except OSError:
            return []
        accounts = [
            Path(entry.name).stem
            for entry in entries
            if entry.is_file() and Path(entry.name).suffix == ".token"
        ]
        return sorted(accounts)

    def serves(self, model: str, *, declared: Sequence[str] | None = None) -> bool:
        """Check the declared model list. An absent list permits any model.

        A full Claude ID remains valid on its declared family route after the
        roster changes to family names.
        """
        names = self.models if declared is None else declared
        if not names or model in names:
            return True
        families = {
            family for name in names if (family := claude_family(name)) is not None
        }
        if self.kind != "claude" or not families or claude_family(model) is not None:
            return model in names
        base = model.removesuffix("[1m]")
        if base not in self.catalog.values():
            return False
        found = re.fullmatch(r"claude-(opus|sonnet|haiku)-[a-zA-Z0-9.-]+", base)
        if found is None:
            return model in names
        if found[1] not in families:
            return False
        return (
            not model.endswith("[1m]") or model in names or f"{found[1]}[1m]" in names
        )

    @property
    def catalog(self) -> dict[str, str]:
        if isinstance(self.model_catalog, claude_catalog.ModelCatalogError):
            raise self.model_catalog
        if self.model_catalog is not None:
            return self.model_catalog
        return claude_catalog.catalog_read(self.bin)

    def ordered_accounts(self) -> list[str]:
        """The accounts in the order the picker breaks ties on; the first is the default.

        DECLARED order when the roster declares accounts, because that is the order
        the owner wrote; a secrets directory's names sorted, since a filename has no
        order of its own.
        """
        if self.accounts or self.declared:
            return list(self.accounts)
        return self.discovered_accounts()

    def resolve_native_account(self, account_name: str | None) -> NativeAccount:
        """Resolve an explicit alias, expanding its paths only at use time."""
        alias = account_name
        if alias is None or alias == self.name:
            # DECLARED, not ordered: the same order as the picker and
            # the child's `_selected_account`.
            alias = next(iter(self.accounts), None)
        if alias is None or alias not in self.accounts:
            raise ValueError(
                f'account "{account_name}" is unavailable in provider "{self.name}" accounts'
            )
        account = self.accounts[alias]
        # `replace` rather than a field-by-field rebuild: the rebuild silently
        # dropped every field added after it was written, and two were.
        return replace(
            account,
            home=None if account.home is None else expand_home(account.home),
            sqlite_home=(
                expand_home(account.sqlite_home)
                if account.sqlite_home is not None
                else None
            ),
        )

    def resolve_token_file(self, account_name: str | None) -> Path:
        """Resolve the token file for a requested account name.

        Raises:
            ValueError: naming the account and directory that failed.
        """
        if account_name is not None and (
            Path(account_name).name != account_name
            or account_name in (".", "..")
            or "\\" in account_name
        ):
            raise ValueError("token account alias must be a single filename")
        directory = self.secrets_dir()
        if directory is not None:
            if account_name in self.accounts:
                # Explicit native accounts never borrow a legacy default/raw file.
                return directory / f"{account_name}.token"
            if account_name is not None:
                token_path = directory / f"{account_name}.token"
                if token_path.is_file():
                    return token_path
                raw_path = directory / account_name
                if raw_path.is_file():
                    return raw_path
                if account_name in (self.name, "claude"):
                    accounts = self.discovered_accounts()
                    if accounts:
                        return directory / f"{accounts[0]}.token"
                raise ValueError(
                    f'account "{account_name}" not found in secrets directory {directory}'
                )
            accounts = self.discovered_accounts()
            if accounts:
                return directory / f"{accounts[0]}.token"
            raise ValueError(f"no .token files found in secrets directory {directory}")
        explicit = (
            self.oauth_token_file
            if self.oauth_token_file is not None
            else self.token_file
        )
        if explicit is not None:
            return expand_home(explicit)
        # THE VOCABULARY MUST BE WRITABLE: `token_file` is in `RETIRED`,
        # so an owner following this message gets "is retired, use secrets".
        # And a claude-kind account names its own `auth_env`, which this
        # refusal did not say.
        raise ValueError(
            f'account "{self.name}" declares no credential source: give the '
            "provider a secrets directory, or give the account an auth_env"
        )


@dataclass
class Defaults:
    """What a caller gets when it names no provider and the roster names no default."""

    model: str | None = None
    effort: str | None = None
    args: list[str] = field(default_factory=list)
    #: N plugin directories, each becoming its own `--plugin-dir`. The owner
    #: used to spell this by hand in `args`; a list cannot be half-typed.
    plugins: list[str] = field(default_factory=list)
    #: Where the child expands `$PLUGIN` to, and where it looks for the
    #: `.mcp.json` it hands the executor. DERIVED from `plugins[0]`, never
    #: written by the owner: two keys for one directory drifted apart in the
    #: live roster, the root naming a client subdirectory while `plugins`
    #: named the bundle above it.
    #: The file every recipe appends. Named once here instead of repeated as an
    #: `args` pair that a rewrite can drop — and one did, three times.
    system_prompt: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    plugin_root: Path | None = None


@dataclass(frozen=True)
class System1:
    """The typed evaluation route, declared where the owner reads it.

    THE CREDENTIAL DOES NOT LIVE HERE. The roster is a readable and shareable
    configuration file; the key comes from `NOTT_SYSTEM1_API_KEY` in the process environment.
    A key written here leaks on the first `cat spawn.toml`, which is why the parser
    refuses the table by key NAME instead of silently ignoring it.
    """

    endpoint: str | None = None
    model: str | None = None
    timeout_seconds: float | None = None


#: Names that may never appear in the `[system1]` table. Refusing by name catches the
#: intent even when the value is a placeholder — and a placeholder today is a
#: credential in the next edit.
SYSTEM1_CREDENTIAL_KEYS = frozenset(
    {"key", "api_key", "apikey", "token", "authorization", "secret"}
)


#: The one effort ladder class policies are compared on. Providers declare
#: subsets of it in their own order; this is the order.
EFFORT_LADDER: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max", "ultra")

#: The `[classes]` key that governs a dispatch naming no class at all.
UNCLASSIFIED = "unclassified"


@dataclass(frozen=True)
class ClassPolicy:
    """How much reasoning one kind of work buys: where it starts, where it stops.

    Measured 2026-09-19: nine mechanical dispatches ran in `ultra` by the
    agent's choice. The effort is the owner's cost, so policy lives in the roster.
    """

    effort: str
    #: Highest effort a caller may pin for this class; absent means `effort`.
    ceiling: str
    #: Highest provider `strength` this class may reach. None = no cap, and
    #: zero is a real cap: only providers with no strength at all clear it.
    max_strength: int | None = None
    #: Run the class on each route's `model_fast` when the caller pins no model:
    #: the owner's method sends experiments, probes and sweeps to the fast model.
    fast: bool = False

    def __post_init__(self) -> None:
        if self.max_strength is not None and not 0 <= self.max_strength <= 255:
            raise ValueError(f"max_strength {self.max_strength} is not a u8")
        for label in (self.effort, self.ceiling):
            if label not in EFFORT_LADDER:
                raise ValueError(
                    f'effort "{label}" is not on the ladder [{", ".join(EFFORT_LADDER)}]'
                )
        if EFFORT_LADDER.index(self.ceiling) < EFFORT_LADDER.index(self.effort):
            raise ValueError(
                f'ceiling "{self.ceiling}" is below the initial effort "{self.effort}"'
            )


@dataclass
class Roster:
    """The file, parsed. Unknown tables and keys are ignored, never refused."""

    defaults: Defaults = field(default_factory=Defaults)
    providers: list[Provider] = field(default_factory=list)
    #: The per-kind executables, keyed by `KINDS`. DECLARED, NEVER GUESSED: a
    #: roster that resolved `claude` off PATH would silently pick whichever
    #: build the shell happened to expose. One table instead of a `bin` per
    #: provider, because four providers shared two binaries between them.
    bins: dict[str, Path] = field(default_factory=dict)
    system1: System1 = field(default_factory=System1)
    #: Per-class effort policy, keyed by execution class; empty keeps the
    #: pre-policy ladder (caller > provider > defaults).
    classes: dict[str, ClassPolicy] = field(default_factory=dict)
    #: `[native_write] roots`: the only directories under which a native write
    #: run may take its `cwd`. None declared refuses every native write.
    native_write_roots: tuple[Path, ...] = ()
    #: `[roles]`: which declared provider serves each workflow role. Only what the
    #: owner wrote; the workflow owns the default for a role left unbound.
    roles: dict[str, str] = field(default_factory=dict)
    #: `[role_profile.<role>]`: the model and effort a role's requests carry on
    #: the route `[roles]` binds. Only what the owner wrote; absent sends none.
    role_profiles: dict[str, dict[str, str]] = field(default_factory=dict)
    #: `[preference]`: per witness class, the routes (a provider or an account,
    #: with an optional model) an unpinned run tries first, in the owner's order.
    preferences: dict[str, list[tuple[str, str | None]]] = field(default_factory=dict)
    #: Where this roster was read from, so a refusal raised while RESOLVING can
    #: name the file to fix rather than only the malformed value.
    path: Path | None = None

    @staticmethod
    def read(path: Path) -> Roster:
        """Read the roster at ``path``.

        An ABSENT file is an empty roster, never an error: a host that
        declared nothing keeps the behaviour it had before this module.

        Raises:
            RosterError: when the file exists but cannot be read, does not
                parse, repeats a name, or marks two defaults of one kind.
        """
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return Roster()
        except OSError as source:
            raise RosterError.unreadable(path, source) from source
        except UnicodeDecodeError as error:
            raise RosterError.malformed(path, "the file is not UTF-8") from error
        roster = _parse_roster(path, text)
        config._store_table_strict(path)
        roster._validate(path)
        roster.path = path
        return roster

    @staticmethod
    def default_path(cwd: Path | None = None) -> Path | None:
        """The single config wins; NOTT_NSPAWN_ROSTER is compatibility only.

        cwd never selects a file. An absent config permits the legacy env path.
        """
        declared = config.resolve_roster_path()
        if declared is not None:
            return declared
        named = (os.environ.get("NOTT_NSPAWN_ROSTER") or "").strip()
        if named.startswith("${") and named.endswith("}"):
            # Measured 2026-08-30: a launcher that does not expand `${VAR}` passes the
            # TEMPLATE forward, and treating it as a filename blames the
            # operator's path for the launcher's failure.
            raise NSpawnError(
                NERR_1104,
                f"NOTT_NSPAWN_ROSTER carries the template {named!r}: the reference "
                "was not expanded by whoever launched this process. Declare the "
                f"single file at {config.store_config_path()}, "
                "which does not depend on the launcher's environment.",
            )
        return Path(named) if named else None

    def _validate(self, path: Path) -> None:
        """Both invariants a selector depends on, checked at read time."""
        seen: list[str] = []
        for provider in self.providers:
            if provider.name in seen:
                raise RosterError.duplicate_name(path, provider.name)
            seen.append(provider.name)
        homes: dict[Path, str] = {}
        for provider in self.providers:
            if provider.kind != "claude":
                continue
            for alias, account in provider.accounts.items():
                if account.home is None:
                    continue
                home = claude_config_dir(account).resolve()
                if home in homes:
                    raise RosterError.malformed(
                        path,
                        f"duplicate Claude config directory for accounts {homes[home]} and {alias}",
                    )
                homes[home] = alias
        count = sum(1 for p in self.providers if p.default)
        if count > 1:
            raise RosterError.ambiguous_default(path, "claude", count)
        explicit_aliases = {alias for p in self.providers for alias in p.accounts}
        for alias in sorted(explicit_aliases):
            if alias in seen:
                raise RosterError.malformed(
                    path, f"account alias {alias!r} collides with a provider name"
                )
            owners = [
                p.name for p in self.providers if alias in p.discovered_accounts()
            ]
            if len(owners) > 1:
                raise RosterError.ambiguous_alias(path, alias, owners)
        # AN ENDPOINT OVER PER-ACCOUNT ENDPOINTS is a contradiction, and the
        # winning one is the top one: `build_child_env` applies the account and ONLY THEN
        # the env blocks (child.py:294). A provider with accounts that provide
        # `base_url` gets all of them pinned to the same address and
        # load spreading has nowhere to go. `[defaults.env]` counts
        # just as much as `[provider.env]`, because `resolve` merges the two and it is
        # the merge that the child reads.
        for provider in self.providers:
            com_endpoint = [
                alias
                for alias, account in provider.accounts.items()
                if account.base_url
            ]
            if not com_endpoint:
                continue
            for bloco, env in (
                (f"[provider.env] of {provider.name!r}", provider.env),
                ("[defaults.env]", self.defaults.env),
            ):
                if ANTHROPIC_BASE_URL_KEY in env:
                    raise RosterError.malformed(
                        path,
                        f"{bloco} sets {ANTHROPIC_BASE_URL_KEY}, which wins over "
                        f"the endpoint declared by account(s) "
                        f"{', '.join(sorted(com_endpoint))}: declare the "
                        "endpoint on the accounts or on the provider, never on "
                        "both",
                    )

    def select(self, name: str | None) -> Provider | None:
        """The provider a caller named, or the roster's default.

        Selection is by NAME, then by a discovered account alias; `kind` decides
        which binary is spawned (owner order 2026-09-05), never which provider
        is selected. An alias discovered under two providers is refused here,
        after discovery — never resolved to the first.

        A roster with exactly ONE provider serves it without a `default` marker:
        requiring the marker there would be ceremony over a set with no
        ambiguity in it.
        """
        if name is not None:
            for provider in self.providers:
                if provider.name == name:
                    return provider
            owners = [p for p in self.providers if name in p.discovered_accounts()]
            if len(owners) > 1:
                raise RosterError.ambiguous_alias(
                    self.path or Path("<roster>"), name, [p.name for p in owners]
                )
            return owners[0] if owners else None
        for provider in self.providers:
            if provider.default:
                return provider
        return self.providers[0] if self.providers else None

    def env_of(self, provider: Provider | None) -> dict[str, str]:
        """`[defaults.env]` under `[provider.env]`, key by key: the env a spawn
        of `provider` gets. Parsed since the field existed and once never
        merged, so a default the owner declared once reached nobody."""
        merged = dict(self.defaults.env)
        if provider is not None:
            merged.update(provider.env)
        return merged

    def resolve(self, name: str | None) -> Resolved:
        """Resolve what a spawn gets — a pure function of file+name."""
        provider = self.select(name)
        if provider is None and name is not None:
            provider = self.select(None)
        plugin_root = (
            expand_home(self.defaults.plugin_root)
            if self.defaults.plugin_root is not None
            else None
        )
        # `plugins` and `system_prompt` are the typed spelling of what the
        # owner used to write into `args` by hand; they compose INTO it, so a
        # roster using either form produces the same command line.
        args: list[str] = []
        for plugin in self.defaults.plugins:
            args.extend(plugin_flag(plugin, self.path or Path("<roster>")))
        if self.defaults.system_prompt is not None:
            args.extend(
                (
                    "--append-system-prompt-file",
                    str(expand_home(Path(self.defaults.system_prompt))),
                )
            )
        if provider is None or not provider.args:
            args.extend(self.defaults.args)
        else:
            args.extend(provider.args)
        # The claude CLI appends only the LAST --append-system-prompt-file, and the
        # supervisor tells whether a child already carries the law from the flag and
        # its path as two items. Either shape would let a child run without the law
        # while the plugin's copy is silenced (review of PR #853, 2026-09-26).
        flag = "--append-system-prompt-file"
        who = f"provider {provider.name}" if provider is not None else "the defaults"
        roster_path = self.path or Path("<roster>")
        if args.count(flag) > 1:
            raise RosterError.system_prompt_not_once(
                roster_path,
                who,
                f"{flag} more than once, and the CLI keeps only the last",
            )
        if any(arg.startswith(f"{flag}=") for arg in args):
            raise RosterError.system_prompt_not_once(
                roster_path, who, f"{flag}=PATH, which nSPAWN cannot pair with its path"
            )

        # AN EFFORT THE VENDOR DOES NOT KNOW IS A REFUSED TURN, NOT A SLOWER
        # ONE, so the global default reaches a spawn only where the roster
        # shows it is safe. A provider naming its own effort keeps it; a
        # provider whose declared ladder carries the default takes it (and a
        # provider declaring no ladder cannot be checked, so it takes it as
        # before); with NO provider of this kind the default is not invented.
        effort: str | None = None
        if provider is not None and provider.effort is not None:
            effort = provider.effort
        elif (
            provider is not None
            and self.defaults.effort is not None
            and (not provider.efforts or self.defaults.effort in provider.efforts)
        ):
            effort = self.defaults.effort
        env = self.env_of(provider) or None
        return Resolved(args=args, effort=effort, plugin_root=plugin_root, env=env)

    def refuse_unservable_spawn(self, provider_name: str | None, model: str) -> None:
        """Refuse a spawn the selected provider cannot serve, before any process exists.

        Both checks are against DECLARED lists and silent on undeclared ones.
        ONLY an explicitly named provider is checked: a caller naming none
        rides the roster's default account, and enforcing that account's lists
        on it changed behaviour a caller never chose.

        Raises:
            NSpawnError(NERR_1105, ...): the named model is not in the
                provider's declared `models`. NERR_1105 is the wire-level
                code the MCP boundary converts to a JSON-RPC error — a
                bare `ValueError` would cross the boundary as a parse
                refusal and terminate the session without a typed answer.
            NSpawnError(NERR_1120, ...): the provider's `effort` is not
                in its own `efforts` ladder. Same boundary reason.
        """
        if provider_name is None:
            return
        provider = self.select(provider_name)
        if provider is None:
            return
        if not provider.serves(model):
            raise NSpawnError(
                NERR_1105,
                f"model {model} is not served by provider {provider.name}; served models "
                f"(spawn.toml, [[provider]] {provider.name}, models): "
                f"[{', '.join(provider.models)}]",
            )
        if (
            provider.effort is not None
            and provider.efforts
            and provider.effort not in provider.efforts
        ):
            raise NSpawnError(
                NERR_1124,
                f'provider {provider.name} declares effort = "{provider.effort}" outside its '
                f"own efforts ladder [{', '.join(provider.efforts)}]; fix spawn.toml "
                f"([[provider]] {provider.name}, effort / efforts)",
            )


@dataclass
class Resolved:
    """Everything one spawn needs from the roster, resolved once."""

    args: list[str] = field(default_factory=list)
    effort: str | None = None
    plugin_root: Path | None = None
    #: ``Some`` only when the provider declares environment; ``None`` keeps
    #: the caller on the plain minted-home path.
    env: dict[str, str] | None = None


def _parse_roster(path: Path, text: str) -> Roster:
    try:
        document = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, ValueError) as error:
        raise RosterError.malformed(path, str(error)) from error
    defaults_table = document.get("defaults", {})
    if not isinstance(defaults_table, dict):
        raise RosterError.malformed(path, "defaults is not a table")
    for key in defaults_table:
        _refuse_retired(path, f"defaults.{key}", f"defaults.{key}")
    defaults = Defaults(
        model=_optional_str(path, "defaults.model", defaults_table.get("model")),
        effort=_optional_str(path, "defaults.effort", defaults_table.get("effort")),
        args=_str_list(path, "defaults.args", defaults_table.get("args", [])),
        plugins=_str_list(path, "defaults.plugins", defaults_table.get("plugins", [])),
        system_prompt=_optional_str(
            path, "defaults.system_prompt", defaults_table.get("system_prompt")
        ),
        env=_str_map(path, "defaults.env", defaults_table.get("env", {})),
    )
    if defaults.plugins:
        defaults.plugin_root = Path(defaults.plugins[0])
    bins_table = document.get("bins", {})
    if not isinstance(bins_table, dict):
        raise RosterError.malformed(path, "bins is not a table")
    bins: dict[str, Path] = {}
    for kind, value in bins_table.items():
        if kind not in KINDS:
            raise RosterError.malformed(
                path, f"bins.{kind} is not one of {', '.join(KINDS)}"
            )
        binary = _optional_path(path, f"bins.{kind}", value)
        if binary is not None:
            bins[kind] = binary
    providers: list[Provider] = []
    for table in document.get("provider", []):
        if not isinstance(table, dict):
            raise RosterError.malformed(path, "provider entry is not a table")
        providers.append(_parse_provider(path, table))
    # The fill runs BEFORE the binary is judged: a DIRECT provider that takes
    # its executable from `[bins]` declares none of its own, and a refusal
    # raised inside `_parse_provider` would fire before this table was read.
    for provider in providers:
        if provider.bin is None:
            provider.bin = bins.get(provider.kind)
        if provider.bin is None and provider.kind in DIRECT_KINDS:
            raise RosterError.malformed(
                path,
                f'provider "{provider.name}" has kind {provider.kind} and no bin: '
                f"a direct executable is declared, never guessed; declare "
                f"[bins].{provider.kind}",
            )
    # The table's name before System1 replaced the external Jev service. Unknown tables
    # are ignored here, so a roster that still carries it would lose its endpoint in
    # silence; it is refused by that name instead.
    if "jev" in document:
        raise RosterError.malformed(
            path, "[jev] was renamed to [system1]; rename the table"
        )
    system1_table = document.get("system1", {})
    if not isinstance(system1_table, dict):
        raise RosterError.malformed(path, "system1 is not a table")
    proibidas = sorted(SYSTEM1_CREDENTIAL_KEYS & set(system1_table))
    if proibidas:
        raise RosterError.malformed(
            path,
            f"system1.{proibidas[0]} is a credential; the roster is readable "
            "configuration and the key comes from NOTT_SYSTEM1_API_KEY",
        )
    tempo = system1_table.get("timeout_seconds")
    if tempo is not None and (
        isinstance(tempo, bool) or not isinstance(tempo, (int, float)) or tempo <= 0
    ):
        raise RosterError.malformed(
            path, "system1.timeout_seconds is not a positive number"
        )
    system1 = System1(
        endpoint=_optional_str(path, "system1.endpoint", system1_table.get("endpoint")),
        model=_optional_str(path, "system1.model", system1_table.get("model")),
        timeout_seconds=None if tempo is None else float(tempo),
    )
    classes_table = document.get("classes", {})
    if not isinstance(classes_table, dict):
        raise RosterError.malformed(path, "classes is not a table")
    classes: dict[str, ClassPolicy] = {}
    for class_name, policy_table in classes_table.items():
        if not isinstance(policy_table, dict):
            raise RosterError.malformed(path, f"classes.{class_name} is not a table")
        effort = _optional_str(
            path, f"classes.{class_name}.effort", policy_table.get("effort")
        )
        if effort is None:
            raise RosterError.malformed(path, f"classes.{class_name}.effort is missing")
        ceiling = _optional_str(
            path, f"classes.{class_name}.ceiling", policy_table.get("ceiling")
        )
        max_strength = _rank(
            path,
            policy_table.get("max_strength"),
            f"classes.{class_name}.max_strength",
        )
        fast = policy_table.get("fast", False)
        if not isinstance(fast, bool):
            raise RosterError.malformed(
                path, f"classes.{class_name}.fast is not a boolean"
            )
        try:
            classes[class_name] = ClassPolicy(
                effort=effort,
                ceiling=ceiling or effort,
                max_strength=max_strength,
                fast=fast,
            )
        except ValueError as error:
            raise RosterError.malformed(
                path, f"classes.{class_name}: {error}"
            ) from error
    # A roster that governs classes at all must declare the fail-closed one: it
    # is what a DECLARED class falls back to when the System1 cannot observe it.
    # Absent, an outage would ESCALATE.
    #
    # It does NOT say the same on both axes, and the message must not pretend
    # it does. A dispatch with no class at all — `nspawn_batch_start`, `nspawn_route_ping`,
    # the credential repick — takes this policy for EFFORT (`_class_effort`
    # reads `execution_class or UNCLASSIFIED`) and takes NO strength cap
    # (`_class_strength_policy` returns `None` for a classless dispatch, by the
    # decision that those three are probes across every provider). An owner who
    # read "every unobserved dispatch" here would write `unclassified.max_
    # strength` believing it caps the batch, and it does not.
    if classes and UNCLASSIFIED not in classes:
        raise RosterError.malformed(
            path,
            f"classes declares {', '.join(sorted(classes))} but not "
            f"{UNCLASSIFIED}, the class a declared one falls back to when the "
            "System1 cannot observe it",
        )
    if classes:
        classes.setdefault("implementation", _implementation_fallback(classes))
    writer_table = document.get("native_write", {})
    if not isinstance(writer_table, dict):
        raise RosterError.malformed(path, "native_write is not a table")
    native_write_roots: list[Path] = []
    for root in _str_list(path, "native_write.roots", writer_table.get("roots", [])):
        expanded = expand_home(Path(root))
        if not expanded.is_absolute():
            raise RosterError.malformed(
                path, f"native_write.roots entry {root!r} is not an absolute path"
            )
        native_write_roots.append(expanded)
    roles_table = document.get("roles", {})
    if not isinstance(roles_table, dict):
        raise RosterError.malformed(path, "roles is not a table")
    declared = {provider.name for provider in providers}
    for role, route in roles_table.items():
        if role not in ROLES:
            raise RosterError.malformed(
                path, f"roles.{role} is not one of {', '.join(ROLES)}"
            )
        if not isinstance(route, str) or route not in declared:
            raise RosterError.malformed(
                path, f"roles.{role} names no declared provider: {route!r}"
            )
    return Roster(
        defaults=defaults,
        providers=providers,
        bins=bins,
        system1=system1,
        classes=classes,
        native_write_roots=tuple(native_write_roots),
        roles=dict(roles_table),
        role_profiles=_role_profiles(path, document, roles_table, providers, classes),
        preferences=_preferences(path, document, providers),
    )


def _preferences(
    path: Path, document: dict[str, object], providers: list[Provider]
) -> dict[str, list[tuple[str, str | None]]]:
    """`[preference]`: `<class> = ["<route>[/<model>]", ...]`, checked here.

    A route is a declared provider or account alias. A model must be one the
    route's provider declares, when it declares a list. A reader that predates
    the table ignores it, as it ignores every unknown top-level table.
    """
    table = document.get("preference", {})
    if not isinstance(table, dict):
        raise RosterError.malformed(path, "preference is not a table")
    owners = {provider.name: provider for provider in providers}
    for provider in providers:
        for alias in provider.discovered_accounts():
            owners.setdefault(alias, provider)
    preferences: dict[str, list[tuple[str, str | None]]] = {}
    for name, entries in table.items():
        label = f"preference.{name}"
        if name not in PREFERENCE_CLASSES:
            raise RosterError.malformed(
                path, f"{label} is not one of {', '.join(PREFERENCE_CLASSES)}"
            )
        ordered: list[tuple[str, str | None]] = []
        for entry in _str_list(path, label, entries):
            # A route name never holds "/"; a model id may (`vendor/model`).
            route, slash, model = entry.partition("/")
            owner = owners.get(route)
            if owner is None:
                raise RosterError.malformed(
                    path, f"{label} names no declared provider or account: {route!r}"
                )
            if slash and (not model or (owner.models and model not in owner.models)):
                raise RosterError.malformed(
                    path,
                    f"{label}: {entry} names a model that {owner.name} does not serve",
                )
            ordered.append((route, model or None))
        preferences[name] = ordered
    return preferences


def _implementation_fallback(classes: dict[str, ClassPolicy]) -> ClassPolicy:
    """The policy `implementation` takes from a table written before it existed.

    Left out, the class would be governed by nothing: no ceiling on a pinned
    effort, no strength cap, and no cost the System1 or the `unclassified` fall
    can be compared against. So it takes `review`, else `judgment` — settled
    work needs no more than either — else the most restrictive strength-ranked
    class declared, else the fail-closed `unclassified` every governed table has.
    """
    for name in ("review", "judgment"):
        if name in classes:
            return classes[name]
    declared = [classes[n] for n in ("planning", "verification") if n in classes]
    if not declared:
        return classes[UNCLASSIFIED]
    return min(
        declared,
        key=lambda policy: (
            256 if policy.max_strength is None else policy.max_strength,
            EFFORT_LADDER.index(policy.ceiling),
            EFFORT_LADDER.index(policy.effort),
        ),
    )


def _role_profiles(
    path: Path,
    document: dict[str, object],
    roles: dict[str, str],
    providers: list[Provider],
    classes: dict[str, ClassPolicy],
) -> dict[str, dict[str, str]]:
    """`[role_profile.<role>]`, checked against the route `[roles]` binds.

    A separate table, not a table in `[roles]`: a reader that predates it
    ignores an unknown top-level table and keeps loading the roster. The effort
    meets the picker's own rules for the role's class here, because the
    workflow freezes it into its request and a roster fix would never reach
    that request.
    """
    from .picker import CODEX_REWRITTEN_EFFORTS, served_efforts  # picker imports us

    table = document.get("role_profile", {})
    if not isinstance(table, dict):
        raise RosterError.malformed(path, "role_profile is not a table")
    profiles: dict[str, dict[str, str]] = {}
    for role, profile in table.items():
        label = f"role_profile.{role}"
        if role not in PROFILED_ROLES:
            raise RosterError.malformed(
                path, f"{label}: only {', '.join(PROFILED_ROLES)} takes a profile"
            )
        if not isinstance(profile, dict) or not profile:
            raise RosterError.malformed(path, f"{label} is not a table of model/effort")
        route = roles.get(role)
        if route is None:
            raise RosterError.malformed(
                path, f"{label} needs roles.{role} to name the route it applies to"
            )
        provider = next(p for p in providers if p.name == route)
        class_name = PROFILE_CLASSES[role]
        policy = classes.get(class_name)
        for key, value in profile.items():
            if key not in ("model", "effort"):
                raise RosterError.malformed(
                    path, f"{label}.{key} is not model or effort"
                )
            if not isinstance(value, str) or not value:
                raise RosterError.malformed(path, f"{label}.{key} is not a name")
            served = provider.models if key == "model" else served_efforts(provider)
            rewritten = (
                key == "effort"
                and provider.kind == "codex"
                and value in CODEX_REWRITTEN_EFFORTS
            )
            admitted = (
                not provider.models or value in served
                if key == "model"
                else not provider.efforts or value in served
            )
            if rewritten or not admitted:
                raise RosterError.malformed(
                    path, f"{label}.{key} {value!r} is not served by {route}"
                )
            if (
                key == "effort"
                and policy is not None
                and value in EFFORT_LADDER
                and EFFORT_LADDER.index(value) > EFFORT_LADDER.index(policy.ceiling)
            ):
                raise RosterError.malformed(
                    path,
                    f"{label}.{key} {value!r} is above the {class_name} class ceiling "
                    f"{policy.ceiling!r}",
                )
        profiles[role] = dict(profile)
    return profiles


def _refuse_retired(path: Path, spelling: str, written_as: str) -> None:
    """Refuse one retired key by name, carrying the spelling that replaces it."""
    replacement = RETIRED_KEYS.get(spelling)
    if replacement is not None:
        raise RosterError.malformed(
            path, f"{written_as} is retired; declare {replacement} instead"
        )


def _rank(
    path: Path, value: object, label: str = "provider tiering rank"
) -> int | None:
    """One u8 tiering rank, shared by provider tiering and the class ceiling.

    The label names the field that was wrong: a bad class ceiling must not send
    the owner looking through `[[provider]]`.
    """
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 255:
        return value
    raise RosterError.malformed(path, f"{label} is not a u8")


def _digit_codes(path: Path, field_name: str, value: object) -> frozenset[str]:
    """ASCII digit strings only: the one shape the CLI's `[<code>]` can carry."""
    refusal = RosterError.malformed(
        path, f"{field_name} is not a list of digit strings"
    )
    if not isinstance(value, list):
        raise refusal
    codes: set[str] = set()
    for item in value:
        if not isinstance(item, str) or re.fullmatch(r"[0-9]+", item) is None:
            raise refusal
        codes.add(item)
    return frozenset(codes)


def _parse_provider(path: Path, table: dict[str, object]) -> Provider:
    name = table.get("name")
    if not isinstance(name, str) or not name:
        raise RosterError.malformed(path, "provider.name is missing or not a string")
    tiering = table.get("strength"), table.get("thrift"), table.get("design")
    kind = _optional_str(path, "provider.kind", table.get("kind")) or "claude"
    if kind not in KINDS:
        raise RosterError.malformed(
            path, f"provider.kind {kind!r} is not one of {', '.join(KINDS)}"
        )
    if table.get("proxy") is not None:
        raise RosterError.malformed(
            path,
            f'provider "{name}" declares proxy: proxy routes are retired',
        )
    for key in table:
        _refuse_retired(path, f"provider.{key}", f'provider "{name}" key {key}')
    provider = Provider(
        name=name,
        kind=kind,
        secrets=_optional_path(path, "provider.secrets", table.get("secrets")),
        auth_env=_optional_str(path, "provider.auth_env", table.get("auth_env")),
        oauth_token_file=_optional_path(
            path, "provider.oauth_token_file", table.get("oauth_token_file")
        ),
        # `bin` is filled from `[bins]` in `_parse_roster`; the provider table
        # no longer spells it.
        env=_str_map(path, "provider.env", table.get("env", {})),
        args=_str_list(path, "provider.args", table.get("args", [])),
        model=_optional_str(path, "provider.model", table.get("model")),
        model_fast=_optional_str(path, "provider.model_fast", table.get("model_fast")),
        models=_str_list(path, "provider.models", table.get("models", [])),
        effort=_optional_str(path, "provider.effort", table.get("effort")),
        efforts=_str_list(path, "provider.efforts", table.get("efforts", [])),
        default=table.get("default", False) is True,
        strength=_rank(path, tiering[0]),
        thrift=_rank(path, tiering[1]),
        design=_rank(path, tiering[2]),
        accounts=_native_accounts(path, kind, table.get("accounts", {})),
        declared="accounts" in table,
        excluded_accounts=_str_list(
            path, "provider.excluded_accounts", table.get("excluded_accounts", [])
        ),
        exhausted_codes=_digit_codes(
            path, "provider.exhausted_codes", table.get("exhausted_codes", [])
        ),
        named_only=_flag(path, "provider.named_only", table.get("named_only", False)),
        fill_first=_flag(path, "provider.fill_first", table.get("fill_first", False)),
        quota_source=_optional_str(
            path, "provider.quota_source", table.get("quota_source")
        ),
    )
    if provider.named_only and provider.default:
        raise RosterError.malformed(
            path, f'provider "{name}" is named_only and default: choose one'
        )
    if provider.quota_source not in (None, "colab"):
        raise RosterError.malformed(
            path, f'provider "{name}" provider.quota_source is not "colab"'
        )
    # A claude-kind provider MAY now carry accounts (the local qwen routes are
    # accounts with a `base_url` and no vendor home), but the mixing rule below
    # survives for it unchanged: a provider-level credential beside per-account
    # ones leaves nothing saying which of the two a given account spawns with.
    if provider.accounts and (
        provider.token_file
        or provider.oauth_token_file
        or provider.auth_env
        # A shared `secrets` directory is consumed ONLY through an account that
        # names the variable to carry the token: `native.prepare` reads
        # `<secrets>/<alias>.token` inside `if account.auth_env is not None`
        # (native.py:835-863). With accounts declared and not one of them naming
        # an auth_env, nothing can ever read the directory, so it is a
        # provider-level credential sitting beside native accounts — the mixing
        # this refusal exists to name.
        or (
            provider.secrets
            and not any(
                account.auth_env is not None for account in provider.accounts.values()
            )
        )
    ):
        raise RosterError.malformed(
            path,
            f'provider "{name}" accounts cannot mix with provider token_file, '
            "oauth_token_file, auth_env or an unreadable secrets directory. "
            "Declare the credential on each account instead: an account names "
            "its own auth_env and reads <secrets>/<alias>.token",
        )
    if provider.kind in DIRECT_KINDS:
        # An explicit empty list is a provider with no account yet: it reads, and
        # every run on it is refused until one is registered.
        if not (
            provider.declared
            or provider.secrets
            or provider.token_file
            or provider.oauth_token_file
        ):
            raise RosterError.malformed(
                path,
                f'provider "{name}" has kind {kind} and no secrets, '
                "oauth_token_file or accounts: a direct kind requires a "
                "credential source",
            )
        if not provider.model:
            raise RosterError.malformed(
                path,
                f'provider "{name}" has kind {kind} and no model: a direct kind '
                "never takes the fleet default",
            )
        return provider
    return provider


def _flag(path: Path, label: str, value: object) -> bool:
    if not isinstance(value, bool):
        raise RosterError.malformed(path, f"{label} is not true or false")
    return value


def _native_accounts(path: Path, kind: str, value: object) -> dict[str, NativeAccount]:
    if isinstance(value, list):
        return _accounts_from_homes(path, value)
    if not isinstance(value, dict):
        raise RosterError.malformed(
            path, "provider.accounts is not a table or a list of homes"
        )
    accounts: dict[str, NativeAccount] = {}
    for alias, table in value.items():
        field_name = f"provider.accounts.{alias}"
        if (
            not isinstance(alias, str)
            or not alias.strip()
            or not isinstance(table, dict)
        ):
            raise RosterError.malformed(path, f"{field_name} needs an alias and table")
        home = _optional_path(path, f"{field_name}.home", table.get("home"))
        # Codex and AGY require a directory. Claude can inherit its configuration.
        if home is None and kind in DIRECT_KINDS:
            raise RosterError.malformed(path, f"{field_name}.home is required")
        base_url = _optional_str(path, f"{field_name}.base_url", table.get("base_url"))
        if base_url is not None and kind in DIRECT_KINDS:
            raise RosterError.malformed(
                path,
                f"{field_name}.base_url is not accepted on kind {kind}: a direct "
                "executable resolves its own endpoint from the vendor home",
            )
        # Checked here as well as in `__post_init__`, so the refusal carries the
        # dotted key the owner has to go and edit, not just the rule it broke.
        if base_url is not None and re.fullmatch(r"https?://\S+", base_url) is None:
            raise RosterError.malformed(
                path, f"{field_name}.base_url is not an http:// or https:// endpoint"
            )
        try:
            accounts[alias] = NativeAccount(
                home=home,
                sqlite_home=_optional_path(
                    path, f"{field_name}.sqlite_home", table.get("sqlite_home")
                ),
                auth_env=_optional_str(
                    path, f"{field_name}.auth_env", table.get("auth_env")
                ),
                model_provider=_optional_str(
                    path, f"{field_name}.model_provider", table.get("model_provider")
                ),
                base_url=base_url,
                model=_optional_str(path, f"{field_name}.model", table.get("model")),
                capacity=_capacity(path, field_name, table.get("capacity")),
                bootstrap=_bootstrap(path, field_name, table.get("bootstrap")),
                env=_str_map(path, f"{field_name}.env", table.get("env", {})),
            )
        except ValueError as error:
            raise RosterError.malformed(path, f"{field_name}: {error}") from error
    return accounts


def _accounts_from_homes(path: Path, homes: list[object]) -> dict[str, NativeAccount]:
    """``accounts = ["~/.codex-one"]`` — one home per entry, alias derived.

    The shorthand for the common case; the table form stays the only way to
    declare `sqlite_home`, `auth_env` or `model_provider`, which have nowhere
    to live inside a bare path.
    """
    accounts: dict[str, NativeAccount] = {}
    for index, entry in enumerate(homes):
        field_name = f"provider.accounts[{index}]"
        home = _optional_path(path, field_name, entry)
        if home is None:
            raise RosterError.malformed(path, f"{field_name} is required")
        alias = home.name.lstrip(".")
        if not alias:
            raise RosterError.malformed(
                path, f"{field_name} derives no alias from {str(home)!r}"
            )
        if alias in accounts:
            raise RosterError.malformed(
                path,
                f"provider.accounts derives the alias {alias!r} twice; "
                "a list of homes cannot name two accounts the same",
            )
        try:
            accounts[alias] = NativeAccount(home=home)
        except ValueError as error:
            raise RosterError.malformed(path, f"{field_name}: {error}") from error
    return accounts


def _capacity(path: Path, field_name: str, value: object) -> int:
    if value is None:
        return 1
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise RosterError.malformed(
            path, f"{field_name}.capacity is not a positive integer"
        )
    return value


def _bootstrap(path: Path, field_name: str, value: object) -> tuple[str, ...]:
    """argv that wakes a sleeping route, never a shell line."""
    if value is None:
        return ()
    if not isinstance(value, list):
        raise RosterError.malformed(
            path,
            f"{field_name}.bootstrap is not a list: it is argv, executed with no "
            "shell, so a string would carry expansion, quoting and chaining that "
            "nothing here interprets",
        )
    if not value:
        raise RosterError.malformed(path, f"{field_name}.bootstrap is empty")
    for argument in value:
        if not isinstance(argument, str) or not argument.strip():
            raise RosterError.malformed(
                path, f"{field_name}.bootstrap holds a nonempty-string-less entry"
            )
        if any(ch in argument for ch in "\0\n\r"):
            raise RosterError.malformed(
                path, f"{field_name}.bootstrap entry {argument!r} is not a single line"
            )
    return tuple(value)


def _optional_str(path: Path, field_name: str, value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        if not value:
            raise RosterError.malformed(path, f"{field_name} is empty")
        return value
    raise RosterError.malformed(path, f"{field_name} is not a string")


def _optional_path(path: Path, field_name: str, value: object) -> Path | None:
    if value is None:
        return None
    if isinstance(value, str):
        # An empty string is refused, never read as Path(".") — the current directory
        # would become a credential store or an executable nobody declared.
        if not value:
            raise RosterError.malformed(path, f"{field_name} is empty")
        return Path(value)
    raise RosterError.malformed(path, f"{field_name} is not a path string")


def _str_list(path: Path, field_name: str, value: object) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RosterError.malformed(path, f"{field_name} is not a list of strings")
    return list(value)


def _str_map(path: Path, field_name: str, value: object) -> dict[str, str]:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        raise RosterError.malformed(path, f"{field_name} is not a table of strings")
    return dict(value)


def expand_home(path: Path) -> Path:
    """Expand a leading ``~`` against ``HOME``; anything else returns as-is."""
    text = str(path)
    if not text.startswith("~/"):
        return Path(text)
    home = os.environ.get("HOME")
    if home is None:
        return Path(text)
    return Path(home) / text[2:]


#: A `plugins` entry naming a GitHub repository rather than a directory.
GH_PLUGIN_PREFIX = "gh:"


def plugin_flag(entry: str, path: Path) -> tuple[str, str]:
    """One `plugins` entry as the flag pair it becomes.

    `gh:org/repo` or `gh:org/repo@ref` becomes `--plugin-url` at that
    repository's zip; anything else is a directory and becomes `--plugin-dir`.
    The vendor accepts both, repeatably.

    A malformed `gh:` entry is REFUSED rather than guessed: silently treating
    `gh:not-a-repo` as a directory would spawn a child whose plugin never
    loads, and the failure would surface as a missing tool much later.
    """
    if not entry.startswith(GH_PLUGIN_PREFIX):
        return "--plugin-dir", str(expand_home(Path(entry)))
    spec = entry[len(GH_PLUGIN_PREFIX) :]
    repo, _, ref = spec.partition("@")
    owner, slash, name = repo.partition("/")
    if not (owner and slash and name):
        raise RosterError.malformed(
            path,
            f"defaults.plugins entry {entry!r} is not a repository: "
            f"expected {GH_PLUGIN_PREFIX}owner/name or "
            f"{GH_PLUGIN_PREFIX}owner/name@ref",
        )
    target = ref or "HEAD"
    return "--plugin-url", f"https://github.com/{owner}/{name}/archive/{target}.zip"


def expand_value_with(
    value: str,
    plugin_root: Path | None,
    env: Mapping[str, str] | None = None,
) -> str:
    """Expand ``$PLUGIN`` and ``$NAME`` references in one roster value.

    ``$PLUGIN`` is caller-supplied rather than ambient: a per-spawn value read
    out of the ambient environment would be shared by every concurrent run.
    A reference that resolves to nothing expands to the EMPTY string, never
    to its own name — a ``$ZAI_API_KEY`` arriving at the executor as those
    characters would report an auth failure in the wrong place.
    """
    out: list[str] = []
    rest = value
    while True:
        at = rest.find("$")
        if at < 0:
            out.append(rest)
            break
        out.append(rest[:at])
        after = rest[at + 1 :]
        end = len(after)
        for index, ch in enumerate(after):
            if not (ch.isascii() and (ch.isalnum() or ch == "_")):
                end = index
                break
        name, rest = after[:end], after[end:]
        if not name:
            out.append("$")
        elif name == "PLUGIN":
            if plugin_root is not None:
                out.append(str(plugin_root))
        else:
            found = (os.environ if env is None else env).get(name)
            if found is not None:
                out.append(found)
    return "".join(out)


# --- writing one provider's accounts ----------------------------------------------

#: Where `child._deliver_credential` puts a claude-kind account's token when neither
#: the account nor its provider names a variable. Same spelling as
#: `child.CLAUDE_OAUTH_TOKEN_ENV`, not imported for the cycle `ANTHROPIC_BASE_URL_KEY`
#: already names.
CLAUDE_OAUTH_TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"

#: A key TOML reads without quotes.
_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")

#: A provider's own `auth_env` the way the writer can lift it out whole: one line,
#: a plain string, no comment riding on it.
_PLAIN_AUTH_ENV = re.compile(r"""\s*auth_env\s*=\s*"[A-Za-z_][A-Za-z0-9_]*"\s*""")


class RosterEditRefused(RosterError):
    """An account edit the writer will not make. Its message names no host path,
    so a caller may show it to whoever asked for the edit as it stands."""


def make_default_account(path: Path, provider_name: str, alias: str) -> Path | None:
    """Put ``alias`` first among ``provider_name``'s accounts: it becomes the default.

    Returns the backup of the roster as it was, or None when it already was.
    """

    def first(order: list[str]) -> list[str]:
        _refuse_unknown(provider_name, alias, order)
        return [alias, *(other for other in order if other != alias)]

    return write_accounts(path, provider_name, first)


def remove_account(path: Path, provider_name: str, alias: str) -> Path | None:
    """Take ``alias`` out of ``provider_name``'s accounts in the roster only.

    The default is never removed, so a provider keeps the account its runs use.
    Returns the backup of the roster as it was.
    """

    def without(order: list[str]) -> list[str]:
        _refuse_unknown(provider_name, alias, order)
        if alias == order[0]:
            raise RosterEditRefused(
                f"{alias} is the default of {provider_name}; make another account "
                "the default first"
            )
        return [other for other in order if other != alias]

    return write_accounts(path, provider_name, without)


def _refuse_unknown(provider_name: str, alias: str, order: list[str]) -> None:
    if alias not in order:
        raise RosterEditRefused(f"{provider_name} has no account {alias!r}")


def write_accounts(
    path: Path,
    provider_name: str,
    aliases: Sequence[str] | Callable[[list[str]], Sequence[str]],
) -> Path | None:
    """Write ``aliases`` as the accounts of ``provider_name``, in that order, and
    return the backup of the file as it was, or None when nothing changed.

    ``aliases`` may be a function of the accounts as they stand: it runs under the
    lock, so an edit computed from them never overwrites one that landed meanwhile.

    The first account is the default: the one the picker sends an idle dispatch to
    and the one an absent alias means (`child._selected_account`). Only an account
    the provider already has can be kept, so this reorders and removes, never adds,
    and never touches an account's home or its credential.

    Every byte outside the moved accounts stays as the owner wrote it. An account
    declared as its own `[provider.accounts.<alias>]` table moves whole, with its
    comments and any key this loader does not read. Accounts discovered in the
    secrets directory become such tables, reading the same `<secrets>/<alias>.token`
    under the same variable. Any other spelling (inline, a list of homes, dotted
    keys) is refused rather than rewritten. The result must parse to the same TOML
    document with only those accounts changed, and pass the owning reader, before
    it replaces the file; writers of this function take turns on a lock beside it.

    Raises:
        RosterEditRefused: an unknown provider, a list that is not some of its own
            accounts each once, a spelling the writer cannot keep, a rewrite that
            would change anything else, or a file that changed while it was edited.
        RosterError: the file could not be read, parsed or written.
    """
    target = path.resolve()
    with _writer_lock(target):
        raw, text, roster, index = _read_for_edit(path, target, provider_name)
        current = roster.providers[index].ordered_accounts()
        wanted = list(aliases(list(current)) if callable(aliases) else aliases)
        provider = roster.providers[index]
        if wanted == current and provider.declared:
            return None
        if not provider.declared and provider_name in wanted:
            # An alias that is its provider's name means the default to a run
            # (`child._selected_account`), so it cannot be declared.
            raise RosterEditRefused(
                f"{provider_name} finds an account named {provider_name}, which a "
                f"run reads as its default: rename {provider_name}.token, then try "
                "again"
            )
        if (
            not wanted
            or len(set(wanted)) < len(wanted)
            or not set(wanted) <= set(current)
        ):
            raise RosterEditRefused(
                f"{provider_name} keeps one or more of its own accounts, each once: "
                f"{', '.join(current) or 'it has none'}"
            )
        return _write_order(path, target, raw, text, provider, index, wanted)


def _write_order(
    path: Path,
    target: Path,
    raw: bytes,
    text: str,
    provider: Provider,
    index: int,
    wanted: list[str],
) -> Path:
    """`write_accounts` once ``wanted`` is settled, under its lock."""
    document = tomllib.loads(text)
    table = document["provider"][index]
    expected = copy.deepcopy(document)
    edited = expected["provider"][index]
    if "accounts" in table:
        rewritten = _moved_tables(text, index, table["accounts"], wanted)
        edited["accounts"] = {alias: table["accounts"][alias] for alias in wanted}
    else:
        if provider.kind in DIRECT_KINDS:
            raise RosterEditRefused(
                f"{provider.name} finds its accounts in a secrets directory, and "
                f"a {provider.kind} account needs its home: declare each as a "
                "[provider.accounts.<alias>] table"
            )
        variable = provider.auth_env or CLAUDE_OAUTH_TOKEN_ENV
        rewritten = _declared_tables(text, index, wanted, variable, provider)
        edited["accounts"] = {alias: {"auth_env": variable} for alias in wanted}
        edited.pop("auth_env", None)
    _refuse_other_changes(provider.name, rewritten, expected, index, wanted)
    return _replace_keeping_backup(path, target, raw, rewritten.encode("utf-8"))


def _refuse_other_changes(
    provider_name: str,
    rewritten: str,
    expected: dict[str, Any],
    index: int,
    wanted: list[str],
) -> None:
    """Refuse a rewrite that is not ``expected`` with the accounts in ``wanted``
    ORDER: TOML tables compare equal in any order, and the first is the default."""
    refusal = (
        f"the roster could not be rewritten to change only {provider_name}'s "
        "accounts; nothing was written"
    )
    try:
        after = tomllib.loads(rewritten)
    except tomllib.TOMLDecodeError as error:
        raise RosterEditRefused(refusal) from error
    accounts = after["provider"][index].get("accounts")
    if after != expected or not isinstance(accounts, dict) or list(accounts) != wanted:
        raise RosterEditRefused(refusal)


#: A new account's name: one shell word, so the command the app shows is safe to
#: paste, and a TOML bare key.
NEW_ACCOUNT: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")

#: Where each native kind's own login leaves its credential, inside the account home.
OAUTH_CREDENTIAL: Final = {
    "codex": "auth.json",
    "agy": ".gemini/antigravity-cli/antigravity-oauth-token",
}


@dataclass(frozen=True, slots=True)
class CredentialSource:
    """Where the secret of one run comes from: a token file, an ambient
    variable, or the OAuth credential in a native home."""

    kind: Literal["token", "env", "home"]
    #: The file path, or the variable name for `env`.
    location: str
    #: For `home`: the account home, which the native spawn resolves strictly.
    root: str | None = None
    #: For `env`: why no token file resolved, the resolver's own words.
    unresolved: str | None = None

    def key(self) -> str:
        """The file the spawn actually reads, as one text: two runs on one
        secret give one key. A token file on an absolute path is resolved
        through its links (`realpath`), a native home as the native spawn
        resolves it (`resolve(strict=True)`). A path that does not resolve,
        or a relative one (HOME unset), keeps its spelling, and that is logged:
        the key never reads the process cwd and never raises."""
        if self.kind == "env":
            return f"env:{self.location}"
        try:
            if not os.path.isabs(self.location):
                raise ValueError("the path is relative")
            if self.kind == "home" and self.root is not None:
                inner = Path(self.location).relative_to(self.root)
                resolved = str(Path(self.root).resolve(strict=True) / inner)
            else:
                resolved = os.path.realpath(self.location)
        except (OSError, ValueError, RuntimeError) as error:
            with contextlib.suppress(Exception):
                print(
                    f"nspawn.credential: {self.kind} {self.location} keeps its "
                    f"spelling in the refusal key: {type(error).__name__}",
                    file=sys.stderr,
                    flush=True,
                )
            return f"{self.kind}:{self.location}"
        return f"{self.kind}:{resolved}"


def credential_source(
    provider: Provider, account_alias: str | None
) -> CredentialSource:
    """The ONE answer to where a run's secret comes from. The spawn reads its
    secret here (`child._read_credential_bytes` for Claude, `native.prepare`
    for codex and AGY), and a failover keys a refusal by it, so the two never
    drift.

    A native route (`DIRECT_KINDS`) reads its API account's token file, else
    its OAuth credential inside the account home. Any other route reads the
    token file `resolve_token_file` gives, and only when none resolves, the
    `auth_env` variable.

    Raises:
        ValueError: the provider declares no source for this account.
    """
    if provider.kind in DIRECT_KINDS:
        alias = account_alias
        if alias is None or alias == provider.name:
            alias = next(iter(sorted(provider.accounts)), None)
        account = provider.resolve_native_account(alias)
        if account.auth_env is not None:
            return CredentialSource("token", str(provider.resolve_token_file(alias)))
        if account.home is None:
            raise ValueError(f'native account "{alias}" declares no home')
        return CredentialSource(
            "home",
            str(account.home / OAUTH_CREDENTIAL[provider.kind]),
            root=str(account.home),
        )
    try:
        return CredentialSource(
            "token", str(provider.resolve_token_file(account_alias))
        )
    except ValueError as error:
        if provider.auth_env is None:
            raise
        return CredentialSource("env", provider.auth_env, unresolved=str(error))


def login_home(kind: str, alias: str) -> str:
    """The fresh home the app proposes for a new ``kind`` account, under ``~``."""
    return f"~/.{kind}-{alias}"


def token_variable(provider: Provider) -> str:
    """The variable a token provider's accounts carry their token in."""
    declared = (a.auth_env for a in provider.accounts.values() if a.auth_env)
    return next(declared, None) or provider.auth_env or CLAUDE_OAUTH_TOKEN_ENV


def register_account(path: Path, provider_name: str, alias: str) -> Path:
    """Add ``alias`` as the last of ``provider_name``'s accounts, once the owner's
    own run of the vendor login left its credential: the only write is the roster's.

    The provider's accounts are an explicit list: opening Add declares a secrets
    directory's discovered accounts as they stand (`make_default_account` on the
    default), so a token file that appears later orders nothing. The new account
    joins the end of that list and the default never changes. A codex or agy
    account must have logged in on its proposed home (`login_home`), which no
    account may already name; a token provider's must have its
    `<secrets>/<alias>.token`. Returns the backup of the roster.

    Raises:
        RosterEditRefused: a name that is not new or not one word, an endpoint
            provider, a login that left no credential, or a rewrite the writer
            cannot keep.
        RosterError: the file could not be read, parsed or written.
    """
    target = path.resolve()
    with _writer_lock(target):
        raw, text, roster, index = _read_for_edit(path, target, provider_name)
        provider = roster.providers[index]
        _refuse_new_account(roster, provider, alias)
        declared = list(provider.accounts)
        if provider.kind in DIRECT_KINDS:
            home = login_home(provider.kind, alias)
            found = expand_home(Path(home))
            if not (found / OAUTH_CREDENTIAL[provider.kind]).is_file():
                raise RosterEditRefused(
                    f"{home} holds no {provider.kind} login yet: run the command "
                    "first, then register"
                )
            homes = [
                expand_home(a.home).resolve()
                for p in roster.providers
                for a in p.accounts.values()
                if a.home is not None
            ]
            if found.resolve() in homes:
                raise RosterEditRefused(f"an account already uses {home}")
            # Absolute: a node refuses an agy home under `~` (`route.agy_route`).
            table = {"home": str(found)}
            return _append_table(path, target, raw, text, index, declared, alias, table)
        directory = provider.secrets_dir()
        assert directory is not None  # `_refuse_new_account` checked it
        token = directory / f"{alias}.token"
        if not token.is_file() or token.stat().st_size == 0:
            raise RosterEditRefused(
                f"{provider_name} has no token for {alias} yet: put it in its file "
                "first, then register"
            )
        if provider.declared:
            table = {"auth_env": token_variable(provider)}
            return _append_table(path, target, raw, text, index, declared, alias, table)
        # The provider's first account: declared, so discovery orders nothing.
        return _write_order(path, target, raw, text, provider, index, [alias])


#: The longest key the app may paste; a vendor's is far shorter.
KEY_LIMIT: Final = 4096

#: Both account lock waits must finish before the app request expires.
ROSTER_LOCK_WAIT: Final = 5.0


def write_account_key(
    path: Path, provider_name: str, alias: str, key: str, *, new: bool
) -> Path:
    """Write the owner's pasted ``key`` for account ``alias`` of ``provider_name``.
    Return the secrets directory that received it.

    The file is exactly ``<secrets>/<alias>.token``, never another account's by a
    fallback. The secrets directory is opened once, never through a link, checked
    to be its owner's alone (0700), and held locked against every other writer of
    a key from the check to the rollback; every file operation goes through that
    handle. The key is written whole at 0600 with a trailing newline under a name
    new to this call, synced, put in place at once, and the directory synced. No
    copy of the old key is kept, and a key update never touches the roster. With
    ``new``, the account is not there yet: `register_account`'s own checks run
    before any file exists, its file is created, then the account is registered.
    A failure removes the staged file and the file this call made, and only that
    one, then syncs the directory; only then is the failure raised as it was. A
    codex or agy account logs in on its host instead. No message names the key or
    quotes what the caller sent.

    Raises:
        RosterEditRefused: a key or a name that cannot be written, an unknown
            provider or account, a codex or agy provider, a linked token file or
            a secrets directory others can reach; every key is as it was.
        KeyStateUnknown: a file was touched, and the failure or its cleanup left
            what is on disk unconfirmed; the first failure is its cause.
        RosterError: the roster could not be read or changed; every key is as it
            was.
        OSError: the key could not be written; every key is as it was.
    """
    if (
        not key
        or len(key) > KEY_LIMIT
        or not key.isprintable()
        or any(c.isspace() for c in key)
    ):
        raise RosterEditRefused(
            f"a key is one line of at most {KEY_LIMIT} visible characters, with no "
            "spaces"
        )
    if alias in ("", ".", key) or any(
        part in alias for part in ("/", "\\", "..", "\x00")
    ):
        raise RosterEditRefused("an account's name is one file name, not its key")
    roster = Roster.read(path)
    provider = next((p for p in roster.providers if p.name == provider_name), None)
    if provider is None:
        raise RosterEditRefused("the roster declares no such provider")
    if provider.kind in DIRECT_KINDS:
        raise RosterEditRefused(
            f"a {provider.kind} account has no key to paste: log in on the host"
        )
    directory = provider.secrets_dir()
    refusal = f"{provider.name}'s secrets directory must be yours alone (0700)"
    # Every read-only descriptor of this call, released after its answer is decided.
    with contextlib.ExitStack() as held:
        try:
            if directory is None:
                raise RosterEditRefused(refusal)
            handle = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError as error:
            raise RosterEditRefused(refusal) from error
        held.callback(_release, handle)
        found = os.fstat(handle)
        if stat.S_IMODE(found.st_mode) != 0o700 or found.st_uid != os.getuid():
            raise RosterEditRefused(refusal)
        _lock_acquire(handle, "The secrets directory")
        if new:
            _refuse_new_account(roster, provider, alias)
        elif alias not in provider.ordered_accounts():
            raise RosterEditRefused(f"{provider.name} has no such account")
        _put_key(path, handle, held, provider.name, alias, key, new=new)
        return directory.resolve()


def _release(descriptor: int) -> None:
    """Close a descriptor that wrote nothing. Its close neither publishes nor loses
    a byte, so a failure is logged on the host and never stands in for the answer."""
    try:
        os.close(descriptor)
    except OSError as error:
        _warn(f"a roster writer's descriptor would not close: {error.strerror}")


def _warn(text: str) -> None:
    """``text`` in the host's journal. A log that cannot be written (a broken,
    closed or missing stderr) is dropped: it never fails the operation it
    describes."""
    with contextlib.suppress(Exception):
        print(f"nspawn: {text}", file=sys.stderr)


def _write_all(descriptor: int, content: bytes) -> None:
    """``content`` whole on the raw ``descriptor``: no buffer is left to flush on
    its close."""
    while content:
        content = content[os.write(descriptor, content) :]


class KeyStateUnknown(RosterError):
    """A key file was touched, and what is on disk now could not be confirmed. Its
    message names neither a path nor the key; the first failure is its cause."""


def _put_key(
    path: Path,
    handle: int,
    held: contextlib.ExitStack,
    provider: str,
    alias: str,
    key: str,
    *,
    new: bool,
) -> None:
    """``key`` as ``<alias>.token`` in the directory ``handle`` holds, whole, and a
    new account registered in the roster at ``path``. ``held`` releases the key
    file's read-only descriptor after the cleanup.

    A new file never lands over one that is there; a replaced one is never a link.
    """
    name = f"{alias}.token"
    try:
        target = os.stat(name, dir_fd=handle, follow_symlinks=False)
    except FileNotFoundError:
        target = None
    if target is not None and new:
        raise RosterEditRefused("a token with that name is already there")
    if target is not None and (not stat.S_ISREG(target.st_mode) or target.st_nlink > 1):
        raise RosterEditRefused("the token file is a link: change it where it points")
    staged = f".{name}.{token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    made: int | None = None
    replaced = False
    descriptor = os.open(staged, flags, 0o600, dir_fd=handle)
    try:
        # Written on the raw descriptor, synced and closed before it is published:
        # nothing between its open and its close can leak it, and a close that fails
        # is one more failure before the link or the replace.
        try:
            os.fchmod(descriptor, 0o600)
            # Read-only, held until the cleanup is done: the number of an inode a
            # descriptor still holds is never another file's, so `_remove_own` can
            # tell the key's file apart.
            pin = os.open(staged, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=handle)
            held.callback(_release, pin)
            _write_all(descriptor, f"{key}\n".encode())
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        made = os.fstat(pin).st_ino
        if new:
            os.link(
                staged,
                name,
                src_dir_fd=handle,
                dst_dir_fd=handle,
                follow_symlinks=False,
            )
            # Gone before the directory is synced: a crash keeps one name.
            os.unlink(staged, dir_fd=handle)
        else:
            os.replace(staged, name, src_dir_fd=handle, dst_dir_fd=handle)
            replaced = True
        os.fsync(handle)
        if new:
            register_account(path, provider, alias)
    except BaseException as error:
        lost = (
            "the account was not added and its key file may remain"
            if new
            else "the key's state could not be confirmed; check this account"
        )
        if isinstance(error, RosterEditRefused):
            lost = f"{error.message}; {lost}"
        if replaced:
            raise KeyStateUnknown(lost) from error
        try:
            _remove_own(handle, staged, name if new else None, made)
        except OSError:
            raise KeyStateUnknown(lost) from error
        raise


def _remove_own(handle: int, staged: str, name: str | None, made: int | None) -> None:
    """Remove ``name`` while it is still the file of inode ``made``, which the
    caller holds open, and ``staged`` even when that fails, from the directory
    ``handle`` holds, then sync the directory."""
    try:
        if name is not None and made is not None:
            with contextlib.suppress(FileNotFoundError):
                if os.stat(name, dir_fd=handle, follow_symlinks=False).st_ino == made:
                    os.unlink(name, dir_fd=handle)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(staged, dir_fd=handle)
    os.fsync(handle)


def _refuse_new_account(roster: Roster, provider: Provider, alias: str) -> None:
    """What makes ``alias`` no new account of ``provider``, checked on the roster
    alone: by `register_account` under its lock, and by `write_account_key` before
    any file exists."""
    if NEW_ACCOUNT.fullmatch(alias) is None:
        raise RosterEditRefused(
            "an account's name is letters, digits, - and _, and starts with a "
            "letter or a digit"
        )
    if any(alias == p.name for p in roster.providers):
        raise RosterEditRefused(f"{alias} names a provider; choose another name")
    others = (p for p in roster.providers if p is not provider)
    holder = next((p for p in others if alias in p.ordered_accounts()), None)
    if holder is None and provider.declared and alias in provider.accounts:
        holder = provider
    if holder is not None:
        raise RosterEditRefused(f"{holder.name} already has an account {alias!r}")
    if not provider.declared and provider.discovered_accounts() not in ([], [alias]):
        # Discovery sorts by name: which file is new cannot be told, and a new
        # name that sorts first would already be the default.
        raise RosterEditRefused(
            f"{provider.name}'s accounts are not declared yet: open Add account "
            "again, which declares them as they stand, then register"
        )
    if provider.kind not in DIRECT_KINDS and (
        provider.secrets_dir() is None or not provider.adds_by_login()
    ):
        raise RosterEditRefused(
            f"{provider.name} has no login to add: its accounts are declared "
            "in the roster"
        )


def _append_table(
    path: Path,
    target: Path,
    raw: bytes,
    text: str,
    index: int,
    declared: list[str],
    alias: str,
    table: Mapping[str, str],
) -> Path:
    """``alias`` as a `[provider.accounts.<alias>]` table after the provider's last
    line, under `register_account`'s lock. Accounts spelled any other way than as
    their own tables are refused, since a table cannot join them."""
    lines, block = _provider_block(text, index)
    last = max(end for _, end, _ in block)
    blank = _newline(text)
    if not lines[last].endswith(("\n", "\r")):
        lines[last] += blank
    added = [blank, f"[provider.accounts.{_toml_key(alias)}]{blank}"]
    added += [f"{_toml_key(k)} = {_toml_string(v)}{blank}" for k, v in table.items()]
    rewritten = "".join(lines[: last + 1] + added + lines[last + 1 :])
    expected = tomllib.loads(text)
    edited = expected["provider"][index]
    accounts = edited.get("accounts", {})
    if not isinstance(accounts, dict):
        raise RosterEditRefused(
            "these accounts are not each written as their own "
            "[provider.accounts.<alias>] table, so a new one cannot join them; "
            "declare them that way first"
        )
    edited["accounts"] = {**accounts, alias: dict(table)}
    wanted = [*declared, alias]
    _refuse_other_changes(edited["name"], rewritten, expected, index, wanted)
    return _replace_keeping_backup(path, target, raw, rewritten.encode("utf-8"))


def _read_for_edit(
    path: Path, target: Path, provider_name: str
) -> tuple[bytes, str, Roster, int]:
    """The roster's bytes, text and record, and the index of ``provider_name``."""
    try:
        raw = target.read_bytes()
    except OSError as source:
        raise RosterError.unreadable(path, source) from source
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RosterError.malformed(path, "the file is not UTF-8") from error
    roster = _parse_roster(path, text)
    roster._validate(path)
    index = next(
        (n for n, p in enumerate(roster.providers) if p.name == provider_name), None
    )
    if index is None:
        raise RosterEditRefused(f"the roster declares no provider {provider_name!r}")
    return raw, text, roster, index


def _lock_acquire(descriptor: int, name: str) -> None:
    """Wait for the account lock until its deadline. Refuse before a write."""
    deadline = time.monotonic() + ROSTER_LOCK_WAIT
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RosterEditRefused(
                    f"{name} is busy. Retry the account change."
                ) from None
            time.sleep(min(0.05, remaining))


@contextlib.contextmanager
def _writer_lock(target: Path) -> Iterator[None]:
    """One writer of ``target`` at a time, across threads and processes.

    The lock is a file beside the roster, never the roster itself: replacing the
    roster gives it a new inode, which a lock held on the old one no longer guards.
    """
    lock = target.with_name(f".{target.name}.lock")
    try:
        descriptor = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as source:
        raise RosterError.unwritable(target, source) from source
    try:
        try:
            _lock_acquire(descriptor, "The provider roster")
        except OSError as source:
            raise RosterError.unwritable(target, source) from source
        yield
    finally:
        # The lock wrote nothing: once the roster is replaced, its close must not
        # turn a written roster into a failure.
        _release(descriptor)


def _moved_tables(
    text: str, index: int, accounts: Mapping[str, object], wanted: list[str]
) -> str:
    """``text`` with the provider's own account tables in ``wanted`` order.

    Each table moves whole, from the comments right above its header to its last
    key, at the place of the first one. Nothing else moves. Accounts that are not
    each their own table are refused: there is nothing whole to move.
    """
    lines, block = _provider_block(text, index)
    segments = [
        segment for segment in block if segment[2][:2] == ("provider", "accounts")
    ]
    named = {keys[2] for _, _, keys in segments if len(keys) > 2}
    if named != set(accounts) or any(len(keys) < 3 for _, _, keys in segments):
        raise RosterEditRefused(
            "these accounts are not each written as their own "
            "[provider.accounts.<alias>] table, so they cannot be moved whole; "
            "declare them that way first"
        )
    groups: dict[str, list[str]] = {}
    for start, end, keys in segments:
        groups.setdefault(keys[2], []).extend(lines[start : end + 1])
    blank = _newline(text)
    moved: list[str] = []
    for alias in wanted:
        moved += ([blank] if moved else []) + groups[alias]
    out = lines[: block[0][0]]
    position, placed = block[0][0], False
    for start, end, keys in block:
        _add_gap(out, lines[position:start])
        if keys[:2] != ("provider", "accounts"):
            out += lines[start : end + 1]
        elif not placed:
            out += moved
            placed = True
        position = end + 1
    _add_gap(out, lines[position:])
    return "".join(out)


def _declared_tables(
    text: str, index: int, wanted: list[str], variable: str, provider: Provider
) -> str:
    """``text`` with the provider's discovered accounts declared as tables.

    They go after the provider's last key. When the provider names the variable
    itself, its `auth_env` line moves onto each account, since a provider with
    accounts names none (`_parse_provider`); a line the writer cannot lift out
    whole is refused.
    """
    lines, block = _provider_block(text, index)
    header, end, _ = block[0]
    drop = None
    if provider.auth_env is not None:
        plain = [
            n
            for n in range(header, end + 1)
            if _PLAIN_AUTH_ENV.fullmatch(lines[n].rstrip("\r\n"))
        ]
        if len(plain) != 1:
            raise RosterEditRefused(
                f"{provider.name}'s auth_env is not one plain line the writer can "
                "move onto its accounts"
            )
        drop = plain[0]
    blank = _newline(text)
    last = max(segment_end for _, segment_end, _ in block)
    tables: list[str] = []
    for alias in wanted:
        tables += [
            blank,
            f"[provider.accounts.{_toml_key(alias)}]{blank}",
            f"auth_env = {_toml_string(variable)}{blank}",
        ]
    if not lines[last].endswith(("\n", "\r")):
        lines[last] += blank
    out = [line for n, line in enumerate(lines[: last + 1]) if n != drop]
    return "".join(out + tables + lines[last + 1 :])


def _provider_block(
    text: str, index: int
) -> tuple[list[str], list[tuple[int, int, tuple[str, ...]]]]:
    """The file's lines, and the ``index``-th provider's tables in it.

    Each table is (first line, last line, dotted key): it begins at the comments
    right above its header and ends at its last line that says something to TOML,
    so blank and comment lines between tables are left where they are.
    """
    lines = text.splitlines(keepends=True)
    heads = [(n, keys) for n, line in enumerate(lines) if (keys := _header_keys(line))]

    def opens_provider(n: int, keys: tuple[str, ...]) -> bool:
        return keys == ("provider",) and lines[n].lstrip().startswith("[[")

    starts = [k for k, (n, keys) in enumerate(heads) if opens_provider(n, keys)]
    if index >= len(starts):
        raise RosterEditRefused("the roster's provider tables could not be located")
    first = last = starts[index]
    last += 1
    while (
        last < len(heads)
        and heads[last][1][0] == "provider"
        and not opens_provider(*heads[last])
    ):
        last += 1

    def lead(n: int) -> int:
        while n > 0 and lines[n - 1].lstrip().startswith("#"):
            n -= 1
        return n

    bounds = [lead(n) for n, _ in heads[first:last]]
    bounds.append(lead(heads[last][0]) if last < len(heads) else len(lines))
    block = [
        (
            bounds[k],
            max(m for m in range(n, bounds[k + 1]) if _content(lines[m])),
            keys,
        )
        for k, (n, keys) in enumerate(heads[first:last])
    ]
    # The provider's own header keeps the comments above it where they are.
    block[0] = (heads[first][0], block[0][1], block[0][2])
    return lines, block


def _add_gap(out: list[str], gap: list[str]) -> None:
    """Append the blank and comment lines between two tables, folding a blank line
    that would double where a table moved away."""
    if out and not out[-1].strip():
        while gap and not gap[0].strip():
            gap = gap[1:]
    if out and gap and not out[-1].endswith(("\n", "\r")):
        out[-1] += _newline("".join(out))
    out += gap


def _newline(text: str) -> str:
    """The line ending the file already uses."""
    return "\r\n" if "\r\n" in text else "\n"


def _header_keys(line: str) -> tuple[str, ...] | None:
    """The dotted key a table header names, or None when ``line`` is no header.

    The stdlib parses the header, so a quoted key reads exactly as TOML says.
    """
    stripped = line.strip()
    if not stripped.startswith("["):
        return None
    try:
        node: object = tomllib.loads(stripped)
    except tomllib.TOMLDecodeError:
        return None
    keys: list[str] = []
    while isinstance(node, dict) and len(node) == 1:
        key, node = next(iter(node.items()))
        keys.append(key)
    return tuple(keys) or None


def _content(line: str) -> bool:
    """A line that says something to TOML: neither blank nor a comment."""
    stripped = line.strip()
    return bool(stripped) and not stripped.startswith("#")


def _toml_key(key: str) -> str:
    return key if _BARE_KEY.fullmatch(key) else _toml_string(key)


def _toml_string(value: str) -> str:
    """A TOML basic string: JSON's escapes are TOML's, astral characters left whole."""
    return json.dumps(value, ensure_ascii=False)


def _replace_keeping_backup(path: Path, target: Path, raw: bytes, new: bytes) -> Path:
    """Put ``new`` in place of ``target`` at once, keeping ``raw`` beside it.

    The new bytes are staged beside the roster and read back by `Roster.read`,
    the owner of every check a reader makes, before anything else happens. Called
    under `_writer_lock`; an edit by hand is caught by the comparison only.
    """
    try:
        mode = stat.S_IMODE(target.stat().st_mode)
    except OSError as source:
        raise RosterError.unwritable(path, source) from source
    staged = _fresh_file(target, f".{target.name}.{os.getpid()}.tmp", new, mode)
    try:
        try:
            Roster.read(staged)
        except (RosterError, config.ConfigError) as error:
            raise RosterEditRefused(
                "the rewritten roster would not pass its own reader; nothing was "
                "written"
            ) from error
        if target.read_bytes() != raw:
            raise RosterEditRefused(
                "the roster changed while it was edited; nothing was written"
            )
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        backup = _fresh_file(target, f"{target.name}.bak-{stamp}-accounts", raw, mode)
        os.replace(staged, target)
    except OSError as source:
        staged.unlink(missing_ok=True)
        raise RosterError.unwritable(path, source) from source
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    return backup


def _fresh_file(beside: Path, name: str, content: bytes, mode: int) -> Path:
    """A new file named ``name`` (numbered when taken) beside ``beside``, synced.

    A file whose creation did not complete is removed, so no partial backup or
    staging file is ever left under a name that looks whole.
    """
    for attempt in range(1, 100):
        path = beside.with_name(name if attempt == 1 else f"{name}-{attempt}")
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        except FileExistsError:
            continue
        except OSError as source:
            raise RosterError.unwritable(beside, source) from source
        try:
            # On the raw descriptor, closed before the file is used: nothing between
            # its open and its close can leak it.
            try:
                os.fchmod(descriptor, mode)
                _write_all(descriptor, content)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except BaseException as error:
            path.unlink(missing_ok=True)
            if isinstance(error, OSError):
                raise RosterError.unwritable(beside, error) from error
            raise
        return path
    raise RosterError(f"no free name beside {beside} for {name}")
