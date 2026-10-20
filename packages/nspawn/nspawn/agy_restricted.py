"""Passive composition of AGY's kernel-enforced native process boundary.

The selected executable and account HOME remain at their installed paths.
Only the checkout and explicitly admitted administrative destinations may be
writable. Every descendant inherits the mount, privilege and PID boundary;
the caller still supplies the owning AggregateScope and process custody.
This does not establish vendor compatibility or lift AGY admission refusal.
"""

from __future__ import annotations

import os
import shutil
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .claude_restricted import RUNTIME_PATH
from .errors import NERR_1116, NSpawnError


class RestrictedLaunchRefused(NSpawnError):
    """An AGY process boundary cannot be established before native execution."""

    def __init__(self, detail: str) -> None:
        super().__init__(NERR_1116, f"agy restricted launch: {detail}")


@dataclass(frozen=True, slots=True)
class RestrictedLaunch:
    """Exact native command/environment and its enforced filesystem grants.

    Read-only child mounts, including Git metadata and installed inputs,
    override any enclosing writable destination. No credential enters repr.
    """

    argv: tuple[str, ...]
    env: dict[str, str] = field(repr=False)
    cwd: Path
    read_paths: tuple[Path, ...]
    write_paths: tuple[Path, ...]


def _canonical(path: Path, *, directory: bool) -> Path:
    try:
        if (
            not path.is_absolute()
            or path == Path("/")
            or path.resolve(strict=True) != path
        ):
            raise RestrictedLaunchRefused("destination is absent or noncanonical")
        mode = path.lstat().st_mode
        expected = stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)
        if not expected:
            raise RestrictedLaunchRefused("destination has an unsupported file type")
    except (OSError, RuntimeError) as error:
        raise RestrictedLaunchRefused("destination cannot be inspected") from error
    return path


def _private(path: Path) -> Path:
    _canonical(path, directory=True)
    metadata = path.stat()
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise RestrictedLaunchRefused("administrative destination is not private")
    return path


def _overlaps(left: Path, right: Path) -> bool:
    return left.is_relative_to(right) or right.is_relative_to(left)


def _git_paths(checkout: Path, read_paths: tuple[Path, ...]) -> tuple[Path, ...]:
    git = checkout / ".git"
    try:
        metadata = git.lstat()
    except FileNotFoundError:
        return ()
    except OSError as error:
        raise RestrictedLaunchRefused("Git metadata cannot be inspected") from error
    if stat.S_ISDIR(metadata.st_mode):
        return (_canonical(git, directory=True),)
    if not stat.S_ISREG(metadata.st_mode):
        raise RestrictedLaunchRefused("Git metadata is neither a directory nor a file")
    try:
        pointer = git.read_text()
        if not pointer.startswith("gitdir: "):
            raise RestrictedLaunchRefused("linked Git metadata is invalid")
        own = (checkout / pointer[8:].strip()).resolve(strict=True)
        common_file = own / "commondir"
        common = (
            (own / common_file.read_text().strip()).resolve(strict=True)
            if common_file.exists()
            else own
        )
    except (OSError, RuntimeError, UnicodeError) as error:
        raise RestrictedLaunchRefused(
            "linked Git metadata cannot be resolved"
        ) from error
    linked = tuple(dict.fromkeys((own, common)))
    for directory in linked:
        _canonical(directory, directory=True)
        if not any(directory.is_relative_to(root) for root in read_paths):
            raise RestrictedLaunchRefused(
                "linked Git metadata has no admitted read root"
            )
    return (git, *linked)


def compose_restricted_launch(
    *,
    argv: tuple[str, ...],
    env: Mapping[str, str],
    checkout: Path,
    home: Path,
    run_dir: Path,
    scratch: Path,
    writable: bool,
    read_paths: tuple[Path, ...] = (),
    administrative_paths: tuple[Path, ...] = (),
    runtime_paths: tuple[Path, ...] = (),
    runtime_aliases: tuple[tuple[Path, Path], ...] = (),
    customization: Path | None = None,
    sockets: tuple[Path, ...] = (),
) -> RestrictedLaunch:
    """Wrap already selected native inputs without launching or copying them.

    HOME is read-only; native state writes require explicit administrative
    subdirectories. Certificate/DNS/configuration inputs outside /usr must be
    named by the caller. An explicit canonical source may supply the host's
    existing /etc/resolv.conf symlink read-only. No host passwd, bus or cgroup
    tree is mounted.
    Native network access remains the selected client's existing egress;
    filesystem and process isolation never rely on the vendor sandbox flag.
    A private `customization` tree is overlaid read-only on HOME's existing
    `.gemini`, which keeps every original entry; each of `sockets` must
    be this account's own socket and is the only entry shown of its directory.
    """
    if (
        type(writable) is not bool
        or not argv
        or any(
            not isinstance(argument, str) or not argument or "\x00" in argument
            for argument in argv
        )
    ):
        raise RestrictedLaunchRefused("native argv or writable grant is invalid")
    binary = _canonical(Path(argv[0]), directory=False)
    if not os.access(binary, os.X_OK):
        raise RestrictedLaunchRefused("selected native executable is not executable")
    _canonical(checkout, directory=True)
    _canonical(home, directory=True)
    metadata = home.stat()
    if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022:
        raise RestrictedLaunchRefused("native account home is not owner-controlled")
    for directory in (run_dir, scratch):
        _private(directory)
    if any(
        not isinstance(name, str)
        or not name
        or "=" in name
        or "\x00" in name
        or not isinstance(value, str)
        or "\x00" in value
        for name, value in env.items()
    ):
        raise RestrictedLaunchRefused("native environment is invalid")
    if env.get("HOME") != str(home) or env.get("TMPDIR") != str(scratch):
        raise RestrictedLaunchRefused(
            "native state differs from its admitted destinations"
        )
    for path in (*read_paths, *runtime_paths):
        _canonical(path, directory=path.is_dir())
    for path in sockets:
        try:
            if not path.is_absolute() or path.resolve(strict=True) != path:
                raise RestrictedLaunchRefused("MCP socket is absent or noncanonical")
            metadata = path.lstat()
        except (OSError, RuntimeError) as error:
            raise RestrictedLaunchRefused("MCP socket cannot be inspected") from error
        if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise RestrictedLaunchRefused("MCP endpoint is not this account's socket")
    gemini = home / ".gemini"
    if customization is not None:
        _private(customization)
        if not gemini.is_dir() or gemini.is_symlink():
            raise RestrictedLaunchRefused("the account has no AGY state directory")
        # A staged directory would hide a linked or non-directory original.
        for path in (gemini / "config", gemini / "config/plugins"):
            if path.is_symlink() or (path.exists() and not path.is_dir()):
                raise RestrictedLaunchRefused(f"AGY {path.name} is not a directory")
    aliases: dict[Path, Path] = {}
    for source, destination in runtime_aliases:
        _canonical(source, directory=False)
        try:
            if (
                destination != Path("/etc/resolv.conf")
                or destination in aliases
                or not destination.is_symlink()
                or destination.resolve(strict=True) != source
            ):
                raise RestrictedLaunchRefused("DNS alias differs from its source")
        except (OSError, RuntimeError) as error:
            raise RestrictedLaunchRefused("DNS alias cannot be inspected") from error
        aliases[destination] = source
    for path in administrative_paths:
        _private(path)
    administration = tuple(dict.fromkeys((run_dir, scratch, *administrative_paths)))
    platform = tuple(
        Path(path) for path in ("/usr", "/etc", "/proc", "/dev", "/sys", "/run")
    )
    for destination in administration:
        if (
            _overlaps(destination, checkout)
            or destination == home
            or any(_overlaps(destination, root) for root in platform)
        ):
            raise RestrictedLaunchRefused(
                "administrative grant exposes project or host control"
            )
    if any(_overlaps(checkout, root) for root in platform):
        raise RestrictedLaunchRefused("checkout exposes platform writes")
    git = _git_paths(checkout, read_paths)
    if any(
        _overlaps(path, destination) for path in git for destination in administration
    ):
        raise RestrictedLaunchRefused("administrative grant exposes Git metadata")
    bwrap = shutil.which("bwrap", path=RUNTIME_PATH)
    if bwrap is None:
        raise RestrictedLaunchRefused("kernel confinement is unavailable")
    readonly = tuple(
        dict.fromkeys(
            (
                Path("/usr"),
                home,
                *((checkout,) if not writable else ()),
                *read_paths,
                binary,
                *runtime_paths,
                *aliases.values(),
                *aliases,
                *git,
                *sockets,
            )
        )
    )
    writes = tuple(dict.fromkeys((*((checkout,) if writable else ()), *administration)))
    mounts = {path: False for path in readonly}
    mounts[checkout] = writable
    mounts.update({path: True for path in administration})
    for path in (binary, *runtime_paths, *git, *sockets):
        mounts[path] = False
    if customization is not None and any(
        path.is_relative_to(gemini) for path in mounts
    ):
        raise RestrictedLaunchRefused("a grant lies under the overlaid AGY state")
    # Another socket can sit beside each one (the node host's owner door does).
    if any(
        door.parent.is_relative_to(path)
        for door in sockets
        for path in mounts
        if path not in sockets
    ):
        raise RestrictedLaunchRefused("a grant shows an MCP socket's directory")
    parents = {
        parent for path in mounts for parent in path.parents if parent != Path("/")
    }
    command = [
        bwrap,
        "--unshare-all",
        "--share-net",
        "--die-with-parent",
        "--new-session",
        "--cap-drop",
        "ALL",
        "--tmpfs",
        "/tmp",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
    ]
    for parent in sorted(parents, key=lambda path: (len(path.parts), str(path))):
        if parent not in (Path("/proc"), Path("/dev")):
            command.extend(("--dir", str(parent)))
    for path in sorted(mounts, key=lambda path: (len(path.parts), str(path))):
        command.extend(
            (
                "--bind" if mounts[path] else "--ro-bind",
                str(aliases.get(path, path)),
                str(path),
            )
        )
    if customization is not None:
        # After HOME's own mount; the last source is the top layer, so the
        # session's entries win and every original entry stays beneath them.
        command.extend(
            (
                "--overlay-src",
                str(gemini),
                "--overlay-src",
                str(customization),
                "--ro-overlay",
                str(gemini),
            )
        )
    for alias in ("/bin", "/lib", "/lib64"):
        source = Path(alias)
        if source.is_symlink() and source.resolve().is_relative_to("/usr"):
            command.extend(("--symlink", os.readlink(source), alias))
        elif source.exists():
            raise RestrictedLaunchRefused("platform loader alias is unsupported")
    command.extend(("--remount-ro", "/", "--chdir", str(checkout), "--", *argv))
    return RestrictedLaunch(tuple(command), dict(env), checkout, readonly, writes)
