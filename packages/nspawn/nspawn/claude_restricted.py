"""The fail-closed launch of a Claude native writer under the client's own sandbox.

`claude -p --restricted` with `--settings` runs every Bash call inside Claude
Code's bubblewrap sandbox. Measured on 2.1.284 (P-C E0, 2026-09-29), four
boundaries hold only if the launcher closes them, so this module does:

1. A garbled `--settings` file runs the tools UNCAGED with rc=0 and no warning.
   The settings are written here, 0600, re-read, hash-pinned and validated;
   `verify_pinned_settings` is the check to repeat right before exec.
2. A linked worktree's common gitdir stays readable whatever denyRead says.
   Only a standalone clone, whose `.git` is a real directory, may be the cwd.
3. Tools inherit the launch environment. The environment is a closed allowlist
   plus an explicit deny for every credential and host-only name. It never
   carries `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1`: measured on 2.1.284, that
   variable switches off `denyRead: ["/"]`, and the tools read $HOME and every
   path outside the checkout; only explicitly named denies stayed masked.
4. The default per-user temp is shared across sessions. TMPDIR is private to
   the run.
5. The client's sandbox setup creates .claude/.cc-writes under its initial
   working directory (2.1.296). Both writers and reviewers start in private
   scratch; project work names the separately admitted checkout.

A boundary this composition cannot establish refuses the launch; it never
degrades to an uncaged child.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from .admission import AdmissionRefusal, PrelaunchProfileV1, runtime_files
from .child import CREDENTIAL_ENV_NAMES
from .errors import NERR_1116, NSpawnError
from .owner_roster import model_env_build

SETTINGS_FILE: Final = "claude-settings.json"
#: Where each run's TMPDIR is made: short, because the client binds its sandbox
#: bridge sockets under TMPDIR and AF_UNIX paths stop at 108 bytes. Measured on
#: 2.1.284: a 30-character TMPDIR worked; a 93-character one, an installed run's
#: `~/.nott/state/nspawn/runs/<uuid>/tmp`, failed "to create bridge sockets".
SCRATCH_ROOT: Final = Path("/var/tmp")
#: No settings layer but the pinned `--settings`: the read-only child's
#: `claude_child.SETTING_SOURCES` keeps the user's; this writer keeps none.
SETTING_SOURCES: Final = ""
#: The client's own config directory (credentials, transcripts): in the run, so
#: the pinned token is the only credential it can find and nothing lands in the
#: operator's `~/.claude`. Measured on 2.1.284: without it the transcript did.
CONFIG_DIR: Final[str] = "claude"
#: The PATH the client and its tools resolve `bwrap` and the shell from.
RUNTIME_PATH: Final = "/usr/local/bin:/usr/bin:/bin"
#: The system trees a tool needs to execute anything at all (E0 settings-2).
RUNTIME_READ: Final[tuple[str, ...]] = (
    "/usr",
    "/bin",
    "/lib",
    "/lib64",
    "/etc",
    "/dev",
)
#: Account homes under $HOME, expanded at compose time: the sandbox takes paths.
#: Directories only, plus `.claude.json`: the vendor churns `.claude.json.tmp.*`
#: files, and a denied path gone by exec time fails bwrap ("Can't get type of
#: source"). `denyRead: ["/"]` already hides them.
HOME_GUARDS: Final[tuple[str, ...]] = (".claude*", ".codex*", ".nott")
HOME_GUARD_FILES: Final[tuple[str, ...]] = (".claude.json",)
#: Host-only names the E0 scrub left visible to tools.
HOST_ONLY_ENV: Final[tuple[str, ...]] = ("DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR")
MAX_SETTINGS_BYTES: Final = 64 * 1024

#: (key path under "sandbox", required value): the cage itself.
_REQUIRED_VALUES: Final[tuple[tuple[tuple[str, ...], object], ...]] = (
    (("enabled",), True),
    (("failIfUnavailable",), True),
    (("allowUnsandboxedCommands",), False),
    (("autoAllowBashIfSandboxed",), True),
    (("network", "allowedDomains"), []),
    (("network", "strictAllowlist"), True),
)
_REQUIRED_LISTS: Final[tuple[tuple[str, ...], ...]] = (
    ("filesystem", "denyRead"),
    ("filesystem", "allowRead"),
    ("filesystem", "denyWrite"),
    ("filesystem", "allowWrite"),
    ("credentials", "envVars"),
)


class RestrictedLaunchRefused(NSpawnError):
    """A required boundary of the restricted writer cannot be established."""

    def __init__(self, detail: str) -> None:
        super().__init__(NERR_1116, f"claude restricted launch: {detail}")


@dataclass(frozen=True, slots=True)
class RestrictedLaunch:
    """What the supervisor execs: argv, the closed env, and the pinned settings.

    The prompt is not on argv; the caller writes it to stdin. The cwd is the
    checkout the composition was given.
    """

    argv: tuple[str, ...]
    env: dict[str, str]
    settings_path: Path
    settings_sha256: str
    #: Where the child starts: private scratch, because the client's sandbox
    #: setup writes its masks and staging directories in its initial cwd.
    #: Those administrative writes must never enter the admitted project.
    cwd: Path
    #: Where the child must perform project work, with its admitted access.
    preface: str | None


def _at(document: object, keys: tuple[str, ...]) -> object:
    for key in keys:
        if not isinstance(document, dict) or key not in document:
            return _MISSING
        document = document[key]
    return document


_MISSING: Final = object()


def verify_pinned_settings(path: Path, sha256: str) -> None:
    """Refuse unless `path` holds exactly the pinned, well-formed cage."""
    try:
        metadata = os.lstat(path)
        if not stat.S_ISREG(metadata.st_mode):
            raise RestrictedLaunchRefused(f"settings {path} is not a regular file")
        if stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_uid != os.getuid():
            raise RestrictedLaunchRefused(f"settings {path} is not 0600 and ours")
        with open(path, "rb") as handle:
            raw = handle.read(MAX_SETTINGS_BYTES + 1)
    except OSError as error:
        raise RestrictedLaunchRefused(f"settings {path}: {error}") from error
    if hashlib.sha256(raw).hexdigest() != sha256:
        raise RestrictedLaunchRefused(f"settings {path} do not match their pin")
    try:
        sandbox = _at(json.loads(raw), ("sandbox",))
    except (ValueError, RecursionError) as error:
        raise RestrictedLaunchRefused(f"settings {path} are not JSON") from error
    for keys, expected in _REQUIRED_VALUES:
        value = _at(sandbox, keys)
        # `type` first: JSON 1 must not pass for true.
        if type(value) is not type(expected) or value != expected:
            raise RestrictedLaunchRefused(f"settings lack sandbox.{'.'.join(keys)}")
    for keys in _REQUIRED_LISTS:
        if not isinstance(_at(sandbox, keys), list):
            raise RestrictedLaunchRefused(f"settings lack sandbox.{'.'.join(keys)}")
    deny_read = _at(sandbox, ("filesystem", "denyRead"))
    if not isinstance(deny_read, list) or deny_read[:1] != ["/"]:
        raise RestrictedLaunchRefused("settings do not deny reading by default")


def make_scratch(root: Path = SCRATCH_ROOT) -> Path:
    """A fresh short scratch directory, 0700, for one caged run. The caller
    removes it, and only once the run's slice is empty: a surviving descendant
    would otherwise meet a freed name under a world-writable parent."""
    try:
        return Path(tempfile.mkdtemp(prefix="nott-claude-", dir=root))
    except OSError as error:
        raise RestrictedLaunchRefused(f"scratch root {root}: {error}") from error


def _require_private_directory(directory: Path) -> None:
    try:
        info = os.lstat(directory)
    except OSError as error:
        raise RestrictedLaunchRefused(f"scratch {directory}: {error}") from error
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o700
        or info.st_uid != os.getuid()
    ):
        raise RestrictedLaunchRefused(
            f"scratch {directory} is not a private directory of this account"
        )


def _require_standalone_checkout(checkout: Path) -> None:
    if os.path.realpath(checkout) != os.fspath(checkout):
        raise RestrictedLaunchRefused(f"checkout {checkout} is not a canonical path")
    git = checkout / ".git"
    try:
        mode = os.lstat(git).st_mode
    except OSError as error:
        raise RestrictedLaunchRefused(f"{git}: {error}") from error
    # A `.git` FILE is a linked worktree: its common gitdir stays readable
    # under any denyRead (E0 3a). A symlink points the same way.
    if not stat.S_ISDIR(mode):
        raise RestrictedLaunchRefused(f"{git} is not a standalone clone's directory")


def _review_gitdirs(checkout: Path) -> tuple[Path, ...]:
    """What a reviewer reads its history from, beyond the checkout itself.

    None for a snapshot (no `.git`) or a clone (`.git` inside the checkout); a
    linked worktree's own gitdir and the common gitdir it names otherwise.
    """
    if os.path.realpath(checkout) != os.fspath(checkout):
        raise RestrictedLaunchRefused(f"checkout {checkout} is not a canonical path")
    git = checkout / ".git"
    try:
        mode = os.lstat(git).st_mode
    except FileNotFoundError:
        return ()
    except OSError as error:
        raise RestrictedLaunchRefused(f"{git}: {error}") from error
    if stat.S_ISDIR(mode):
        return ()
    if not stat.S_ISREG(mode):
        raise RestrictedLaunchRefused(f"{git} is neither a gitdir nor a gitdir file")
    try:
        pointer = git.read_text().strip()
        own = Path(pointer.removeprefix("gitdir: ")).resolve(strict=True)
        common = (own / (own / "commondir").read_text().strip()).resolve(strict=True)
    except (OSError, ValueError) as error:
        raise RestrictedLaunchRefused(f"{git}: {error}") from error
    return (own, common)


def _settings_document(
    checkout: Path,
    tmpdir: Path,
    home: Path,
    host_auth: Mapping[str, str],
    *,
    writable: bool,
    extra_read: tuple[Path, ...],
) -> dict[str, object]:
    # A writer's `.git` is denied read and write on purpose: its Bash runs no
    # git, so it can neither plant a hook nor rewrite history; the workflow
    # reads the diff from outside the cage. A reviewer reads history and
    # writes nothing of the candidate.
    guarded = [
        *([str(checkout / ".git")] if writable else []),
        *sorted(
            str(p)
            for pattern in HOME_GUARDS
            for p in home.glob(pattern)
            if p.is_dir() or p.name in HOME_GUARD_FILES
        ),
        "/run/user",
    ]
    denied_env = sorted(set(host_auth) | CREDENTIAL_ENV_NAMES | set(HOST_ONLY_ENV))
    if writable:
        deny_write, allow_write = guarded, [str(checkout), str(tmpdir)]
    else:
        candidate = [str(checkout)]
        if (checkout / ".git").is_dir():
            candidate.append(str(checkout / ".git"))
        deny_write = [*guarded, *candidate, *(str(p) for p in extra_read)]
        allow_write = [str(tmpdir)]
    return {
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "allowUnsandboxedCommands": False,
            "autoAllowBashIfSandboxed": True,
            "filesystem": {
                "denyRead": ["/", *guarded],
                "allowRead": [
                    *RUNTIME_READ,
                    str(checkout),
                    str(tmpdir),
                    *(str(p) for p in extra_read),
                ],
                "denyWrite": deny_write,
                "allowWrite": allow_write,
            },
            "network": {"allowedDomains": [], "strictAllowlist": True},
            "credentials": {
                "envVars": [{"name": name, "mode": "deny"} for name in denied_env]
            },
        }
    }


def compose_restricted_launch(
    *,
    binary: Path,
    checkout: Path,
    run_dir: Path,
    host_auth: Mapping[str, str],
    model: str | None = None,
    effort: str | None = None,
    system_prompt_file: Path | None = None,
    session_id: str | None = None,
    home: Path | None = None,
    path: str = RUNTIME_PATH,
    tmpdir: Path,
    writable: bool = True,
    read_paths: tuple[Path, ...] = (),
    config_dir: Path | None = None,
    resume: bool = False,
) -> RestrictedLaunch:
    """Compose one caged Claude turn, or refuse: a writer (`writable`) writes
    its standalone checkout; a reviewer writes only its scratch, and reads the
    candidate, its history and the named `read_paths`.

    `host_auth` is the CLI host's credential (CLAUDE_CODE_OAUTH_TOKEN): it is in
    the launch env and denied to every tool. `run_dir` must exist and be private
    to this run; the settings and the client's config dir are created in it.
    `tmpdir` is the run's scratch from `make_scratch`, owned by the caller.
    """
    for label, candidate in (
        ("binary", binary),
        ("checkout", checkout),
        ("run directory", run_dir),
    ):
        if not candidate.is_absolute():
            raise RestrictedLaunchRefused(f"the {label} {candidate} is not absolute")
    for extra in read_paths:
        if not extra.is_absolute():
            raise RestrictedLaunchRefused(f"the read path {extra} is not absolute")
    if writable:
        _require_standalone_checkout(checkout)
        extra_read: tuple[Path, ...] = ()
    else:
        extra_read = (*_review_gitdirs(checkout), *read_paths)
    if not host_auth:
        # Without it the client finds no login in its empty config dir and fails
        # on authentication inside the cage; name the cause here instead.
        raise RestrictedLaunchRefused("the launch carries no credential")
    stray = set(host_auth) - CREDENTIAL_ENV_NAMES
    if stray:
        raise RestrictedLaunchRefused(
            f"host_auth carries non-credential {sorted(stray)}"
        )
    if shutil.which("bwrap", path=path) is None:
        raise RestrictedLaunchRefused(f"bwrap is not on {path}")
    home = Path.home() if home is None else home
    config_dir = run_dir / CONFIG_DIR if config_dir is None else config_dir
    if resume:
        _require_private_directory(config_dir)
    settings = run_dir / SETTINGS_FILE
    _require_private_directory(tmpdir)
    raw = (
        json.dumps(
            _settings_document(
                checkout,
                tmpdir,
                home,
                host_auth,
                writable=writable,
                extra_read=extra_read,
            ),
            indent=1,
        ).encode()
        + b"\n"
    )
    try:
        fd = os.open(
            settings,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
        if not resume:
            config_dir.mkdir(mode=0o700)
    except OSError as error:
        raise RestrictedLaunchRefused(f"run directory {run_dir}: {error}") from error
    sha256 = hashlib.sha256(raw).hexdigest()
    verify_pinned_settings(settings, sha256)
    cwd = tmpdir / "work"
    try:
        cwd.mkdir(mode=0o700)
    except OSError as error:
        raise RestrictedLaunchRefused(f"scratch {tmpdir}: {error}") from error
    if writable:
        preface = (
            f"The writable project checkout is the exact directory {checkout}. "
            "Enter that directory as your working directory for all project work. "
            "Your initial working directory is private administrative scratch, "
            "outside the project."
        )
    else:
        preface = (
            f"The candidate under review is the read-only directory {checkout}: "
            "read it there. Your working directory is a private scratch "
            "directory, and nothing you write reaches the candidate."
        )

    env = {
        "PATH": path,
        "HOME": str(home),
        "TMPDIR": str(tmpdir),
        "CLAUDE_CONFIG_DIR": str(config_dir),
    }
    # Model and effort travel in the env, never on argv (child.build_child_env).
    # The native catalogue resolves family names before the tier values are set.
    if model is not None:
        model_env_build(model, None, binary, env)
    if effort is not None:
        env["CLAUDE_CODE_EFFORT_LEVEL"] = effort
    env.update(host_auth)

    # `--allowedTools Bash`: without it the permission layer answers before
    # bwrap, and the cage is never what refused.
    argv = [
        os.fspath(binary),
        "-p",
        "--restricted",
        "--permission-mode",
        "dontAsk",
        "--tools",
        "Bash",
        "--allowedTools",
        "Bash",
        "--strict-mcp-config",
        "--output-format",
        "stream-json",
        "--verbose",
        # No settings layer but the pinned one: neither the user's nor one the
        # model could plant in the checkout for a later attempt.
        "--setting-sources",
        SETTING_SOURCES,
        "--settings",
        str(settings),
    ]
    if system_prompt_file is not None:
        argv.extend(("--append-system-prompt-file", os.fspath(system_prompt_file)))
    if session_id is not None:
        if resume:
            argv.extend(("--resume", session_id))
        else:
            argv.append(f"--session-id={session_id}")
    return RestrictedLaunch(
        argv=tuple(argv),
        env=env,
        settings_path=settings,
        settings_sha256=sha256,
        cwd=cwd,
        preface=preface,
    )


#: What 2.1.284 answers every Bash call with when its sandbox cannot start; with
#: `failIfUnavailable` nothing runs, yet the turn itself still ends "completed".
SANDBOX_DOWN: Final = b"Sandbox is required but failed to initialize"


def _refused_by_unstarted_sandbox(line: bytes) -> bool:
    """A tool result the client itself failed with the unstarted-sandbox
    message: never a command's output, a file or the model's text quoting it."""
    if SANDBOX_DOWN not in line:
        return False
    try:
        frame = json.loads(line)
    except ValueError:
        return False
    message = frame.get("message") if isinstance(frame, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if frame.get("type") != "user" or not isinstance(content, list):
        return False
    return any(
        isinstance(block, dict)
        and block.get("type") == "tool_result"
        and block.get("is_error") is True
        and isinstance(block.get("content"), str)
        and block["content"].encode().startswith(SANDBOX_DOWN)
        for block in content
    )


def require_sandbox_started(stdout: Path) -> None:
    """Refuse a finished turn whose tool calls the unstarted sandbox refused."""
    try:
        with stdout.open("rb") as handle:
            down = any(_refused_by_unstarted_sandbox(line) for line in handle)
    except FileNotFoundError:
        return
    if down:
        raise RestrictedLaunchRefused(
            "the client's sandbox did not start; every command was refused"
        )


NODE_BOOTSTRAP = """import os, socket, subprocess, sys, time
bridge = subprocess.Popen([sys.argv[1], "TCP4-LISTEN:39871,bind=127.0.0.1,reuseaddr,fork", "UNIX-CONNECT:" + sys.argv[2]])
deadline = time.monotonic() + 60
while True:
    if bridge.poll() is not None or time.monotonic() >= deadline:
        raise SystemExit(126)
    try:
        with socket.create_connection(("127.0.0.1", 39871), .1):
            break
    except OSError:
        time.sleep(.01)
os.execvpe(sys.argv[3], sys.argv[3:], os.environ)
"""


@dataclass(frozen=True, slots=True)
class NodeLaunch:
    """The full process boundary, established before SDK or native bootstrap."""

    argv: tuple[str, ...]
    env: dict[str, str]
    proxy: Path | None


def _destination_within(path: str, grants: list[str]) -> bool:
    target = Path(path)
    return any(
        target == Path(grant) or target.is_relative_to(grant) for grant in grants
    )


def node_tool_settings(
    scope: Mapping[str, Any],
    scratch: str,
    configuration: list[str],
    credential_names: set[str] | frozenset[str] = CREDENTIAL_ENV_NAMES,
) -> dict[str, Any]:
    """The native tool cage is narrower than producer administration.

    Bash receives no authentication environment or network. Native file tools
    cannot read administrative state or modify selected launch inputs. The
    outer cage continues to bound startup, producers and every native process.
    """
    administrative = [path for path in scope["administrative"] if path != scratch]
    hidden = [
        path
        for path in administrative
        if _destination_within(path, [*scope["read"], *scope["write"]])
    ]
    read = list(dict.fromkeys([*RUNTIME_READ, *scope["read"], scratch]))
    write = list(dict.fromkeys([*scope["write"], scratch]))
    denies = list(dict.fromkeys([*administrative, *configuration]))

    # Native gitignore rules use // for filesystem-root paths. Escape literal
    # destination names before adding the deliberate descendant wildcard.
    def permission_path(path: str) -> str:
        return "/" + "".join("\\" + char if char in "\\*?[]" else char for char in path)

    permissions = [
        f"{tool}({permission_path(path)}/**)"
        for path in denies
        for tool in ("Read", "Edit", "Write", "Glob", "Grep")
    ]
    permissions += [
        f"{tool}({permission_path(path)})"
        for path in configuration
        for tool in ("Read", "Edit", "Write")
    ]
    return {
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "allowUnsandboxedCommands": False,
            "autoAllowBashIfSandboxed": True,
            "filesystem": {
                "denyRead": ["/", *hidden, *configuration],
                "allowRead": read,
                "denyWrite": [
                    *administrative,
                    *configuration,
                    *([] if scope["write"] else scope["read"]),
                ],
                "allowWrite": write,
            },
            "network": {"allowedDomains": [], "strictAllowlist": True},
            "credentials": {
                "envVars": [
                    {"name": name, "mode": "deny"}
                    for name in sorted(set(credential_names) | set(HOST_ONLY_ENV))
                ]
            },
        },
        "settings": {"permissions": {"deny": permissions}},
    }


def _tool_boundary(data: dict[str, Any]) -> None:
    options = data["constructor"]["options"]
    scratch = options.get("env", {}).get("TMPDIR")
    if not isinstance(scratch, str):
        raise AdmissionRefusal("native tool boundary has no scratch destination")
    credential_names = set(CREDENTIAL_ENV_NAMES)
    for producer in [*data["mcp"], *data["producer"]]:
        credential_names.update(producer["inputs"].get("credential_environment", []))
    required = node_tool_settings(
        data["scope"],
        scratch,
        [entry["path"] for entry in data["configuration"]],
        credential_names,
    )
    if options.get("sandbox") != required["sandbox"]:
        raise AdmissionRefusal("native tool boundary differs from its admitted scope")
    settings = options.get("settings", {})
    denied = settings.get("permissions", {}).get("deny", [])
    if not isinstance(denied, list) or any(
        permission not in denied
        for permission in required["settings"]["permissions"]["deny"]
    ):
        raise AdmissionRefusal("native tool boundary exposes administrative inputs")


def runtime_roots(data: Mapping[str, Any]) -> tuple[str, ...]:
    """Original installed roots selected by existing Python/plugin inputs."""
    roots = {
        path
        for item in data["mcp"]
        for path in item["inputs"]
        .get("environment", {})
        .get("PYTHONPATH", "")
        .split(":")
        if path
    }
    plugin = data["project"]["plugin_parent"]
    if plugin is not None and Path(plugin).is_dir():
        roots.add(plugin)
    roots.update(data["policy"]["trust_filter"].get("installed_plugins", []))
    protected = [
        data["project"]["root"],
        *data["scope"]["write"],
        *data["scope"]["administrative"],
    ]
    selected: list[str] = []
    for root in sorted(roots, key=lambda value: (value.count("/"), value)):
        path = Path(root)
        if (
            not path.is_absolute()
            or path.resolve() != path
            or not path.is_dir()
            or root == "/"
            or any(
                path.is_relative_to(other) or Path(other).is_relative_to(path)
                for other in protected
            )
        ):
            raise AdmissionRefusal("runtime root overlaps or lacks a canonical input")
        if "/usr" in data["scope"]["read"] and path.is_relative_to("/usr"):
            continue
        if not any(path.is_relative_to(parent) for parent in selected):
            selected.append(root)
    return tuple(selected)


def runtime_inventory_capture(
    data: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Inventory every installed entry, including bootstrap, cache and aliases."""
    from .route import RouteRefused, file_identity

    inventory: dict[str, Any] = {}
    identities: dict[str, dict[str, str]] = {}

    def refused(error: OSError) -> None:
        raise AdmissionRefusal("runtime inventory is unavailable") from error

    try:
        for location in runtime_roots(data):
            root = Path(location)
            directories, links = [], {}
            for parent, names, filenames in os.walk(
                root, followlinks=False, onerror=refused
            ):
                directories.append(str(Path(parent).relative_to(root)))
                for name in sorted([*names, *filenames]):
                    path = Path(parent) / name
                    info = path.lstat()
                    if stat.S_ISLNK(info.st_mode):
                        target = path.resolve(strict=True)
                        if not target.is_relative_to(root) or not (
                            target.is_file() or target.is_dir()
                        ):
                            raise AdmissionRefusal(
                                "runtime inventory has an external or unknown link"
                            )
                        links[str(path.relative_to(root))] = str(target)
                        if target.is_dir():
                            continue
                    elif stat.S_ISDIR(info.st_mode):
                        continue
                    elif not stat.S_ISREG(info.st_mode):
                        raise AdmissionRefusal(
                            "runtime inventory contains a nonregular input"
                        )
                    identity = file_identity(path)
                    identities[identity["path"]] = identity
                    if len(identities) > 16384:
                        raise AdmissionRefusal("runtime inventory exceeds its bound")
            inventory[location] = {
                "directories": sorted(directories),
                "links": links,
            }
    except (OSError, RuntimeError, RouteRefused) as error:
        raise AdmissionRefusal("runtime inventory changed or is unavailable") from error
    return inventory, list(identities.values())


def runtime_inventory_verify(
    data: Mapping[str, Any], *, captured_configuration: bool = False
) -> None:
    """Revalidate original inputs and complete topology before bootstrap effects."""
    from .route import RouteRefused, file_identity

    inventory, identities = runtime_inventory_capture(data)
    expected = data["policy"]["trust_filter"].get("runtime_inventory", {})
    if inventory != expected:
        raise AdmissionRefusal("runtime inventory topology differs")
    selected = runtime_files(data["runtime"])
    selected.extend(
        identity
        for producer in [*data["mcp"], *data["producer"]]
        for identity in producer["executables"]
    )
    expected_files: dict[str, dict[str, str]] = {}
    for identity in selected:
        prior = expected_files.get(identity["path"])
        if prior is not None and prior != identity:
            raise AdmissionRefusal("runtime input has incompatible identities")
        expected_files[identity["path"]] = identity
    observed = {identity["path"]: identity for identity in identities}
    roots = tuple(inventory)
    if set(observed) != {
        path
        for path in expected_files
        if any(Path(path).is_relative_to(root) for root in roots)
    }:
        raise AdmissionRefusal("runtime inventory contains missing or extra inputs")
    configuration = (
        {item["path"] for item in data["configuration"]}
        if captured_configuration
        else set()
    )
    try:
        for path, identity in expected_files.items():
            if path in configuration:
                continue
            actual = observed.get(path) or file_identity(Path(path))
            if actual != identity:
                raise AdmissionRefusal("runtime content identity differs")
    except RouteRefused as error:
        raise AdmissionRefusal("runtime input is unavailable") from error


def configuration_verify(
    data: Mapping[str, Any],
    configuration: Mapping[str, Path],
    pinned: Mapping[str, int],
) -> None:
    """Named configuration overlays retain sealed bytes and native path identity."""
    expected = tuple(dict.fromkeys(item["path"] for item in data["configuration"]))
    if tuple(configuration) != expected:
        raise AdmissionRefusal("configuration snapshot differs from selected inputs")
    if not expected:
        return
    root = configuration[expected[0]].parent
    protected = [
        path
        for kind in ("read", "write", "administrative")
        for path in data["scope"][kind]
    ]
    info = root.lstat()
    parent = root.parent.lstat()
    if (
        root.resolve() != root
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o555
        or not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != os.geteuid()
        or stat.S_IMODE(parent.st_mode) != 0o700
        or any(
            root.is_relative_to(path) or Path(path).is_relative_to(root)
            for path in protected
        )
        or set(root.iterdir()) != set(configuration.values())
    ):
        raise AdmissionRefusal("configuration snapshot topology differs")
    for index, location in enumerate(expected):
        source = configuration[location]
        descriptor = pinned.get(location)
        if source != root / str(index) or descriptor is None:
            raise AdmissionRefusal("configuration snapshot identity differs")
        info = source.lstat()
        admitted = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_size != admitted.st_size
            or stat.S_IMODE(info.st_mode) != stat.S_IMODE(admitted.st_mode)
        ):
            raise AdmissionRefusal("configuration snapshot file identity differs")
        with source.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").digest()
        captured = hashlib.sha256()
        offset = 0
        while chunk := os.pread(descriptor, 65536, offset):
            captured.update(chunk)
            offset += len(chunk)
        if digest != captured.digest():
            raise AdmissionRefusal("configuration snapshot captured bytes differ")


def compose_node_launch(
    profile: PrelaunchProfileV1,
    *,
    consumer_argv: tuple[str, ...],
    host_auth: Mapping[str, str],
    schema: Path,
    grant_root: Path,
    descriptor: int,
    pinned: Mapping[str, int] | None = None,
    producer_auth: Mapping[str, str] | None = None,
    configuration: Mapping[str, Path] | None = None,
) -> NodeLaunch:
    """Compose a Linux mount/network/PID cage for the complete node process.

    This is passive: it creates no process, proxy, hook, helper or MCP server.
    The owning launcher supplies its exact verified descriptor and grant root.
    """
    from .network import destination_read

    data = profile.data()
    scope = data["scope"]
    _tool_boundary(data)
    roots = runtime_roots(data)
    if roots:
        runtime_inventory_verify(data, captured_configuration=pinned is not None)
    if configuration is not None:
        if pinned is None:
            raise AdmissionRefusal("configuration snapshots require sealed inputs")
        configuration_verify(data, configuration, pinned)
    if not consumer_argv or consumer_argv[0] != data["runtime"]["node"]["path"]:
        raise AdmissionRefusal("consumer runtime differs from PRELAUNCH")
    bwrap = shutil.which("bwrap", path=RUNTIME_PATH)
    if bwrap is None:
        raise AdmissionRefusal("node bootstrap confinement is unavailable")
    env = {"PATH": RUNTIME_PATH, "LANG": "C.UTF-8"}
    nonsecret = data["constructor"]["options"].get("env", {})
    if not isinstance(nonsecret, dict) or any(
        not isinstance(name, str)
        or not isinstance(value, str)
        or name in CREDENTIAL_ENV_NAMES
        or name in HOST_ONLY_ENV
        or name in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}
        for name, value in nonsecret.items()
    ):
        raise AdmissionRefusal(
            "constructor environment exceeds its admitted vocabulary"
        )
    env.update(nonsecret)
    for name in ("HOME", "CLAUDE_CONFIG_DIR", "TMPDIR"):
        if name not in env or not _destination_within(
            env[name], scope["administrative"]
        ):
            raise AdmissionRefusal(
                "native state and scratch must have admitted destinations"
            )
    if not host_auth or set(host_auth) - CREDENTIAL_ENV_NAMES:
        raise AdmissionRefusal("native account authentication is unavailable")
    env.update(host_auth)
    credential_channels = {
        name
        for producer in [*data["mcp"], *data["producer"]]
        for name in producer["inputs"].get("credential_environment", [])
    }
    if set(producer_auth or {}) != credential_channels or any(
        not isinstance(name, str)
        or not name.startswith("NOTT_")
        or not isinstance(value, str)
        or not value
        for name, value in (producer_auth or {}).items()
    ):
        raise AdmissionRefusal("producer authentication differs from admitted channels")
    env.update(producer_auth or {})
    writable = [*scope["write"], *scope["administrative"]]
    readonly = [
        *scope["read"],
        schema.as_posix(),
        *(identity["path"] for identity in runtime_files(data["runtime"])),
        *(item["path"] for item in data["configuration"]),
        *(
            path
            for path in (
                data["project"]["plugin_parent"],
                data["project"]["plugin_view"],
            )
            if path is not None
        ),
    ]
    for item in [*data["mcp"], *data["producer"]]:
        if (
            item["caller"] != data["node_id"]
            or item["project"] != data["project"]["id"]
        ):
            raise AdmissionRefusal("producer caller or project differs")
        for kind in ("read", "write", "administrative"):
            grants = scope[kind] if kind == "read" else writable
            if any(
                not _destination_within(path, grants) for path in item["grants"][kind]
            ):
                raise AdmissionRefusal("producer filesystem grants exceed the node")
        if set(item["grants"]["network"]) - set(scope["network"]) or any(
            not _destination_within(path, writable) for path in item["destinations"]
        ):
            raise AdmissionRefusal("producer destinations exceed the node")
        readonly.extend(identity["path"] for identity in item["executables"])
    for destination in scope["network"]:
        destination_read(destination)
    for path in [*readonly, *writable]:
        candidate = Path(path)
        if path == "/" or candidate.resolve() != candidate or not candidate.exists():
            raise AdmissionRefusal(
                "node filesystem destination is absent or noncanonical"
            )
    if any(
        _destination_within(path, writable)
        for path in ("/usr", "/etc", "/bin", "/lib", "/lib64", "/proc", "/dev")
    ):
        raise AdmissionRefusal("node administrative grants expose platform writes")
    # Broad accepted project/admin grants are mounted first. Exact configuration,
    # runtime and producer inputs overlay them read-only.
    mounts: dict[str, bool] = {path: False for path in readonly}
    mounts.update({path: True for path in writable})
    for path in readonly[len(scope["read"]) :]:
        mounts[path] = False
    exact = {
        data["runtime"][name]["path"]
        for name in ("launcher", "node", "consumer", "sdk", "sdk_package", "native")
    }
    exact.update(item["path"] for item in data["configuration"])
    exact.update(
        identity["path"]
        for item in [*data["mcp"], *data["producer"]]
        for identity in item["executables"]
    )
    for root in roots:
        for location in tuple(mounts):
            if Path(location).is_relative_to(root) and location not in exact:
                del mounts[location]
        mounts[root] = False
    if "/usr" in scope["read"]:
        for location in tuple(mounts):
            if (
                location != "/usr"
                and Path(location).is_relative_to("/usr")
                and location not in exact
            ):
                del mounts[location]
    # Another socket can sit beside a granted one (the node host's owner door
    # does): no other mount may show its directory.
    sockets = [path for path in scope["read"] if Path(path).is_socket()]
    if any(
        Path(door).parent.is_relative_to(path)
        for door in sockets
        for path in mounts
        if path not in sockets
    ):
        raise AdmissionRefusal("a grant shows a socket's directory")
    parents = {
        str(parent)
        for path in mounts
        for parent in Path(path).parents
        if str(parent) != "/"
    }
    argv = [
        bwrap,
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--cap-drop",
        "ALL",
        # Nested native bubblewrap prepares its root at /tmp, independently of
        # TMPDIR. Give it a private empty mount, never the host's temporary files.
        "--tmpfs",
        "/tmp",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
    ]
    for parent in sorted(parents, key=lambda path: (path.count("/"), path)):
        if parent not in {"/proc", "/dev"}:
            argv.extend(("--dir", parent))
    for path in sorted(mounts, key=lambda path: (path.count("/"), path)):
        if configuration is not None and path in configuration:
            argv.extend(("--ro-bind", str(configuration[path]), path))
        elif pinned is not None and path in pinned:
            mode = stat.S_IMODE(os.fstat(pinned[path]).st_mode)
            argv.extend(
                ("--perms", f"{mode:04o}", "--ro-bind-data", str(pinned[path]), path)
            )
        else:
            argv.extend(("--bind" if mounts[path] else "--ro-bind", path, path))
    # Platform loader symlinks are immutable aliases of the admitted /usr
    # runtime. Bubblewrap preserves the launcher's explicit pass_fds unchanged;
    # no path copy or descriptor recreation occurs inside the cage.
    if "/usr" in mounts:
        for alias in ("/bin", "/lib", "/lib64"):
            source = Path(alias)
            if source.is_symlink() and source.resolve().is_relative_to("/usr"):
                argv.extend(("--symlink", os.readlink(source), alias))
    if descriptor < 3:
        raise AdmissionRefusal("the sealed profile descriptor is not private")
    argv.extend(("--chdir", data["cwd"]))
    proxy = None
    command = consumer_argv
    if scope["network"]:
        bridge = next(
            (item for item in data["producer"] if item["id"] == "network-bridge"), None
        )
        bootstrap = next(
            (item for item in data["producer"] if item["id"] == "network-bootstrap"),
            None,
        )
        endpoint = (
            None
            if bridge is None or len(bridge["arguments"]) != 2
            else bridge["arguments"][1]
        )
        if not isinstance(endpoint, str) or not endpoint.startswith("UNIX-CONNECT:/"):
            raise AdmissionRefusal("private egress endpoint was not admitted")
        proxy = Path(endpoint.removeprefix("UNIX-CONNECT:"))
        if len(os.fsencode(proxy)) >= 108 or not _destination_within(
            str(proxy), scope["administrative"]
        ):
            raise AdmissionRefusal("private egress port exceeds its admitted boundary")
        if (
            bridge is None
            or bootstrap is None
            or bridge["arguments"]
            != [
                "TCP4-LISTEN:39871,bind=127.0.0.1,reuseaddr,fork",
                "UNIX-CONNECT:" + str(proxy),
            ]
            or bootstrap["arguments"] != ["-c", NODE_BOOTSTRAP]
        ):
            raise AdmissionRefusal("network bootstrap producers were not admitted")
        command = (
            bootstrap["command"],
            "-c",
            NODE_BOOTSTRAP,
            bridge["command"],
            str(proxy),
            *consumer_argv,
        )
        env.update(
            {
                "HTTP_PROXY": "http://127.0.0.1:39871",
                "HTTPS_PROXY": "http://127.0.0.1:39871",
                "ALL_PROXY": "http://127.0.0.1:39871",
                "NO_PROXY": "",
            }
        )
    argv.extend(("--", *command))
    return NodeLaunch(tuple(argv), env, proxy)
