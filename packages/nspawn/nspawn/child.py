"""Codex child version contract, and the v7 detached Claude child custody.

The Codex half (``Version`` through ``app_server_arguments``) is a port of
``capabilities/nspawn/src/child.rs`` at pin ``d8e0be5ff``, kept byte-compatible
for existing consumers; it no longer describes the execution path a spawn
takes.

The Claude custody surface below it (``build_child_env`` through
``spawn_child``) has no Rust oracle: v7 replaces the guardian/pidfd tree in
``process_guard.rs`` with a plain ``Popen(start_new_session=True)`` plus the
same ``ProcessIdentity`` fencing ``process_identity.py`` already proves, and
SIGSTOP-then-SIGKILL on the process group instead of a supervised subreaper.
"""

from __future__ import annotations

import os
import re
import select
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import BinaryIO, Final, Literal, assert_never, cast

from . import claude_child, wire
from .custody import ExecutorScope
from .errors import NERR_1112, NERR_2002, NERR_2003, NERR_2006, NSpawnError
from .inference import StructuredInference
from .owner_roster import (
    DERIVED_ENV_KEYS,
    NativeAccount,
    Provider,
    claude_config_dir,
    claude_family,
    credential_source,
    expand_value_with,
    model_env_build,
)
from .wire import SANDBOX_TOOL_PATH

#: The environment key the proxy route owns.
ANTHROPIC_BASE_URL_KEY = "ANTHROPIC_BASE_URL"
#: The app sets this policy for its managed sessions and their descendants.
MEMORY_POLICY_ENV = "NOTT_MEMORY_POLICY"
#: What picks a child's endpoint or model. The route declares these, the
#: caller never does: an inherited one sends the child somewhere else.
ROUTE_ENV_NAMES = (
    ANTHROPIC_BASE_URL_KEY,
    "ANTHROPIC_SMALL_FAST_MODEL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
)
from .process_identity import (
    ProcessIdentity,
    ProcessIdentityError,
    capture_process_identity,
    group_is_only_zombies,
    process_identity_matches,
)
from .pump import OutputPump, OutputStream, RunOutputBudget
from .redact import SecretNeedles

_U64_MAX: Final = (1 << 64) - 1
APP_SERVER_CONFIG_OVERRIDES: Final[tuple[str, ...]] = (
    'web_search="disabled"',
    "tools.web_search=false",
    "notify=[]",
    "orchestrator.mcp.enabled=false",
    "orchestrator.skills.enabled=false",
    "analytics.enabled=false",
    'history.persistence="none"',
    'shell_environment_policy.inherit="none"',
)


@dataclass(frozen=True, order=True, slots=True)
class Version:
    """One semantic version whose components fit the oracle's Rust ``u64``."""

    major: int
    minor: int
    patch: int

    def __post_init__(self) -> None:
        for name, value in (
            ("major", self.major),
            ("minor", self.minor),
            ("patch", self.patch),
        ):
            if type(value) is not int or not 0 <= value <= _U64_MAX:
                raise ValueError(f"version {name} must be a u64")

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


class ChildError(NSpawnError):
    """Root of child lifecycle refusals."""


def _parse_u64(value: str) -> int | None:
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if 0 <= parsed <= _U64_MAX else None


def parse_version(text: str) -> Version | None:
    """Return the first dotted numeric token using the oracle's exact rules.

    Two components are required, a missing or malformed patch becomes zero, and
    a malformed major/minor in the first dotted token refuses the whole input.
    """
    for token in re.split(r"[^0-9.]+", text):
        if not token or "." not in token:
            continue
        # A token that survives the check above contains at least one ".",
        # so splitting on it always yields at least two parts. The oracle's
        # `parts.len() < 2` guard at this point is therefore unreachable;
        # deliberately not ported, unlike the Rust below it that IS reachable.
        parts = token.split(".")
        major = _parse_u64(parts[0])
        minor = _parse_u64(parts[1])
        if major is None or minor is None:
            return None
        patch = _parse_u64(parts[2]) if len(parts) >= 3 else None
        return Version(major=major, minor=minor, patch=patch or 0)
    return None


def app_server_arguments() -> list[str]:
    """Build the confined Codex app-server argv from the contractned contract."""
    arguments = ["app-server", "--stdio", "--strict-config"]
    for feature in wire.DISABLED_CAPABILITY_FEATURES:
        arguments.extend(("--disable", feature))
    for feature in ("code_mode", "code_mode_host"):
        arguments.extend(("--enable", feature))
    for config in APP_SERVER_CONFIG_OVERRIDES:
        arguments.extend(("-c", config))
    arguments.extend(
        (
            "-c",
            f'shell_environment_policy.set.PATH="{SANDBOX_TOOL_PATH}"',
        )
    )
    return arguments


class ChildEnvironmentError(ChildError):
    """The closed child environment or its credential could not be built."""

    def __init__(self, provider_name: str, detail: str) -> None:
        super().__init__(NERR_1112, f"provider {provider_name!r}: {detail}")


class ChildArgvError(ChildError):
    """The requested argv composition is not a legal Claude turn."""

    def __init__(self, detail: str) -> None:
        super().__init__(NERR_2002, detail)


class ReadPathIsFile(ChildArgvError):
    """A read_paths entry names a file: the caller's input, not a spawn failure.

    The cages are proven for directory grants only (`--add-dir`, the Codex
    permission profile, the sandbox read roots), so the entry stays refused,
    but the message names the cause first, not the vendor binary.
    """

    def __init__(self, path: str) -> None:
        self.entry = NERR_2002
        self.detail = f"read_paths entry is a file; grant its directory: {path}"
        Exception.__init__(self, self.detail)


class ChildSpawnError(ChildError):
    """The binary could not be executed as a detached child."""

    def __init__(self, binary: str, detail: str) -> None:
        super().__init__(NERR_2002, f"{binary}: {detail}")


class ChildPipeError(ChildError):
    """The child was spawned but a stdio pipe was never bound."""

    def __init__(self, detail: str) -> None:
        super().__init__(NERR_2003, detail)


class ChildIdentityError(ChildError):
    """The child's identity could not be captured, or no longer matches."""

    def __init__(self, detail: str) -> None:
        super().__init__(NERR_2006, detail)


def _credential_from_auth_env(
    provider: Provider, resolve_error: ValueError, ambient: Mapping[str, str]
) -> bytes:
    """Read the declared credential from the AMBIENT the caller passed, never `os.environ`.

    Its sibling `_credential_from_proxy` has always taken `ambient`, and
    `_read_credential_bytes` holds it for both. Reaching past the parameter to the process
    environment made this function's answer depend on WHO RAN IT: measured 2026-09-03, the two
    `test_child_env_denylist` cases pass for the owner (whose shell exports `ZAI_API_KEY`) and
    fail for the gate's executor, which reads as a defect and is an unnamed dependency on the
    operator's shell.
    """
    if provider.auth_env is None:
        raise ChildEnvironmentError(
            provider.name, str(resolve_error)
        ) from resolve_error
    value = ambient.get(provider.auth_env)
    if not value:
        raise ChildEnvironmentError(
            provider.name, f"auth_env {provider.auth_env!r} is unset or empty"
        ) from resolve_error
    return value.encode("utf-8")


#: Ambient names whose VALUE is a credential for something that has nothing to
#: do with this run. F-024, measured 2026-08-29: the child received 231 keys,
#: 78 matching these — AIKIDO_API_KEY, CARGO_REGISTRY_TOKEN, CLOUDFLARE_API_KEY.
#:
#: A DENYLIST AND NOT AN ALLOWLIST, on purpose. The inheritance is what LD-3
#: needs: sealing the child into an empty home stripped `~/.claude` and its
#: `permissions.defaultMode`, and the child sat waiting for an approval from
#: nobody. Enumerating what to KEEP would rediscover that ceiling one variable
#: at a time; enumerating what to DROP keeps the cure and removes the leak.
SECRET_NAME_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")


def is_secret_name(name: str) -> bool:
    """Whether an AMBIENT variable name reads as somebody's credential.

    Applied to the inherited environment only. What the ROSTER declares is
    never filtered: `ANTHROPIC_AUTH_TOKEN` matches this and is exactly what a
    proxied provider must set.
    """
    upper = name.upper()
    return any(marker in upper for marker in SECRET_NAME_MARKERS)


def _read_credential_bytes(
    provider: Provider, account_alias: str | None, ambient: Mapping[str, str]
) -> bytes:
    try:
        source = credential_source(provider, account_alias)
    except ValueError as error:
        raise ChildEnvironmentError(provider.name, str(error)) from error
    if source.kind == "env":
        return _credential_from_auth_env(
            provider, ValueError(source.unresolved or "no token file resolves"), ambient
        )
    if source.kind != "token":
        # A native OAuth home is `native.prepare`'s, never a Claude child's.
        raise ChildEnvironmentError(
            provider.name, f"a {source.kind} credential is not a Claude child's"
        )
    token_path = Path(source.location)
    try:
        return token_path.read_bytes()
    except OSError as error:
        raise ChildEnvironmentError(
            provider.name, f"credential file {token_path} could not be read: {error}"
        ) from error


def _selected_account(
    provider: Provider, account_alias: str | None
) -> NativeAccount | None:
    """The DECLARED account this run was routed to, or None.

    Read straight out of `provider.accounts` rather than through
    `resolve_native_account`, which rebuilds the record field by field: a field
    that selector forgets to copy would read here as "undeclared" instead of as
    a defect. Only the alias default is shared with it — an absent alias, or one
    that is the provider's own name, means the first declared account.
    """
    alias = account_alias
    if alias is None or alias == provider.name:
        # DECLARED, not ordered: it is the order the picker breaks ties on
        # (`picker.py:201`), and two orders for the same choice are two
        # truths about which account an absent alias means.
        alias = next(iter(provider.accounts), None)
    return None if alias is None else provider.accounts.get(alias)


def build_child_env(
    provider: Provider,
    account_alias: str | None,
    *,
    defaults_env: dict[str, str] | None = None,
    model: str | None = None,
    model_fast: str | None = None,
    effort: str | None = None,
    plugin_root: Path | None = None,
    base_environ: dict[str, str] | None = None,
    ambient: Mapping[str, str] | None = None,
    run_id: str | None = None,
    binary: str | Path | None = None,
) -> tuple[dict[str, str], SecretNeedles]:
    """Build the closed child environment and exact secret-redaction needles.

    ``base_environ`` is the caller's already-closed allowlist (never the
    ambient host environment); this function only fingerprints the selected
    account's credential and layers the provider's own declared environment
    on top, expanding ``$PLUGIN`` and ``$NAME`` references, and sets the six
    vendor model/effort variables from the caller's pick.
    """
    ambient_env = os.environ if ambient is None else ambient
    memory_policy = ambient_env.get(
        MEMORY_POLICY_ENV, (base_environ or {}).get(MEMORY_POLICY_ENV)
    )
    if memory_policy is not None and memory_policy != "push":
        raise ChildEnvironmentError(
            provider.name, "NOTT_MEMORY_POLICY must be push when set"
        )
    account = _selected_account(provider, account_alias)
    credential = _read_credential_bytes(provider, account_alias, ambient_env)
    needles = SecretNeedles.from_credential(credential)
    if needles.is_empty():
        raise ChildEnvironmentError(
            provider.name,
            "credential yields no redactable secret material; nSPAWN would be "
            "unable to keep it out of executor output",
        )
    # The operator's own environment is the base. The owner's recipe sets no
    # CLAUDE_CONFIG_DIR: the child inherits `~/.claude` and, with it, the
    # `permissions.defaultMode` that lets it actually work. Sealing that into
    # an empty per-run directory stripped exactly that, and the child ended up
    # asking approval of nobody — LD-3 died with the child answering
    # "aguardando sua aprovacao". Order: ambient, then the provider, then the
    # account credential last, so a run routed to one account can never spend
    # the operator's own token.
    env: dict[str, str] = {
        name: value
        for name, value in (os.environ if ambient is None else ambient).items()
        if not is_secret_name(name)
    }
    #: The endpoint belongs to the route, never to the caller: a session on
    #: z.ai handed its `ANTHROPIC_BASE_URL` to a claude child, which sent the
    #: account's OAuth token there and got a 401 (2026-09-23). The roster, the
    #: provider and the account set it below when the route has one.
    for name in ROUTE_ENV_NAMES:
        env.pop(name, None)
    if base_environ is not None:
        env.update(base_environ)
    # The picked ACCOUNT may carry its own endpoint and model. Both land before
    # `[provider.env]`/`[defaults.env]` so an explicit roster entry still wins,
    # which is the precedence everything else in this function already has.
    if account is not None:
        if account.base_url:
            env[ANTHROPIC_BASE_URL_KEY] = account.base_url
        model = account.model or model
    # Resolve native families before launch and keep every tier on that model.
    if model is not None:
        model_env_build(model, model_fast, binary or provider.bin, env)
    if effort is not None:
        env["CLAUDE_CODE_EFFORT_LEVEL"] = effort
    for name, raw in (defaults_env or provider.env).items():
        value = expand_value_with(raw, plugin_root, ambient_env)
        if not value and "$" in raw:
            # A REFERENCE THAT RESOLVED TO NOTHING IS AN ABSENT SETTING, never
            # an empty one. A literal empty value in the roster is still
            # honoured: only an unresolved `$NAME` drops.
            continue
        env[name] = value
    # The account is more specific than the provider, so it comes AFTER it.
    if account is not None and account.env:
        for name, raw in account.env.items():
            value = expand_value_with(raw, plugin_root, ambient_env)
            if not value and "$" in raw:
                continue
            env[name] = value
    # One window, two spellings: the roster writes the owner's spelling and this follows it,
    # clearing whatever the parent's environment brought. The client reads both and
    # letting them diverge is like declaring neither.
    for derived, source in DERIVED_ENV_KEYS.items():
        if source in env:
            env[derived] = env[source]
    if model is not None and (
        claude_family(model) is not None or model.startswith("claude-")
    ):
        model_env_build(model, model_fast, binary or provider.bin, env)

    if provider.kind == "claude" and account is not None and account.home is not None:
        env["CLAUDE_CONFIG_DIR"] = str(claude_config_dir(account, ambient_env))
    _deliver_credential(env, provider, account, credential)
    # WHO spawned this child, under a name that survives the child rebinding
    # `CLAUDE_CODE_MESSAGING_SOCKET` to its own pid. Absent when this supervisor
    # was not itself started from a Claude session: an EMPTY address still reads
    # as an address and would be sent to, so absent stays absent.
    caller = caller_address(ambient_env)
    if caller:
        env[CALLER_ADDR_ENV] = caller
    else:
        env.pop(CALLER_ADDR_ENV, None)
    if run_id is not None:
        env[RUN_ID_ENV] = run_id
    env.pop(MEMORY_POLICY_ENV, None)
    if memory_policy is not None:
        env[MEMORY_POLICY_ENV] = memory_policy
    return env, needles


#: The variable the `claude` binary reads its OAuth credential from. Measured
#: working against 2.1.250, and the form the owner's own fleet recipes use.
CLAUDE_OAUTH_TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"

#: Names a provider may already use to hand the child its credential. A proxy
#: provider carries ANTHROPIC_AUTH_TOKEN in its own `env` block; a second,
#: injected credential would give one child TWO, and which one wins would be
#: an accident of ordering.
CREDENTIAL_ENV_NAMES = frozenset(
    {CLAUDE_OAUTH_TOKEN_ENV, "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"}
)


def _deliver_credential(
    env: dict[str, str],
    provider: Provider,
    account: NativeAccount | None,
    credential: bytes,
) -> None:
    """Put the chosen account's credential where the child will read it.

    The credential used to be read ONLY to build the redaction needles and
    then dropped, so a provider with no `env` block of its own — the `claude`
    provider is exactly that — spawned a child with nothing to authenticate
    with, and the vendor answered "OAuth session expired". Observed against
    the real binary on 2026-08-28.

    The value is BUILT from the account file nSPAWN picked, never filtered
    from the ambient environment, so the sealed-home property holds: the child
    cannot inherit the operator's own credential, only the one it was routed.

    The DESTINATION is the chosen ACCOUNT's `auth_env` first, then the
    provider's. With accounts declared the roster REFUSES a provider-level
    `auth_env` beside them (owner_roster.py:757), so for a per-account route
    like the local qwen the account is the only place that can name it.

    Falling back to `provider.auth_env` when the provider declares one:
    Claude Code pointed at a non-Anthropic gateway reads the same bytes from
    `ANTHROPIC_AUTH_TOKEN`, not from the OAuth name. The needles are built from
    the credential itself, upstream of this function, so a credential delivered
    under another name is redacted exactly as before.

    """
    if CREDENTIAL_ENV_NAMES & provider.env.keys():
        # The provider declares its own credential — a proxy carries
        # ANTHROPIC_AUTH_TOKEN in its env block. Injecting a second one would
        # give TWO to the same child, and whichever wins would be an accident of order.
        #
        # A credential coming from the ENVIRONMENT does not count here: it belongs to the operator, and
        # a run routed to another account that spent it would charge the
        # wrong account. That one is always overwritten right below.
        return
    # The file ends in a newline; an env var carrying one fails auth silently.
    try:
        token = credential.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as error:
        # An environment value is text. Credential bytes that are not UTF-8
        # cannot become one, and letting UnicodeDecodeError escape would put an
        # untyped exception on the spawn path. The fuzz corpus drives this.
        raise ChildEnvironmentError(
            provider.name, "credential file is not valid UTF-8 text"
        ) from error
    if not token:
        raise ChildEnvironmentError(
            provider.name, "credential file holds no token after stripping whitespace"
        )
    destination = (account.auth_env if account is not None else None) or (
        provider.auth_env or CLAUDE_OAUTH_TOKEN_ENV
    )
    env[destination] = token


#: The closed BUILT-IN tool set a read-only child gets. `--tools` governs only
#: built-ins; the MCP permission surface is separate. `--restricted` removes
#: command-running tools, then these four name back the reads a review needs,
#: plus SendMessage for replies and ToolSearch to load deferred read-only MCP
#: schemas. Discovery grants no tool permission; READ_ONLY_MCP_TOOLS still does.
READ_ONLY_TOOLS: tuple[str, ...] = (
    "Bash",
    "Read",
    "Grep",
    "Glob",
    "SendMessage",
    "ToolSearch",
)
#: The one MCP tool authorized in the non-interactive `dontAsk` child.
READ_ONLY_MCP_TOOLS: tuple[str, ...] = (
    "mcp__plugin_nott_nmemory__nmemory_context_show",
)
#: What the cage denies by name, whatever the vendor classifies as read-only.
#: A repository's own config can make a read-only git spelling run a program
#: or read outside the roots, so the cage grants no git at all.
READ_ONLY_DENIED: tuple[str, ...] = ("Bash(git:*)",)
#: Roster args follow the cage on argv, so they may not replace its tool sets
#: or its deny.
READ_ONLY_POLICY_FLAGS: tuple[str, ...] = (
    "--tools",
    "--allowedTools",
    "--allowed-tools",
    "--permission-mode",
    "--disallowedTools",
    "--disallowed-tools",
)

#: The env var claude exports naming THIS session's own messaging socket. The
#: supervisor inherits it from the session that started it, which is exactly the
#: address a child must answer to. It is never passed DOWN under this name: the
#: child binds its own socket and would collide.
CALLER_SOCKET_ENV: Final = "CLAUDE_CODE_MESSAGING_SOCKET"

#: The marker every child of a run carries, so a hook inside it knows it is one.
RUN_ID_ENV: Final = "NOTT_NSPAWN_RUN_ID"

#: The session id a claude child was launched with (`--session-id` or `--resume`). The
#: Stop hooks leave that session's final text alone and judge any other: the run marker
#: is inherited by anything the child starts, an interactive session included.
SESSION_ID_ENV: Final = "NOTT_NSPAWN_SESSION_ID"

#: The child's address, handed to the caller so it can subscribe instead of poll.
CALLER_ADDR_ENV: Final = "NOTT_NSPAWN_CALLER_ADDR"


def caller_address(environ: Mapping[str, str] | None = None) -> str | None:
    """`uds:<this process's own session socket>`, or None outside a session.

    The address the supervisor hands DOWN to a child
    (`NOTT_NSPAWN_CALLER_ADDR`). A run's terminal notice does not read it: that
    goes to the session its `notices` row names (`notify.push_ready`). An
    EMPTY value still reads as an address and would be sent to, so absent
    stays absent.
    """
    env = os.environ if environ is None else environ
    caller = env.get(CALLER_SOCKET_ENV)
    return f"uds:{caller}" if caller else None


def child_socket_address(
    pid: int, *, environ: Mapping[str, str] | None = None
) -> str | None:
    """`uds:<sockets dir>/<pid>.sock`, or None when this process has no session.

    The sockets DIRECTORY comes from the caller's own socket rather than a
    literal path: the claude binary accepts four namespace layouts
    (`/tmp/cc-socks`, `/private/tmp/cc-socks`, `/run/user/<uid>/cc-socks`, the
    termux one) plus an explicit `--messaging-socket-path`, so hardcoding one
    would answer wrongly off this host instead of answering nothing.
    """
    env = os.environ if environ is None else environ
    caller = env.get(CALLER_SOCKET_ENV)
    if not caller:
        return None
    directory = os.path.dirname(caller)
    if not directory:
        return None
    return f"uds:{os.path.join(directory, f'{pid}.sock')}"


MAX_READ_PATHS: Final = 8


def validate_read_paths(
    read_paths: Sequence[str] | None, *, read_only: bool
) -> tuple[str, ...]:
    if isinstance(read_paths, (str, bytes)):
        raise ChildArgvError("read_paths must be a sequence of paths, not a string")
    if not read_paths:
        # NOTHING GRANTED IS NOT A MALFORMED GRANT. This function's own answer
        # for "no extra trees" is `()`, and the supervisor validates once then
        # carries that value down to `compose_child_argv`, which validates it
        # AGAIN. Refusing it here made every caged run without extra trees die
        # at spawn — `nspawn_batch_start`, caged by construction, with it.
        return ()
    if not read_only:
        raise ChildArgvError("read_paths requires read_only confinement")
    if len(read_paths) > MAX_READ_PATHS:
        raise ChildArgvError(f"read_paths must carry at most {MAX_READ_PATHS} paths")
    paths = tuple(read_paths)
    for path in paths:
        if not isinstance(path, str) or not path:
            raise ChildArgvError("each read_paths entry must be a non-empty string")
        if "\x00" in path:
            raise ChildArgvError("read_paths entries cannot contain NUL bytes")
        tree = Path(path)
        if not tree.is_absolute():
            raise ChildArgvError("read_paths entries must be absolute directory paths")
        if ".." in tree.parts:
            raise ChildArgvError("read_paths entries cannot contain parent traversal")
        if tree.is_file():
            raise ReadPathIsFile(path)
        if not tree.is_dir():
            raise ChildArgvError("read_paths entries must name existing directories")
    return paths


def _plugin_dirs_the_explicit_one_does_not_replace(
    declared_args: Sequence[str],
    plugin_dir: Path | str | None,
    read_only: bool,
) -> list[str]:
    """Drop the roster `--plugin-dir` pairs the explicit directory supersedes.

    The roster's `plugins` compose into `declared_args` as their own
    `--plugin-dir` pairs, and the supervisor ALSO hands one directory over
    explicitly. Both reached the child, and in the read-only cage that undid
    the filter: the cage's directory carries an `.mcp.json` reduced to nmemory
    with `--read-only`, while the roster's pair mounted the whole catalogue
    beside it. Measured on run f28f47db (2026-09-20) — only `--allowedTools`
    stood between that child and every server.

    Under the cage the explicit directory is the ONLY one. Outside it, the
    explicit directory is one of the declared ones, so the pair that names the
    same path is a duplicate and the rest are the owner's other plugins.
    """
    if plugin_dir is None:
        return list(declared_args)
    explicit = os.path.realpath(os.fspath(plugin_dir))
    kept: list[str] = []
    index = 0
    while index < len(declared_args):
        argument = declared_args[index]
        if argument == "--plugin-dir" and index + 1 < len(declared_args):
            candidate = declared_args[index + 1]
            if read_only or os.path.realpath(candidate) == explicit:
                index += 2
                continue
            kept.extend((argument, candidate))
            index += 2
            continue
        kept.append(argument)
        index += 1
    return kept


def compose_child_argv(
    binary: Path | str,
    *,
    session_id: str | None = None,
    resume: bool = False,
    append_system_prompt_file: Path | str | None = None,
    plugin_dir: Path | str | None = None,
    declared_args: Sequence[str] | None = None,
    read_only: bool = False,
    read_paths: Sequence[str] | None = None,
    inference_profile: StructuredInference | None = None,
) -> list[str]:
    """Compose one Claude stream-json turn without shell interpretation.

    The prompt is NOT on this argv: the caller writes it to the child's stdin,
    which ``claude -p`` reads when no prompt argument is given. Linux caps one
    argv string at 131 072 bytes (MAX_ARG_STRLEN); a larger prompt made
    ``execve`` fail with E2BIG, and every large prompt with it
    (measured 2026-09-23: 137 669 bytes failed, 114 769 passed).

    ``read_only`` is the CAGE, not a request: `--restricted` makes the vendor
    ignore the operator's settings files — the ones carrying
    `permissions.defaultMode: bypassPermissions` — refuse that mode outright,
    and confine the file tools to the working directory plus explicit
    ``read_paths``. Those paths widen reading only; they are refused without the
    cage.

    The cage grants READING, not execution. `Bash` is in the built-in set, but
    under `--permission-mode dontAsk` the vendor auto-admits only the commands
    it classifies as read-only (grep, find, wc) and denies the rest with
    nobody to ask: a caged child never runs a script, an interpreter, a clone
    or a mktemp. Git is refused by name:
    `--disallowedTools Bash(git:*)`, because a repository's own config can make
    a read-only git spelling run a program or read outside the roots. Measured
    2026-10-09 on claude 2.1.295 inside a repository: without the rule plain
    `git log`, `git status`, `cd . && git log`, `command git log` and `\\git log`
    ran; with it every git spelling tried was denied while `ls` still ran.
    Widening that would take
    a `Bash(...)` allow pattern, and `bash -c` writes as readily as it reads —
    so a review that must DRIVE a gate is not caged; it is dispatched writable
    into its own worktree.
    """
    granted_read_paths = validate_read_paths(read_paths, read_only=read_only)
    if inference_profile is not None and (
        not read_only
        or granted_read_paths
        or plugin_dir is not None
        or append_system_prompt_file is not None
    ):
        raise ChildArgvError(
            "structured inference admits no tools, plugins or extra grants"
        )
    argv: list[str] = [
        os.fspath(binary),
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--setting-sources",
        "" if inference_profile is not None else "user",
    ]
    if plugin_dir is None:
        # No sanctioned plugin channel: the child stays hermetic and the strict
        # lock closes a door nobody is going to use.
        #
        # WITH a plugin_dir the lock has to come off. `--strict-mcp-config`
        # makes the vendor ignore EVERY MCP config except one passed through
        # `--mcp-config`, and that flag is forbidden here — a `.mcp.json` names
        # commands to execute. The two together guaranteed `mcp_servers: []` by
        # construction, which is the opposite of the child seeing nMEMORY.
        #
        # Dropping it leaks nothing: measured 2026-08-28 under the sealed home,
        # the child came up with the plugin's six servers and NONE of the
        # operator's thirteen. The seal is CLAUDE_CONFIG_DIR pointing at this
        # run's own empty config directory, never this flag.
        argv.append("--strict-mcp-config")
    if inference_profile is not None:
        argv.extend(("--tools", "", "--json-schema", inference_profile.schema()))
    elif read_only:
        argv.extend(("--restricted", "--permission-mode", "dontAsk"))
        for path in granted_read_paths:
            argv.extend(("--add-dir", path))
        if plugin_dir is not None and os.fspath(plugin_dir) not in granted_read_paths:
            argv.extend(("--add-dir", os.fspath(plugin_dir)))
        argv.extend(("--tools", ",".join(READ_ONLY_TOOLS)))
        argv.extend(("--allowedTools", ",".join(READ_ONLY_MCP_TOOLS)))
        argv.extend(("--disallowedTools", ",".join(READ_ONLY_DENIED)))
    if declared_args and inference_profile is None:
        # The roster's own `args`. They come from the owner, but the flag door
        # is a property of the CHILD, not of who asked: a forbidden flag
        # declared here would widen the child's authority through the back
        # door, so it is refused by name rather than trusted by origin.
        for flag in declared_args:
            flag_name = flag.partition("=")[0]
            if flag_name in claude_child.FORBIDDEN_CLAUDE_FLAGS or (
                read_only and flag_name in READ_ONLY_POLICY_FLAGS
            ):
                raise ChildEnvironmentError(
                    "roster", f"declared args carry the forbidden flag {flag!r}"
                )
        argv.extend(
            _plugin_dirs_the_explicit_one_does_not_replace(
                declared_args, plugin_dir, read_only
            )
        )
    if append_system_prompt_file is not None:
        argv.extend(
            ("--append-system-prompt-file", os.fspath(append_system_prompt_file))
        )
    if plugin_dir is not None:
        argv.extend(("--plugin-dir", os.fspath(plugin_dir)))
    if resume:
        if session_id is None:
            raise ChildArgvError("resume=True requires a session_id to resume")
        argv.append(f"--resume={session_id}")
    elif session_id is not None:
        argv.append(f"--session-id={session_id}")
    return argv


#: The variable the plugin's law-session.sh reads: `off` silences its copy of the law.
LAW_SESSION_ENV: Final = "NOTT_LAW_SESSION"
#: The first line of the NOTT law (plugin/NOTT.md).
LAW_TITLE: Final = b"# **NOTT** System Prompt"


def appends_law(declared_args: Sequence[str], cwd: Path | str) -> bool:
    """Whether the roster's `declared_args` append a file that IS the NOTT law.

    The plugin's SessionStart hook prints the whole law as well (~15 KB since
    2026-09-26); a child whose system prompt carries it is told to skip that copy.
    Only the roster's own file is read, and it is the law when it begins with the
    law's first line: a dispatch's text is combined in front of it, so a dispatch
    that quotes the title must never silence the only copy its child would get.
    """
    return any(
        flag == "--append-system-prompt-file"
        and (prompt := Path(cwd, path)).is_file()
        and prompt.read_bytes().startswith(LAW_TITLE)
        for flag, path in pairwise(declared_args)
    )


class SpawnedChild:
    """A child held by its captured identity, session and two output pumps."""

    def __init__(
        self,
        process: subprocess.Popen[bytes],
        identity: ProcessIdentity,
        stdout_pump: OutputPump,
        stderr_pump: OutputPump,
        run_dir: Path,
    ) -> None:
        if process.pid != identity.pid:
            raise ChildIdentityError(
                f"captured identity pid {identity.pid} does not match the "
                f"spawned process pid {process.pid}"
            )
        self._process = process
        self._identity = identity
        self._custody_lock = threading.Lock()
        # Held by a group signaler for its whole life: the leader is never
        # reaped, and its group id never freed, while a signal is still owed.
        self._reap_fence = threading.Lock()
        self.stdout_pump = stdout_pump
        self.stderr_pump = stderr_pump
        self.run_dir = run_dir

    @property
    def identity(self) -> ProcessIdentity:
        return self._identity

    def poll(self) -> int | None:
        """Observe exit without freeing the leader PID before group cleanup."""
        with self._custody_lock:
            if self._process.returncode is not None:
                return self._process.returncode
            try:
                exited = os.waitid(
                    os.P_PID, self._identity.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT
                )
            except ChildProcessError as error:
                # A stale signaler's Popen.wait may have reaped it without
                # storing the code yet; it stores it while holding
                # _waitpid_lock (CPython's private Popen lock; 3.14, pinned in
                # .python-version). Wait only while a reaper holds it, and run
                # no waitpid of our own. Past the bound, or with no code once
                # the lock is free, the bounded refusal; a later poll returns
                # a code stored afterwards.
                waitpid_lock = cast(
                    threading.Lock,
                    self._process._waitpid_lock,  # type: ignore[attr-defined]
                )
                if waitpid_lock.acquire(timeout=SIGNALER_BOUND_SECONDS):
                    waitpid_lock.release()
                if self._process.returncode is not None:
                    return self._process.returncode
                raise ChildIdentityError(
                    f"process {self._identity.pid} exit custody could not be observed"
                ) from error
            except OSError as error:
                raise ChildIdentityError(
                    f"process {self._identity.pid} exit custody could not be observed"
                ) from error
            if exited is None:
                return None
            # WNOWAIT leaves a zombie leader pinning both its PID and group.
            # Signal its verified group before Popen.wait releases that identity.
            # The signaler reaps too, under its fence: this thread never holds
            # the fence, so no interrupt can leave it held, and the reap waits
            # for a stale signaler no longer than a group kill may take.
            self._stop_owned_group(reap=True)
            return self._process.returncode

    def write_stdin(self, payload: bytes, timeout: float | None) -> None:
        """Send a bounded frame without blocking cancellation behind a full pipe."""
        stream = self._process.stdin
        if stream is None or stream.closed:
            raise ChildPipeError("this child has no writable stdin")
        fd = stream.fileno()
        deadline = None if timeout is None else time.monotonic() + timeout
        pending = memoryview(payload)
        while pending:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise ChildPipeError("child stdin write deadline exceeded")
            try:
                if not select.select([], [fd], [], remaining)[1]:
                    raise ChildPipeError("child stdin write deadline exceeded")
                count = os.write(fd, pending[:65536])
            except BlockingIOError:
                continue
            except OSError as error:
                raise ChildPipeError(
                    "child stdin could not accept the frame"
                ) from error
            pending = pending[count:]

    def close_stdin(self) -> None:
        """Signal EOF: a native protocol child ends, a Claude child starts its turn."""
        stream = self._process.stdin
        if stream is not None and not stream.closed:
            stream.close()

    def wait(self, timeout: float | None = None) -> int:
        deadline = None if timeout is None else time.monotonic() + timeout
        while (exit_code := self.poll()) is None:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise subprocess.TimeoutExpired(
                    str(self._identity.pid), cast(float, timeout)
                )
            time.sleep(0.01 if remaining is None else min(0.01, remaining))
        try:
            self.stdout_pump.join(timeout=timeout)
        finally:
            self.stderr_pump.join(timeout=timeout)
        return exit_code

    def stop_then_kill(self, expected: ProcessIdentity) -> None:
        """Verify this child's identity, then stop and kill its owned group."""
        with self._custody_lock:
            if expected != self._identity:
                raise ChildIdentityError(
                    "requested identity does not belong to this child"
                )
            self._stop_owned_group()

    def _stop_owned_group(self, *, reap: bool = False) -> None:
        """The custody lock pins observation, signaling and reaping together.

        The identity recheck, the stop-kill pair and, with `reap`, the reap run
        on their own thread under the reap fence. Python raises a signal
        handler's exception only on the main thread, so an interrupt never
        splits SIGSTOP from SIGKILL, and the leader is never reaped while a
        signal is owed. Once the signaler has started, the caller waits for it
        at most `SIGNALER_BOUND_SECONDS`, the reap included; a start that
        raises waits at most `SIGNALER_START_SECONDS` for the thread to enter.
        Past either, `ChildIdentityError`, with the kill not guaranteed sent.
        Interrupts are re-raised on a best-effort basis:
        Python cannot mask every bytecode, so one may escape unrecorded.
        """
        entered = threading.Event()
        done = threading.Event()
        raised: list[BaseException] = []

        def stop_then_kill() -> None:
            entered.set()
            try:
                with self._reap_fence:
                    if reap and self._process.returncode is not None:
                        return  # a stale signaler reaped it: nothing is owed
                    self._recheck_identity()
                    try:
                        self._signal_owned_group(signal.SIGSTOP)
                    finally:
                        self._signal_owned_group(signal.SIGKILL)
                    if reap:
                        self._process.wait(timeout=0)
            except BaseException as error:  # noqa: BLE001 - carried to the caller, raised there
                raised.append(error)
            finally:
                done.set()

        signaler = threading.Thread(target=stop_then_kill, name="nspawn-group-kill")
        interrupts: list[BaseException] = []
        start_error: BaseException | None = None
        state: Literal[
            "start", "starting", "launching", "running", "joining", "over"
        ] = "start"
        # Bound before any state is entered, and every new deadline set before
        # its state: an interrupt between the two never leaves a state without
        # the deadline that ends it.
        deadline = time.monotonic() + SIGNALER_BOUND_SECONDS
        # Interrupts landing in a step below are recorded and re-raised once.
        while state != "over":
            try:
                if state == "start":
                    state = "starting"
                    signaler.start()
                    deadline = time.monotonic() + SIGNALER_BOUND_SECONDS
                    state = "running"
                elif state == "starting":
                    # start() raised: a thread it failed to create, or an
                    # interrupt with the signaler already launched.
                    deadline = time.monotonic() + SIGNALER_START_SECONDS
                    state = "launching"
                elif state == "launching":
                    if entered.is_set():
                        deadline = time.monotonic() + SIGNALER_BOUND_SECONDS
                        state = "running"
                    elif (remaining := deadline - time.monotonic()) <= 0:
                        state = "over"
                    else:
                        entered.wait(remaining)
                elif state == "running":
                    if done.is_set():
                        state = "joining"
                    elif (remaining := deadline - time.monotonic()) <= 0:
                        state = "over"
                    else:
                        done.wait(remaining)
                elif state == "joining":
                    if not signaler.is_alive():
                        state = "over"
                    elif (remaining := deadline - time.monotonic()) <= 0:
                        state = "over"  # its signals are sent; only its exit is late
                    else:
                        signaler.join(remaining)
                else:
                    assert_never(state)
            except BaseException as error:  # noqa: BLE001 - recorded, raised once below
                if state == "starting" and start_error is None:
                    start_error = error
                else:
                    interrupts.append(error)
        pid = self._identity.pid
        if start_error is not None and not entered.is_set():
            # No signaler ran, so nothing was signaled; one that starts later
            # holds the fence for its own signals.
            refusal = ChildIdentityError(
                f"process group {pid} could not be signaled: no signaler thread "
                f"started: {start_error!r}"
            )
            _note_interrupts(refusal, interrupts)
            raise refusal from start_error
        if start_error is not None:
            interrupts.insert(0, start_error)
        cause: BaseException | None = raised[0] if raised else None
        if not done.is_set():
            cause = ChildIdentityError(
                f"process group {pid}: the signaler did not finish within "
                f"{SIGNALER_BOUND_SECONDS:g} s"
            )
        if interrupts:
            # A refusal the signaler met stays readable behind the interrupt.
            first = interrupts[0]
            _note_interrupts(first, interrupts[1:])
            raise first from cause
        if cause is not None:
            raise cause

    def _recheck_identity(self) -> None:
        expected = self._identity
        try:
            still_live = process_identity_matches(expected)
        except ProcessIdentityError as error:
            raise ChildIdentityError(
                f"process {expected.pid} identity could not be rechecked: {error}"
            ) from error
        if not still_live:
            raise ChildIdentityError(
                f"process {expected.pid} no longer matches its captured identity; "
                "refusing to signal a group that may now belong to another process"
            )

    def _signal_owned_group(self, sig: signal.Signals) -> None:
        # macOS refuses to signal a group whose members are all unreaped
        # zombies (EPERM): nothing is left alive there, so that is not a
        # refusal. Any live member this may not signal stays one.
        expected = self._identity
        try:
            os.killpg(expected.pid, sig)
        except ProcessLookupError as error:
            raise ChildIdentityError(
                f"process group {expected.pid} vanished between identity "
                f"verification and signal: {error}"
            ) from error
        except PermissionError as error:
            if not group_is_only_zombies(expected.pid):
                raise ChildIdentityError(
                    f"process group {expected.pid} could not be signaled: {error}"
                ) from error
        except OSError as error:
            raise ChildIdentityError(
                f"process group {expected.pid} could not be signaled: {error}"
            ) from error


#: How long a group kill waits for its signaler, and for it to start at all
#: when `Thread.start()` itself was interrupted or failed.
SIGNALER_BOUND_SECONDS: Final = 10.0
SIGNALER_START_SECONDS: Final = 1.0


def _note_interrupts(error: BaseException, later: list[BaseException]) -> None:
    for interrupt in later:
        error.add_note(f"interrupted again while the group was killed: {interrupt!r}")


def spawn_child(
    argv: Sequence[str],
    cwd: Path | str,
    env: dict[str, str],
    run_dir: Path,
    secret_needles: SecretNeedles,
    *,
    stdin_pipe: bool = False,
    stdout_transform: Callable[[bytes], bytes] | None = None,
    stderr_transform: Callable[[bytes], bytes] | None = None,
    scope: ExecutorScope | None = None,
    pass_fds: tuple[int, ...] = (),
) -> SpawnedChild:
    """Spawn with ``Popen(start_new_session=True)`` and capture identity."""
    if not argv:
        raise ChildSpawnError("<empty argv>", "argv must name at least the binary")
    try:
        process = subprocess.Popen(
            list(argv) if scope is None else scope.command(list(argv)),
            cwd=os.fspath(cwd),
            env=dict(env) if scope is None else scope.environment(env),
            stdin=subprocess.PIPE if stdin_pipe else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            close_fds=True,
            pass_fds=pass_fds,
        )
    except OSError as error:
        raise ChildSpawnError(str(argv[0]), str(error)) from error
    try:
        if process.stdout is None or process.stderr is None:
            raise ChildPipeError(f"process {process.pid} did not bind both stdio pipes")
        if process.stdin is not None:
            os.set_blocking(process.stdin.fileno(), False)
        try:
            identity = capture_process_identity(process.pid)
        except ProcessIdentityError as error:
            raise ChildIdentityError(
                f"process {process.pid} identity could not be captured: {error}"
            ) from error
        budget = RunOutputBudget()
        stdout_pump = OutputPump(
            cast(BinaryIO, process.stdout),
            run_dir,
            OutputStream.STDOUT,
            secret_needles,
            budget,
            transform_line=stdout_transform,
        ).start()
        stderr_pump = OutputPump(
            cast(BinaryIO, process.stderr),
            run_dir,
            OutputStream.STDERR,
            secret_needles,
            budget,
            transform_line=stderr_transform,
        ).start()
        return SpawnedChild(process, identity, stdout_pump, stderr_pump, run_dir)
    except NSpawnError:
        process.kill()
        process.wait()
        raise
