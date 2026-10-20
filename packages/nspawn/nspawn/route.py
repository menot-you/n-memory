"""The route layer: how one vendor starts on one account of the owner's roster.

A node records its selected account as `<alias>@<where>`. The owning run
primitive composes that account here and owns its actual native process:
claude-agent-acp on Claude/Qwen, codex-acp in its native account home, or the
AGY ACP adapter. Route checks and profile preparation have no launch effect.
The host reads no account credential; each AGY model child enters its enforcing
outer cage at the existing `native.prepare_native` / `AgyTurn` boundary.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import sys
from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .admission import (
    AdmissionRefusal,
    LaunchReceiptV1,
    PrelaunchProfileV1,
    document_digest,
)
from .agy_acp import ACCOUNT_ENV as AGY_ACCOUNT_ENV
from .app_server import AppServerError, _account_error
from .child import CLAUDE_OAUTH_TOKEN_ENV, ROUTE_ENV_NAMES
from .config import ConfigError
from .errors import NERR_1103, NERR_1116, NSpawnError
from .native import NativePreparationError
from .native import _path as native_path
from .owner_roster import (
    ANTHROPIC_BASE_URL_KEY,
    Provider,
    Roster,
    RosterError,
    claude_config_dir,
)
from .picker import (
    AllProvidersWalled,
    PickerError,
    _select_account,
    exclusion_check,
    key_refusal,
    keyless,
)
from .registry import STORE_ERRORS, Registry, RegistryError
from .rows import AccountWallRow
from .terminal import AccountWallRecord, WallSource, subscription_limit

Route = tuple[str, dict[str, str], list[str]]
MAX_PROFILE_BYTES = 4 * 1024 * 1024
MAX_IDENTITY_BYTES = 512 * 1024 * 1024


def _profile_directory(descriptor: int) -> None:
    info = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o700
        or info.st_uid != os.geteuid()
    ):
        raise AdmissionRefusal("profile grant directory is not private and owned")


def profile_read(path: Path, digest: str, *, grant_root: Path) -> PrelaunchProfileV1:
    """Read a private staged profile through a no-follow directory walk.

    The owning activation supplies its existing grant root. This function does
    not infer a grant from a caller-selected profile directory.
    """
    if not path.is_absolute() or not grant_root.is_absolute():
        raise AdmissionRefusal("profile and grant root must be absolute")
    if grant_root.resolve() != grant_root:
        raise AdmissionRefusal("profile grant root is linked or noncanonical")
    try:
        components = path.relative_to(grant_root).parts
    except ValueError as error:
        raise AdmissionRefusal(
            "profile is outside the owning activation grant"
        ) from error
    if not components or any(part in {".", ".."} for part in components):
        raise AdmissionRefusal("profile path is not a closed grant-relative path")
    directory = descriptor = None
    try:
        directory = os.open(grant_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        _profile_directory(directory)
        for component in components[:-1]:
            child = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=directory,
            )
            os.close(directory)
            directory = child
            _profile_directory(directory)
        descriptor = os.open(
            components[-1],
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=directory,
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or before.st_size > MAX_PROFILE_BYTES
        ):
            raise AdmissionRefusal(
                "profile must be a bounded private owned regular file"
            )
        chunks: list[bytes] = []
        total = 0
        while chunk := os.read(descriptor, min(65536, MAX_PROFILE_BYTES + 1 - total)):
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_PROFILE_BYTES:
                raise AdmissionRefusal("profile exceeds the private transport bound")
        after = os.fstat(descriptor)
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise AdmissionRefusal("profile changed while being verified")
        prepared = PrelaunchProfileV1.from_bytes(b"".join(chunks))
        if prepared.digest != digest:
            raise AdmissionRefusal("staged profile digest differs")
        return prepared
    except OSError as error:
        raise AdmissionRefusal("private profile input is unavailable") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory is not None:
            os.close(directory)


@contextmanager
def profile_seal(profile: PrelaunchProfileV1) -> Iterator[int]:
    """Hand the consumer an immutable read-only descriptor of the verified bytes."""
    with descriptor_seal(profile.encoded) as descriptor:
        yield descriptor


@contextmanager
def descriptor_seal(encoded: bytes) -> Iterator[int]:
    """Seal launch bytes, including bubblewrap's bounded argument stream."""
    if sys.platform != "linux" or not hasattr(os, "memfd_create"):
        raise AdmissionRefusal("sealed private profile descriptors require Linux")
    import fcntl

    writable = readonly = None
    try:
        try:
            writable = os.memfd_create(
                "nott-prelaunch", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING
            )
            view = memoryview(encoded)
            while view:
                written = os.write(writable, view)
                if written == 0:
                    raise AdmissionRefusal(
                        "private profile descriptor write made no progress"
                    )
                view = view[written:]
            fcntl.fcntl(
                writable,
                fcntl.F_ADD_SEALS,
                fcntl.F_SEAL_WRITE
                | fcntl.F_SEAL_GROW
                | fcntl.F_SEAL_SHRINK
                | fcntl.F_SEAL_SEAL,
            )
            readonly = os.open(f"/proc/self/fd/{writable}", os.O_RDONLY)
            os.set_inheritable(readonly, True)
        except OSError as error:
            raise AdmissionRefusal(
                "private profile descriptor could not be sealed"
            ) from error
        yield readonly
    finally:
        if readonly is not None:
            os.close(readonly)
        if writable is not None:
            os.close(writable)


@contextmanager
def identity_seal(path: Path, digest: str) -> Iterator[int]:
    """Verify explicit configuration and pin its read-only projection bytes.

    Neither path replacement nor a later write can change configuration bytes
    consumed after preparation. Selected mode is preserved, without owner write
    or privilege bits.
    """
    if not path.is_absolute() or path.resolve() != path:
        raise AdmissionRefusal("selected file identity is linked or noncanonical")
    import fcntl

    source = writable = readonly = None
    try:
        try:
            source = os.open(
                path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
            )
            before = os.fstat(source)
            if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_IDENTITY_BYTES:
                raise AdmissionRefusal("selected file is not a bounded regular input")
            writable = os.memfd_create(
                "nott-launch-input", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING
            )
            observed = hashlib.sha256()
            total = 0
            while chunk := os.read(source, 65536):
                total += len(chunk)
                if total > MAX_IDENTITY_BYTES:
                    raise AdmissionRefusal("selected file exceeds its launch bound")
                observed.update(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(writable, view)
                    if written == 0:
                        raise AdmissionRefusal("selected file pin made no progress")
                    view = view[written:]
            after = os.fstat(source)
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ) or "sha256:" + observed.hexdigest() != digest:
                raise AdmissionRefusal("selected file content identity differs")
            os.fchmod(writable, 0o555 if before.st_mode & 0o111 else 0o444)
            fcntl.fcntl(
                writable,
                fcntl.F_ADD_SEALS,
                fcntl.F_SEAL_WRITE
                | fcntl.F_SEAL_GROW
                | fcntl.F_SEAL_SHRINK
                | fcntl.F_SEAL_SEAL,
            )
            readonly = os.open(f"/proc/self/fd/{writable}", os.O_RDONLY)
            os.set_inheritable(readonly, True)
        except OSError as error:
            raise AdmissionRefusal("selected launch input is unavailable") from error
        yield readonly
    finally:
        for descriptor in (readonly, writable, source):
            if descriptor is not None:
                os.close(descriptor)


@dataclass(frozen=True, slots=True)
class PreparedProfile:
    """One passive check/exec preparation, retaining exactly verified inputs."""

    receipt: LaunchReceiptV1
    argv: tuple[str, ...]
    env: dict[str, str]
    descriptors: tuple[int, ...]
    cwd: str
    proxy: Path | None
    destinations: tuple[str, ...]
    stage: Path | None = None


def profile_stage(profile: PrelaunchProfileV1, grant_root: Path) -> Path:
    """The exact configuration projection owned by this profile and activation."""
    identity = hashlib.sha256(os.fsencode(grant_root) + b"\0" + profile.encoded)
    return Path("/var/tmp") / ("nott-runtime-" + identity.hexdigest())


def profile_cleanup(profile: PrelaunchProfileV1, grant_root: Path) -> None:
    """Remove this exact stage only after its owning cage/group has quiesced."""
    stage = profile_stage(profile, grant_root)
    try:
        info = stage.lstat()
    except FileNotFoundError:
        return
    if (
        stage.resolve() != stage
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise AdmissionRefusal("runtime stage is not private and owned")
    # rmtree never follows links. Restore only owned directory write permissions,
    # so immutable staged files can be unlinked without touching their sources.
    for directory, _, _ in os.walk(stage, followlinks=False):
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if os.fstat(descriptor).st_uid != os.geteuid():
                raise AdmissionRefusal("runtime stage directory ownership differs")
            os.fchmod(descriptor, 0o700)
        finally:
            os.close(descriptor)
    shutil.rmtree(stage)


@contextmanager
def _profile_configuration(
    profile: PrelaunchProfileV1,
    grant_root: Path,
    pinned: Mapping[str, int],
) -> Iterator[dict[str, Path]]:
    data = profile.data()
    stage = profile_stage(profile, grant_root)
    exposed = [
        path
        for kind in ("read", "write", "administrative")
        for path in data["scope"][kind]
    ]
    if any(
        stage.is_relative_to(path) or Path(path).is_relative_to(stage)
        for path in exposed
    ):
        raise AdmissionRefusal("runtime stage has an admitted filesystem alias")
    try:
        stage.mkdir(mode=0o700)
    except FileExistsError as error:
        raise AdmissionRefusal(
            "runtime stage already has an uncertain owner"
        ) from error
    try:
        configuration = tuple(
            dict.fromkeys(item["path"] for item in data["configuration"])
        )
        if configuration:
            source = stage / "configuration"
            source.mkdir(mode=0o700)
            for index, location in enumerate(configuration):
                _profile_snapshot(source / str(index), pinned[location])
            source.chmod(0o555)
        yield {
            location: stage / "configuration" / str(index)
            for index, location in enumerate(configuration)
        }
    finally:
        profile_cleanup(profile, grant_root)


def _profile_snapshot(target: Path, descriptor: int) -> None:
    """Copy only immutable captured bytes into the owning private stage."""
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with target.open("xb") as stream:
        offset = 0
        while chunk := os.pread(descriptor, 65536, offset):
            stream.write(chunk)
            offset += len(chunk)
    target.chmod(stat.S_IMODE(os.fstat(descriptor).st_mode))


def profile_verify(prepared: PreparedProfile) -> None:
    """The final input check immediately before the owning process effect."""
    from .claude_restricted import configuration_verify, runtime_inventory_verify

    data = prepared.receipt.data()["composition"]
    runtime_inventory_verify(data, captured_configuration=prepared.stage is not None)
    if prepared.stage is not None:
        selected = tuple(dict.fromkeys(item["path"] for item in data["configuration"]))
        pinned = dict(zip(selected, prepared.descriptors[: len(selected)], strict=True))
        configuration_verify(
            data,
            {
                location: prepared.stage / "configuration" / str(index)
                for index, location in enumerate(selected)
            },
            pinned,
        )


@contextmanager
def profile_prepare(
    provider: str,
    recorded: str | None,
    *,
    path: Path,
    digest: str,
    grant_root: Path,
    cwd: str | None = None,
) -> Iterator[PreparedProfile]:
    """The sole PRELAUNCH preparation shared by passive check and execution."""
    from .claude_restricted import compose_node_launch, runtime_inventory_verify

    if provider != "claude" or recorded is None:
        raise AdmissionRefusal("PRELAUNCH requires a selected Claude account")
    profile = profile_read(path, digest, grant_root=grant_root)
    data = profile.data()
    if data["route_ref"] != recorded or cwd is not None and cwd != data["cwd"]:
        raise AdmissionRefusal("selected route or working directory differs")
    found, environment, _ = route_start(read_roster(), provider, recorded)
    if (
        found != recorded
        or environment.get(ANTHROPIC_BASE_URL_KEY)
        or not environment.get(CLAUDE_OAUTH_TOKEN_ENV)
    ):
        raise AdmissionRefusal("PRELAUNCH requires native subscription authentication")
    native = environment.get("CLAUDE_CODE_EXECUTABLE")
    if native != data["runtime"]["native"]["path"] and (
        sys.platform == "darwin" or native is not None
    ):
        raise AdmissionRefusal("selected native runtime differs from its account route")
    if Path(data["runtime"]["consumer"]["path"]).suffix != ".mjs":
        raise AdmissionRefusal("the selected consumer is not an ECMAScript module")
    schema = Path(
        str(files("nspawn").joinpath("schema/admission.v1.schema.json"))
    ).resolve()
    runtime_inventory_verify(data)
    identities = [
        {"path": item["path"], "digest": item["digest"]}
        for item in data["configuration"]
    ]
    with ExitStack() as stack:
        pinned: dict[str, int] = {}
        seen: dict[str, str] = {}
        for identity in identities:
            location, content_digest = identity["path"], identity["digest"]
            if location in seen:
                if seen[location] != content_digest:
                    raise AdmissionRefusal(
                        "selected file has incompatible identity bindings"
                    )
                continue
            seen[location] = content_digest
            pinned[location] = stack.enter_context(
                identity_seal(Path(location), content_digest)
            )
        descriptor = stack.enter_context(profile_seal(profile))
        composition_arguments: dict[str, Any] = {
            "consumer_argv": (
                data["runtime"]["node"]["path"],
                data["runtime"]["consumer"]["path"],
                "--profile-fd",
                str(descriptor),
                "--schema",
                str(schema),
            ),
            "host_auth": {CLAUDE_OAUTH_TOKEN_ENV: environment[CLAUDE_OAUTH_TOKEN_ENV]},
            "schema": schema,
            "grant_root": grant_root,
            "descriptor": descriptor,
            "pinned": pinned,
            "producer_auth": {
                name: os.environ.get(name, "")
                for producer in [*data["mcp"], *data["producer"]]
                for name in producer["inputs"].get("credential_environment", [])
            },
        }
        stage = None
        if data["configuration"]:
            configuration = stack.enter_context(
                _profile_configuration(profile, grant_root, pinned)
            )
            stage = profile_stage(profile, grant_root)
            launch = compose_node_launch(
                profile,
                **composition_arguments,
                configuration=configuration,
            )
        else:
            launch = compose_node_launch(profile, **composition_arguments)
        if launch.argv.index("--") > 9000:
            raise AdmissionRefusal("runtime roots exceed the native argument bound")
        composition = {
            name: data[name]
            for name in (
                "scope",
                "runtime",
                "configuration",
                "project",
                "mcp",
                "producer",
                "policy",
            )
        }
        receipt = LaunchReceiptV1(
            {
                "version": 1,
                "activation_id": data["activation_id"],
                "route_ref": found,
                "profile_digest": profile.digest,
                "runtime_digest": document_digest(data["runtime"]),
                "composition": composition,
            }
        )
        # Complete runtime manifests can exceed execve's total argv limit.
        # Bubblewrap owns this documented NUL-delimited argument descriptor.
        separator = launch.argv.index("--")
        arguments = stack.enter_context(
            descriptor_seal(
                b"\0".join(
                    os.fsencode(argument) for argument in launch.argv[1:separator]
                )
                + b"\0"
            )
        )
        prepared = PreparedProfile(
            receipt,
            (launch.argv[0], "--args", str(arguments), *launch.argv[separator:]),
            launch.env,
            (*pinned.values(), descriptor, arguments),
            data["cwd"],
            launch.proxy,
            tuple(data["scope"]["network"]),
            stage,
        )
        profile_verify(prepared)
        yield prepared


class RouteRefused(NSpawnError):
    """A route this roster cannot start: the message names no host path or token."""

    def __init__(self, message: str) -> None:
        self.message = message
        self.entry = NERR_1116
        Exception.__init__(self, message)


def read_roster() -> Roster:
    """The roster a node runs on: `NOTT_NSPAWN_ROSTER`, else `~/.nott/spawn.toml`.

    A roster the loader refuses is refused without its path, which names this host.
    """
    path = os.environ.get("NOTT_NSPAWN_ROSTER") or str(
        Path.home() / ".nott" / "spawn.toml"
    )
    try:
        return Roster.read(Path(path))
    except (RosterError, ConfigError) as error:
        raise RosterUnreadable("the provider roster could not be read") from error


class RosterUnreadable(NSpawnError):
    """The roster could not be read; the message names no host path."""

    def __init__(self, message: str) -> None:
        self.message = message
        self.entry = NERR_1103
        Exception.__init__(self, message)


#: What a claude node's adapter must not inherit: a credential that would win over
#: the account's own, or an endpoint or backend the account's token would reach.
CLAUDE_CLEARED = (
    CLAUDE_OAUTH_TOKEN_ENV,
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    *ROUTE_ENV_NAMES,
)

#: Route names a qwen child must never inherit from the service's environment:
#: the endpoint and credential belong to the roster's account alone.
QWEN_CLEARED = (
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
)


def account_route(alias: str, where: object) -> str:
    """`<alias>@<where>`: the account a node records in its route. The alias is
    percent-encoded, so two accounts never spell the same route and none reads as
    a bare home; an ordinary alias reads as it always did."""
    return f"{quote(alias, safe='')}@{where}"


#: How a new node's account is picked: the provider in, the alias out (`route show`
#: gives nSPAWN's own picker, on its walls, load, quota windows and last runs).
Pick = Callable[[Provider], str | None]


def route_account(
    provider: Provider,
    recorded: str | None,
    route: Callable[[str], str],
    pick: Pick | None = None,
    *,
    leaving: bool = False,
) -> str:
    """The account a node runs on: when it is made, the one `pick` chooses (the
    shared picker with no registry data without one), and the one its recorded route
    names ever after, so a later default rebinds nobody. A recorded account the
    roster now excludes is refused, unless the node is `leaving` it: a wall or a
    switch must still record on it, so the host can move the node off it."""
    order = provider.ordered_accounts()
    if not order:
        raise RouteRefused(f"{provider.name} roster must declare a native account")
    if recorded is None:
        if pick is None:
            try:
                # The node's model is chosen later: `keyless` keeps it off an API key.
                picked = _select_account(keyless(provider, None), None, {}, {}, 0)
            except (AllProvidersWalled, PickerError) as error:
                raise RouteRefused(error.message) from error
        else:
            picked = pick(provider)
        if picked not in order:
            raise RouteRefused(
                f"the {provider.name} roster declares no account {picked}"
            )
        return str(picked)
    found = next((alias for alias in order if route(alias) == recorded), None)
    if found is None:
        if provider.name == "claude":
            alias = next(
                (
                    name
                    for name in order
                    if recorded.startswith(f"{quote(name, safe='')}@")
                ),
                None,
            )
            if alias is not None:
                raise RouteRefused(
                    f"Claude config directory changed for account {alias}"
                )
        raise RouteRefused(
            f"the {provider.name} account this node was made on is no longer in the "
            "roster"
        )
    if found in provider.excluded_accounts and not leaving:
        raise RouteRefused(
            f"the {provider.name} account this node was made on is excluded in the "
            "roster"
        )
    # A node switches model at any time, so it never starts on the key; a wall
    # or a switch still records on it, so the host can move the node off it.
    if not leaving and (refusal := key_refusal(provider, found, None)):
        raise RouteRefused(f"{refusal}; move this node to another account")
    return str(found)


def roster_provider(
    roster: Roster, label: str, match: Callable[[Provider], bool]
) -> Provider:
    """The roster's provider a ``label`` node runs on."""
    found = next((p for p in roster.providers if match(p)), None)
    if found is None:
        raise RouteRefused(f"{label} roster must declare a native account")
    return found


def account_token(provider: Provider, alias: str, label: str, unreadable: str) -> str:
    """The one line `alias`'s token file holds; refused as `label`, never by path."""
    try:
        token = provider.resolve_token_file(alias).read_text().strip()
    except (OSError, ValueError) as error:
        raise RouteRefused(unreadable) from error
    if not token or "\n" in token:
        raise RouteRefused(f"{label} token file holds no single token")
    return token


def route_list(
    roster: Roster,
    registry: Registry,
    *,
    model: str,
    effort: str,
    now: int,
    excluded: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Configured subscription routes in declared order, without quota inference.

    Credential equality excludes obvious aliases; distinct token strings are
    never claimed as proof that two actual vendor accounts are distinct.
    """
    provider = roster_provider(
        roster, "claude", lambda item: item.name == "claude" and item.kind == "claude"
    )
    if not claude_config_dir().is_absolute():
        raise RouteRefused("native account home must be absolute")
    walls = {
        wall.account_alias: wall
        for wall in registry.get_enforced_account_wall_rows(now)
    }
    tokens: set[str] = set()
    routes: list[dict[str, Any]] = []
    accounts = provider.ordered_accounts()
    try:
        exclusion_check(provider, accounts)
    except PickerError as error:
        raise RouteRefused(error.message) from error
    for alias in accounts:
        account = provider.accounts.get(alias)
        recorded = account_route(alias, claude_config_dir(account))
        reasons: list[str] = []
        if alias in provider.excluded_accounts:
            reasons.append("excluded in the roster")
        auth = (
            (account.auth_env if account is not None else None)
            or provider.auth_env
            or CLAUDE_OAUTH_TOKEN_ENV
        )
        if auth != CLAUDE_OAUTH_TOKEN_ENV or account is not None and account.base_url:
            reasons.append("not a native subscription account")
        elif (
            account is not None and account.model is not None and account.model != model
        ):
            reasons.append("account model is incompatible")
        if provider.models and model not in provider.models:
            reasons.append("model is incompatible")
        if provider.efforts and effort not in provider.efforts:
            reasons.append("effort is incompatible")
        if recorded in excluded:
            reasons.append("already attempted")
        wall = walls.get(alias)
        if wall is not None:
            reasons.append("account is cooling")
        if auth == CLAUDE_OAUTH_TOKEN_ENV and not (
            account is not None and account.base_url
        ):
            try:
                token = account_token(
                    provider,
                    alias,
                    "subscription account",
                    "subscription credential is unavailable",
                )
            except RouteRefused:
                reasons.append("subscription credential is unavailable")
            else:
                if token in tokens:
                    reasons.append("duplicate configured credential")
                tokens.add(token)
        routes.append(
            {
                "alias": alias,
                "route": recorded,
                "eligible": not reasons,
                "exclusions": reasons,
                "wall": None if wall is None else asdict(wall),
            }
        )
    return {
        "version": 1,
        "observed_at": now,
        "model": model,
        "effort": effort,
        "routes": routes,
    }


def route_switch(
    roster: Roster,
    registry: Registry,
    recorded: str,
    failure: Mapping[str, object],
    *,
    now: int,
    model: str | None = None,
    effort: str | None = None,
) -> dict[str, Any]:
    """Record a causal wall with fixed event time and reconcile uncertain writes."""
    from .admission import AttemptKey

    provider = roster_provider(
        roster, "claude", lambda item: item.name == "claude" and item.kind == "claude"
    )
    found, environment, _ = claude_route(roster, recorded, leaving=True)
    if (
        found != recorded
        or environment.get(ANTHROPIC_BASE_URL_KEY)
        or not environment.get(CLAUDE_OAUTH_TOKEN_ENV)
    ):
        raise RouteRefused("account switch requires native subscription authentication")
    alias = route_account(
        provider,
        recorded,
        lambda name: account_route(
            name, claude_config_dir(provider.accounts.get(name))
        ),
        leaving=True,
    )
    observed_at = failure.get("observed_at")
    terminal = failure.get("terminal")
    native_session = terminal.get("session_id") if isinstance(terminal, dict) else None
    digest, epoch, key_data = (
        failure.get("profile_digest"),
        failure.get("stop_epoch"),
        failure.get("key"),
    )
    if (
        isinstance(observed_at, bool)
        or not isinstance(observed_at, int)
        or not 0 <= observed_at <= now
        or not isinstance(native_session, str)
        or not native_session
        or not isinstance(digest, str)
        or isinstance(epoch, bool)
        or not isinstance(epoch, int)
        or not isinstance(key_data, dict)
    ):
        raise RouteRefused("account switch lacks a causal subscription event")
    try:
        key = AttemptKey(key_data)
    except AdmissionRefusal as error:
        raise RouteRefused(
            "account switch lacks a causal subscription event"
        ) from error
    wall = subscription_limit(
        failure,
        account_alias=alias,
        key=key,
        profile_digest=digest,
        stop_epoch=epoch,
        native_session=native_session,
        now=observed_at,
    )
    if wall is None or wall.cooling_until <= now:
        raise RouteRefused("account switch lacks a causal subscription event")

    def readback() -> dict[str, Any] | None:
        observed = next(
            (
                row
                for row in registry.get_enforced_account_wall_rows(now)
                if row.account_alias == wall.account_alias
                and row.cooling_until >= wall.cooling_until
                and row.observed_at >= wall.observed_at
            ),
            None,
        )
        return None if observed is None else asdict(observed)

    def write() -> None:
        registry.record_account_wall(
            wall.account_alias,
            wall.cooling_until,
            wall.source.value,
            wall.limit_type,
            wall.observed_at,
        )

    try:
        write()
    except (*STORE_ERRORS, RegistryError):
        # A failed read propagates immediately. Only a successful unsatisfied
        # read admits one identical replay of this fixed event.
        observed = readback()
        if observed is None:
            write()
            observed = readback()
    else:
        observed = readback()
    if observed is None:
        raise RouteRefused("account wall postcondition is inconclusive")
    return {
        **route_list(
            roster,
            registry,
            model=model or provider.model or "default",
            effort=effort or provider.effort or "high",
            now=now,
        ),
        "wall": observed,
    }


def node_wall(
    provider: Provider,
    alias: str,
    failure: Mapping[str, Any],
    now: int,
    walls: tuple[AccountWallRow, ...],
) -> AccountWallRecord | None:
    """The wall a node's failed prompt puts on its account `alias`, or None.

    Claude and Codex: an authentication refusal (-32000), or for Codex the
    structured `codexErrorInfo` the adapter relays (`app_server._account_error`),
    never its free text. Neither
    names a reset, so nSPAWN's own default lasts in its place. AGY: its shim
    already recorded the wall, which is read back from `walls`.
    """
    observed_at = failure["observed_at"]
    if provider.kind == "agy":
        row = next((row for row in walls if row.account_alias == alias), None)
        if row is None or row.cooling_until <= now:
            return None
        return AccountWallRecord(
            alias,
            row.cooling_until,
            WallSource(row.source),
            row.limit_type,
            row.observed_at,
        )
    from .supervisor import Supervisor

    error = failure["error"]
    data = error.get("data")
    try:
        _, kind = _account_error(
            data.get("codexErrorInfo") if isinstance(data, dict) else None
        )
    except AppServerError:
        kind = None
    if error.get("code") == AUTH_REQUIRED:
        kind = "credential_rejected"
    if kind is None:
        return None
    lasting = {
        "credential_rejected": Supervisor.CREDENTIAL_REJECTED_WALL_S,
        "rate_limited": Supervisor.RATE_LIMIT_WALL_S,
    }.get(kind, Supervisor.CREDENTIAL_WALL_S)
    if observed_at + lasting <= now:
        return None
    return AccountWallRecord(
        alias, observed_at + lasting, WallSource.DEFAULT_1H, kind, observed_at
    )


#: ACP's "Authentication required" error code.
AUTH_REQUIRED = -32000


def route_wall(
    roster: Roster,
    registry: Registry,
    provider_name: str,
    recorded: str,
    failure: Mapping[str, object],
    *,
    now: int,
) -> dict[str, Any]:
    """Wall the account of the node on `recorded` for its failed prompt, and read the
    wall back: `{version, wall, reset}`. `reset` is the earliest reset of the
    provider's accounts when none is left unwalled, else None.
    A picker refusal adds `warning` and sets `reset` to None."""
    observed_at, error = failure.get("observed_at"), failure.get("error")
    if (
        isinstance(observed_at, bool)
        or not isinstance(observed_at, int)
        or not 0 <= observed_at <= now
        or not isinstance(error, dict)
        or isinstance(error.get("code"), bool)
        or not isinstance(error.get("code"), int)
    ):
        raise RouteRefused("node failure is malformed")
    bare = False
    if provider_name == "codex":
        provider, alias, found = codex_account(roster, recorded, leaving=True)
    elif provider_name == "agy":
        provider = roster_provider(roster, "agy", lambda p: p.kind == "agy")
        found, environment, _ = agy_route(roster, recorded, leaving=True)
        alias = environment[AGY_ACCOUNT_ENV]
    elif provider_name == "claude":
        home = os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))
        bare = recorded == home
        claude = next(
            (p for p in roster.providers if p.name == "claude" and p.kind == "claude"),
            None,
        )
        # When the roster declares Claude accounts, the picker never gives a bare
        # home to a node, so its wall is read back without a record. Without roster
        # accounts, there is no account to wall.
        if claude is None or not claude.ordered_accounts():
            return {"version": 1, "wall": None, "reset": None}
        provider, found = claude, recorded
        alias = (
            recorded
            if bare
            else route_account(
                provider,
                recorded,
                lambda name: account_route(name, home),
                leaving=True,
            )
        )
    else:
        raise RouteRefused(
            "account walls are recorded for claude, codex and agy nodes only"
        )
    if found != recorded:
        raise RouteRefused(
            "node's persisted native account route does not match this configured "
            "account"
        )
    wall = node_wall(
        provider,
        alias,
        {"observed_at": observed_at, "error": error},
        now,
        registry.get_enforced_account_wall_rows(now),
    )
    if wall is None:
        return {"version": 1, "wall": None, "reset": None}
    if provider.kind != "agy" and not bare:
        registry.record_account_wall(
            wall.account_alias,
            wall.cooling_until,
            wall.source.value,
            wall.limit_type,
            wall.observed_at,
        )
    observed = (
        {**asdict(wall), "source": wall.source.value}
        if bare
        else next(
            (
                asdict(row)
                for row in registry.get_enforced_account_wall_rows(now)
                if row.account_alias == alias
                and row.cooling_until >= wall.cooling_until
            ),
            None,
        )
    )
    if observed is None:
        raise RouteRefused("account wall postcondition is inconclusive")
    warning = None
    try:
        _select_account(
            keyless(provider, None),
            None,
            registry.get_provider_walls(now),
            registry.get_account_walls(now),
            now,
        )
    except AllProvidersWalled as walled:
        reset = walled.nearest_reset
    except PickerError as refusal:
        reset = None
        warning = refusal.message
    else:
        reset = None
    return {
        "version": 1,
        "wall": observed,
        "reset": reset,
        **({"warning": warning} if warning is not None else {}),
    }


def file_identity(path: Path) -> dict[str, str]:
    """Read one canonical regular runtime input, without launching it."""
    path = path.resolve()
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_IDENTITY_BYTES:
            raise RouteRefused("runtime input is not a bounded regular file")
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 65536):
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise RouteRefused("runtime input changed during identity capture")
        return {"path": str(path), "digest": "sha256:" + digest.hexdigest()}
    except OSError as error:
        raise RouteRefused("selected runtime input is unavailable") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def runtime_read(environment: Mapping[str, str]) -> dict[str, Any] | None:
    """Discover the installed public SDK export without importing or querying it.

    Stock ACP wrappers with no public SDK keep their existing caller contract.
    Once the package exists, an invalid export or native identity is a refusal.
    """
    try:
        adapter = adapter_command("claude", [])
    except RouteRefused:
        return None
    if len(adapter) != 2 or Path(adapter[1]).suffix not in {".js", ".mjs"}:
        return None
    package = next(
        (
            parent / "node_modules" / "@anthropic-ai" / "claude-agent-sdk"
            for parent in Path(adapter[1]).resolve().parents
            if (
                parent
                / "node_modules"
                / "@anthropic-ai"
                / "claude-agent-sdk"
                / "package.json"
            ).is_file()
        ),
        None,
    )
    if package is None:
        return None
    package = package.resolve()
    try:
        document = json.loads((package / "package.json").read_text("utf-8"))
    except (OSError, ValueError) as error:
        raise RouteRefused("installed public SDK package is unreadable") from error
    if (
        not isinstance(document, dict)
        or document.get("name") != "@anthropic-ai/claude-agent-sdk"
        or not isinstance(document.get("version"), str)
    ):
        raise RouteRefused("installed public SDK package identity is invalid")
    exports = document.get("exports")
    public = exports.get(".") if isinstance(exports, dict) else None
    if not isinstance(public, dict) or public.get("default") != "./sdk.mjs":
        raise RouteRefused("installed public SDK export does not name sdk.mjs")
    native = environment.get("CLAUDE_CODE_EXECUTABLE")
    if native is None:
        if sys.platform == "darwin":
            raise RouteRefused("Darwin requires the selected product executable")
        platform = (
            "linux-x64"
            if os.uname().machine == "x86_64"
            else "linux-arm64"
            if os.uname().machine == "aarch64"
            else None
        )
        if sys.platform != "linux" or platform is None:
            raise RouteRefused(
                "owned SDK bootstrap requires its installed Linux native runtime"
            )
        native = str(package.parent / ("claude-agent-sdk-" + platform) / "claude")
    selected: list[dict[str, str]] = []
    for root, directories, names in os.walk(package, followlinks=False):
        directories.sort()
        for name in [*directories, *names]:
            if (Path(root) / name).is_symlink():
                raise RouteRefused("public SDK package has an uncontrolled file link")
        selected.extend(file_identity(Path(root) / name) for name in sorted(names))
    node = file_identity(Path(adapter[0]))
    sdk = file_identity(package / "sdk.mjs")
    package_identity = file_identity(package / "package.json")
    native_identity = file_identity(Path(native))
    return {
        "version": 1,
        "sdk_version": document["version"],
        "sdk_package": document,
        "runtime": {
            "node": node,
            "sdk": sdk,
            "sdk_package": package_identity,
            "native": native_identity,
            "files": [*selected, node, native_identity],
        },
    }


def claude_route(
    roster: Roster,
    recorded: str | None,
    pick: Pick | None = None,
    *,
    leaving: bool = False,
) -> Route:
    """Claude Code in its own home, on one of the roster's Claude accounts.

    A new node takes the default account and records it in its route, `<alias>@<home>`;
    a later one keeps it. The token reaches the adapter's environment only, under the
    variable nSPAWN hands a run's account to (`child._deliver_credential`), with the
    inherited endpoint and backend cleared and only the account's own endpoint set.
    A route that is the bare home is a node made before
    accounts, or a roster with none: it keeps the home's own login, with nothing
    inherited over it either.
    """
    home = str(claude_config_dir())
    if not Path(home).is_absolute():
        raise RouteRefused("native account home must be absolute")
    environment = dict.fromkeys(CLAUDE_CLEARED, "")
    provider = next((p for p in roster.providers if p.name == "claude"), None)
    if sys.platform == "darwin" and (provider is None or provider.bin is None):
        raise RouteRefused("Darwin requires [bins].claude to select the executable")
    if provider is not None and provider.bin is not None:
        # The adapter's bundled Claude Code lags the owner's, and so does the model
        # catalog a node selects from (2.1.232 has no Opus 5.5). Judged as nSPAWN's
        # own runs judge it: absolute, executable, owner-controlled.
        try:
            binary = native_path(provider.bin, directory=False)
        except NativePreparationError as error:
            raise RouteRefused(f"[bins].claude is refused: {error}") from error
        environment["CLAUDE_CODE_EXECUTABLE"] = str(binary)
    if recorded == home or provider is None or not provider.ordered_accounts():
        return home, environment, []
    alias = route_account(
        provider,
        recorded,
        lambda name: account_route(
            name, claude_config_dir(provider.accounts.get(name))
        ),
        pick,
        leaving=leaving,
    )
    token = account_token(
        provider,
        alias,
        f"claude account {alias}",
        f"claude account {alias} has no readable token",
    )
    account = provider.accounts.get(alias)
    variable = (
        (account.auth_env if account is not None else None)
        or provider.auth_env
        or CLAUDE_OAUTH_TOKEN_ENV
    )
    if account is not None and account.base_url:
        environment[ANTHROPIC_BASE_URL_KEY] = account.base_url
    environment[variable] = token
    home = str(claude_config_dir(account))
    if account is not None and account.home is not None:
        environment["CLAUDE_CONFIG_DIR"] = home
    return account_route(alias, home), environment, []


def qwen_route(roster: Roster, recorded: str | None, pick: Pick | None = None) -> Route:
    """claude-agent-acp on a qwen account, as nSPAWN routes it: the default for a
    new node, the one its route names after.

    The credential is read from `<secrets>/<account>.token` and placed only in
    the child's environment, under the account's `auth_env`.
    """
    provider = roster_provider(roster, "qwen", lambda p: p.name == "qwen")
    alias = route_account(
        provider,
        recorded,
        lambda name: account_route(
            name, getattr(provider.accounts.get(name), "base_url", None)
        ),
        pick,
    )
    account = provider.accounts.get(alias)
    if (
        account is None
        or account.base_url is None
        or account.auth_env is None
        or provider.secrets is None
    ):
        raise RouteRefused("qwen account requires base_url, auth_env and secrets")
    if account.auth_env not in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"):
        raise RouteRefused("qwen auth_env must name an Anthropic API credential")
    token = account_token(
        provider, alias, "qwen account", "qwen account token file is unreadable"
    )
    environment = dict.fromkeys(QWEN_CLEARED, "")
    environment.update(provider.env)
    environment.update(account.env)
    model = account.model or provider.model
    if model:
        # An account's own model is its fast lane too: the provider's fast lane
        # names the id of another endpoint (vLLM `qwen3.8-27b`, ollama `qwen3.8:27b`).
        fast = provider.model_fast
        environment.update(
            {
                "ANTHROPIC_MODEL": model,
                "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
                "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
                "CLAUDE_CODE_SUBAGENT_MODEL": model,
                "ANTHROPIC_DEFAULT_HAIKU_MODEL": model
                if account.model or fast is None
                else fast,
            }
        )
    environment["ANTHROPIC_BASE_URL"] = account.base_url
    environment[account.auth_env] = token
    # The route names the account and its endpoint, so a persisted node never
    # resumes on another one after the roster changes.
    return account_route(alias, account.base_url), environment, []


def agy_route(
    roster: Roster,
    recorded: str | None,
    pick: Pick | None = None,
    *,
    leaving: bool = False,
) -> Route:
    """The agy shim on an agy account: the default for a new node, its own after."""
    provider = roster_provider(roster, "agy", lambda p: p.kind == "agy")
    alias = route_account(
        provider,
        recorded,
        lambda name: account_route(
            name, getattr(provider.accounts.get(name), "home", None)
        ),
        pick,
        leaving=leaving,
    )
    account = provider.accounts.get(alias)
    if account is None or account.home is None or not account.home.is_absolute():
        raise RouteRefused("agy account requires an absolute home")
    # The shim launches each turn on this account, not on the one it would pick.
    return (
        account_route(alias, account.home),
        {AGY_ACCOUNT_ENV: alias},
        [
            "-m",
            "nspawn.agy_acp",
        ],
    )


def codex_account(
    roster: Roster,
    recorded: str | None,
    pick: Pick | None = None,
    *,
    leaving: bool = False,
) -> tuple[Provider, str, str]:
    """The roster's Codex provider, and the alias and route of the account a Codex
    node runs on."""
    provider = roster_provider(
        roster, "Codex", lambda p: p.name == "codex" and p.kind == "codex"
    )

    def home_of(name: str) -> str:
        account = provider.accounts.get(name)
        if account is None or account.home is None:
            return ""
        return str(account.home.expanduser())

    if recorded is not None and recorded.startswith("/"):
        homes = [
            name for name in provider.ordered_accounts() if home_of(name) == recorded
        ]
        if len(homes) != 1:
            raise RouteRefused(
                "the Codex account this node was made on is no longer one account "
                "in the roster"
            )
        if homes[0] in provider.excluded_accounts and not leaving:
            raise RouteRefused(
                "the Codex account this node was made on is excluded in the roster"
            )
        alias, route = homes[0], recorded
    else:
        alias = route_account(
            provider,
            recorded,
            lambda n: account_route(n, home_of(n)),
            pick,
            leaving=leaving,
        )
        route = account_route(alias, home_of(alias))
    return provider, alias, route


def codex_route(
    roster: Roster,
    recorded: str | None,
    adapter: str | None,
    pick: Pick | None = None,
) -> Route:
    """codex-acp in a Codex account's home: the picked account for a new node,
    recorded as `<alias>@<home>`, and that account after. A route that is a bare home
    was made before accounts were recorded; it resolves only to the one account with
    that home. An external `adapter` takes the native CLI and its configuration as
    JSON.
    """
    provider, alias, route = codex_account(roster, recorded, pick)
    account = provider.accounts.get(alias)
    if account is None or account.home is None:
        raise RouteRefused("Codex roster must declare its native account's home")
    home = account.home.expanduser()
    if not home.is_absolute():
        raise RouteRefused(f"the roster's {alias} account home must be absolute")
    sqlite_home = (account.sqlite_home or home).expanduser()
    if not sqlite_home.is_absolute():
        raise RouteRefused("Codex native sqlite_home must be absolute")
    arguments = ["-c", 'cli_auth_credentials_store="file"']
    model = account.model or provider.model
    if model:
        arguments += ["-c", "model=" + json.dumps(model)]
    environment = {"CODEX_HOME": str(home), "CODEX_SQLITE_HOME": str(sqlite_home)}
    if adapter is not None:
        if not Path(adapter).is_absolute():
            raise RouteRefused(
                "NOTT_NODE_CODEX_ADAPTER must name an absolute native adapter"
            )
        cli = os.environ.get("CODEX_PATH")
        if not cli or not Path(cli).is_absolute():
            raise RouteRefused(
                "CODEX_PATH must name the configured absolute native CLI"
            )
        try:
            config = json.loads(os.environ.get("CODEX_CONFIG", "{}"))
        except ValueError as error:
            raise RouteRefused(
                "CODEX_CONFIG must contain a JSON configuration object"
            ) from error
        if not isinstance(config, dict):
            raise RouteRefused("CODEX_CONFIG must contain a JSON configuration object")
        config["cli_auth_credentials_store"] = "file"
        if model:
            config["model"] = model
        environment.update({"CODEX_PATH": cli, "CODEX_CONFIG": json.dumps(config)})
        arguments = []
    return route, environment, arguments


#: The providers a node runs on, as `route_start` names them.
NODE_PROVIDERS = ("claude", "codex", "qwen", "agy")


def route_start(
    roster: Roster,
    provider: str,
    recorded: str | None = None,
    *,
    codex_adapter: str | None = None,
    pick: Pick | None = None,
) -> Route:
    """The route, environment and arguments a ``provider`` node's adapter starts with.

    ``recorded`` is the route a node was made with, without its provider: the
    account it names is the one resolved. None is a new node, on the account
    ``pick`` chooses (the shared picker with no registry data without one). The
    environment is set
    over the host's own, where "" clears a name the adapter must not inherit.
    ``codex_adapter`` is the external Codex adapter the host runs.
    """
    if provider == "claude":
        return claude_route(roster, recorded, pick)
    if provider == "qwen":
        return qwen_route(roster, recorded, pick)
    if provider == "agy":
        return agy_route(roster, recorded, pick)
    if provider == "codex":
        return codex_route(roster, recorded, codex_adapter, pick)
    raise RouteRefused("provider must name claude, codex, qwen or agy")


def adapter_command(
    provider: str, arguments: list[str], *, codex_adapter: str | None = None
) -> list[str]:
    """The adapter process a ``provider`` node runs: the AGY shim under this
    interpreter, else claude-agent-acp or the Codex adapter as installed, a script
    one under the installed node runtime."""
    if provider == "agy":
        return [sys.executable, *arguments]
    command = (
        ("codex-acp" if codex_adapter is None else codex_adapter)
        if provider == "codex"
        else "claude-agent-acp"
    )
    resolved = shutil.which(command)
    if resolved is None:
        installed = Path.home() / ".npm-global" / "bin" / command
        if not installed.is_file():
            raise RouteRefused(f"native adapter {command} is not installed")
        resolved = str(installed)
    launch = [resolved, *arguments]
    entry = Path(resolved).resolve()
    if entry.suffix in {".js", ".mjs"}:
        runtime = shutil.which("node")
        if runtime is None:
            raise RouteRefused(f"{provider} ACP requires the installed node runtime")
        launch = [runtime, str(entry), *arguments]
    return launch


def route_launch(
    provider: str, recorded: str | None, *, codex_adapter: str | None = None
) -> tuple[list[str], dict[str, str]]:
    """The adapter argv and the environment over this process's own that start a
    ``provider`` node on the account ``recorded`` names (`nspawn route start`)."""
    route, environment, arguments = route_start(
        read_roster(), provider, recorded, codex_adapter=codex_adapter
    )
    if recorded is not None and route != recorded:
        raise RouteRefused(
            "node's persisted native account route does not match this configured "
            "account"
        )
    launch = adapter_command(provider, arguments, codex_adapter=codex_adapter)
    return launch, {**os.environ, **environment}
