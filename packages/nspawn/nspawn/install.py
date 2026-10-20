"""`nspawn skill install` and `nspawn plugin install`.

Only nSPAWN runs the vendor CLIs: `npx skills` and `claude plugin`.
A check runs the same vendor command as the install, in a staging directory that
has its own HOME, `CLAUDE_CONFIG_DIR` and `XDG_STATE_HOME`, and shows what it
wrote. The staging directory is deleted when the check ends. An install runs the
vendor command on the real target.

NOTT never replaces, updates or removes an installed skill or plugin. A vendor call
gets a minimal environment with no token and no gh or git credential.

`admit` is the one admission rule for installed Claude content. The check, the
install and the node profile (`profile_prepare`) all use it. The readers
(`installed`, `claude_roots`) read files only and start no process.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from types import FrameType
from typing import Any, Final

#: The npx skills release NOTT runs. A new release is a new measurement.
SKILLS: Final = "skills@1.7.2"
#: The time all the vendor calls of one verb can take, in seconds.
DEADLINE: Final = 240
#: The most one text of a check answer holds, and the most the answer holds.
TEXT_LIMIT: Final = 64 * 1024
ANSWER_LIMIT: Final = 512 * 1024

NAME: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
#: A source NOTT cannot reach without an ssh key: an scp-like `user@host:path`.
SCP: Final = re.compile(r"[^/\s]+@[^/\s]+:")
#: The name of the local plugin that holds the Claude skills.
SKILLS_PLUGIN: Final = "installed"

#: What an installed plugin root may hold (`admit`). It is an allow-list: a name
#: that a vendor adds later is refused until NOTT admits it.
DIRECTORIES: Final = ("skills", "commands", "agents")
FILES: Final = ("README", "LICENSE", "CHANGELOG")
KEYS: Final = frozenset(
    {
        "name",
        "version",
        "description",
        "author",
        "homepage",
        "repository",
        "license",
        "keywords",
        "commands",
        "agents",
        "skills",
    }
)
#: A marketplace entry also names where its files are, and catalog metadata.
ENTRY_KEYS: Final = KEYS | {"source", "strict", "category", "tags"}
#: A front matter key NOTT can read: a plain name, or a quoted one with no escape.
KEY: Final = re.compile(
    r"""(?:"([^"\\]*)"|'([^']*)'|([A-Za-z_][A-Za-z0-9_-]*))\s*:(?:\s|$)"""
)
RECIPE_KEY: Final = "nott-kind"
RECIPE_KIND: Final = "recipe"


class InstallRefused(Exception):
    """A refusal (exit 2). The message is the owner's text."""


class InstallFailed(Exception):
    """A vendor call that failed (exit 1). The message holds the vendor's stderr."""


class NotAdmitted(InstallRefused):
    """`admit` refused an item; `what` names the path or key."""

    def __init__(self, item: str, what: str, front: str | None = None) -> None:
        self.item = item
        self.what = what
        super().__init__(
            f"{item}/{front} declares hooks in its front matter; NOTT nodes run only "
            "the hooks of their own plugin"
            if front is not None
            else f"{item} holds {what}; NOTT nodes admit only skills, commands and "
            "agents from an installed plugin"
        )


# Paths ---------------------------------------------------------------------------
# Each one reads `Path.home()` when it is called, never at import.


def node_directory() -> Path:
    return Path.home() / ".nott" / "node"


def claude_skills() -> Path:
    """The local plugin "installed": the `CLAUDE_CONFIG_DIR` of the Claude skills."""
    return node_directory() / "claude-skills"


def claude_config() -> Path:
    """The `CLAUDE_CONFIG_DIR` of the Claude plugins that NOTT installs."""
    return node_directory() / "claude"


def skills_target(provider: str) -> Path:
    if provider == "claude":
        return claude_skills() / "skills"
    return Path.home() / ".agents" / "skills"


def private(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


# Requests ------------------------------------------------------------------------


def provider_check(kind: str, provider: str) -> None:
    if kind == "skill":
        if provider == "agy":
            raise InstallRefused(
                "agy reads global skills from ~/.gemini/config/skills, and npx skills "
                "writes no AGY skill there; NOTT installs no AGY skill"
            )
        if provider not in ("claude", "codex"):
            raise InstallRefused(
                "skill install takes provider claude or codex; qwen nodes load what "
                "is installed for claude"
            )
        return
    if provider == "codex":
        raise InstallRefused(
            "a codex plugin install or marketplace upgrade deletes the version that "
            "open Codex sessions use; NOTT installs no Codex plugin"
        )
    if provider == "agy":
        raise InstallRefused(
            "agy plugin install takes only a directory; NOTT installs no AGY plugin"
        )
    if provider != "claude":
        raise InstallRefused(
            "plugin install takes provider claude; qwen nodes load what is installed "
            "for claude"
        )


def source_check(source: str) -> None:
    if source.startswith("-") or any(ord(c) < 32 or ord(c) == 127 for c in source):
        raise InstallRefused(
            'a source must not start with "-" or hold a control character'
        )
    scheme = source.partition("://")[0] if "://" in source else None
    if (scheme is not None and scheme != "https") or (
        scheme is None and SCP.match(source) is not None
    ):
        raise InstallRefused(
            "a source must be an https URL, an owner/repo name or a local path; NOTT "
            "sends no ssh key"
        )


def deadline_check(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise InstallFailed(f"The install did not finish in {DEADLINE} s.")


def links_check(source: str, deadline: float | None = None) -> None:
    """A local source holds no symbolic link: a vendor copies the file that a link
    names, which can be any file this user can read."""
    root = Path(source).expanduser()
    if "://" in source or not root.is_dir():
        return
    for parent, directories, names in os.walk(root, followlinks=False):
        deadline_check(deadline)
        for name in sorted([*directories, *names]):
            path = Path(parent) / name
            if path.is_symlink():
                raise NotAdmitted(source, f"the symbolic link {path.relative_to(root)}")


def name_check(name: str) -> None:
    if NAME.fullmatch(name) is None:
        raise InstallRefused(
            'a skill or plugin name holds only letters, digits, ".", "_" and "-"'
        )


# Admission -----------------------------------------------------------------------


def frontmatter_keys(path: Path) -> list[str] | None:
    """The top-level keys of a Markdown file's front matter. None when a top-level
    line is not a plain key, because a key can hide in it."""
    try:
        lines = path.read_text("utf-8").removeprefix("\ufeff").splitlines()
    except OSError, UnicodeError:
        return None
    if (
        lines
        and lines[0].replace("\ufeff", "").lstrip().startswith("---")
        and not lines[0].isascii()
    ):
        return None
    if not lines or lines[0].rstrip() != "---":
        return []
    keys: list[str] = []
    for line in lines[1:]:
        if line.rstrip() in ("---", "..."):
            return keys
        if not keys and line[:1] in (" ", "\t") and line.strip()[:1] not in ("", "#"):
            return None  # an indented mapping: its keys are top-level keys too
        if not line.strip() or line[0] in " \t#" or line.startswith("- "):
            continue
        match = KEY.match(line)
        if match is None:
            return None
        keys.append(next(group for group in match.groups() if group is not None))
    return None


def metadata_read(path: Path) -> dict[str, str]:
    """Read the limited Agent Skill metadata string map for scan and host."""
    refusal = (
        "recipe metadata must contain unique string keys and values: "
        + path.parent.name
    )
    try:
        with path.open("rb") as source:
            raw = source.read(TEXT_LIMIT + 1)
        header = raw.removeprefix(b"\xef\xbb\xbf").splitlines(keepends=True)
        if not header or header[0].rstrip() != b"---":
            return {}
        parts = [header[0]]
        for raw_line in header[1:]:
            parts.append(raw_line)
            if raw_line.rstrip() in {b"---", b"..."}:
                break
        raw = b"".join(parts)
        if len(raw) > TEXT_LIMIT:
            raise InstallRefused(refusal)
        lines = raw.decode("utf-8", "strict").removeprefix("\ufeff").splitlines()
    except FileNotFoundError:
        return {}
    except UnicodeError as error:
        raise InstallRefused(refusal) from error
    if not lines or lines[0].rstrip() != "---":
        return {}
    values: dict[str, str] = {}
    seen: set[str] = set()
    mapping = False
    for line in lines[1:]:
        if line.rstrip() in {"---", "..."}:
            return values
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0] in " \t":
            if not mapping:
                continue
            if not line.startswith("  ") or line[2:3] in " \t":
                raise InstallRefused(refusal)
            entry = KEY.match(line[2:])
            if entry is None:
                raise InstallRefused(refusal)
            key = next(group for group in entry.groups() if group is not None)
            scalar = line[2 + entry.end() :].strip()
            try:
                if scalar.startswith('"'):
                    value, end = json.JSONDecoder().raw_decode(scalar)
                    rest = scalar[end:].strip()
                    if rest and not rest.startswith("#"):
                        raise ValueError
                elif scalar.startswith("'"):
                    quoted = re.fullmatch(r"'((?:[^']|'')*)'\s*(?:#.*)?", scalar)
                    if quoted is None:
                        raise ValueError
                    value = quoted[1].replace("''", "'")
                else:
                    if not scalar or scalar[0] in "[{|>!&*":
                        raise ValueError
                    value = re.split(r"\s+#", scalar, maxsplit=1)[0].rstrip()
                if (
                    key in values
                    or not isinstance(value, str)
                    or any(ord(character) < 32 for character in value)
                ):
                    raise ValueError
            except ValueError as error:
                raise InstallRefused(refusal) from error
            values[key] = value
            continue
        mapping = False
        entry = KEY.match(line)
        if entry is None:
            raise InstallRefused(refusal)
        key = next(group for group in entry.groups() if group is not None)
        if key in seen:
            raise InstallRefused(refusal)
        seen.add(key)
        if key == "metadata":
            rest = line[entry.end() :].strip()
            if rest and not rest.startswith("#"):
                raise InstallRefused(refusal)
            mapping = True
    raise InstallRefused(refusal)


def paths_check(item: str, document: Mapping[str, Any], label: str) -> None:
    """Each `commands`, `agents` and `skills` value is a relative path that stays in
    one of the allowed directories."""
    for key in DIRECTORIES:
        value = document.get(key)
        if value is None:
            continue
        for entry in value if isinstance(value, list) else [value]:
            parts = (
                [part for part in PurePosixPath(entry).parts if part != "."]
                if isinstance(entry, str) and not entry.startswith("/")
                else []
            )
            if not parts or ".." in parts or parts[0] not in DIRECTORIES:
                raise NotAdmitted(
                    item, f"the {key} path {json.dumps(entry)} in {label}"
                )


def keys_check(
    item: str, document: Any, allowed: frozenset[str], label: str
) -> Mapping[str, Any]:
    if not isinstance(document, dict):
        raise NotAdmitted(item, f"{label} that is not a JSON object")
    for key in sorted(set(document) - allowed):
        raise NotAdmitted(item, f"the key {key} in {label}")
    paths_check(item, document, label)
    return document


def admit(
    root: Path,
    entry: Mapping[str, Any] | None = None,
    item: str = "",
    *,
    deadline: float | None = None,
) -> None:
    """Admit an installed plugin root that holds only skills, commands and agents.

    `entry` is the plugin's inline marketplace entry, when there is one. Claude Code
    loads hooks, MCP and LSP servers, monitors, `bin/`, settings and output styles
    from a plugin root, and it can learn a new kind later. This is an allow-list,
    so a kind that nobody named yet is refused too."""
    item = item or root.name
    if root.is_symlink():
        raise NotAdmitted(item, "a symbolic link at its root")
    for parent, directories, names in os.walk(root, followlinks=False):
        deadline_check(deadline)
        for name in sorted([*directories, *names]):
            path = Path(parent) / name
            if path.is_symlink():
                raise NotAdmitted(item, f"the symbolic link {path.relative_to(root)}")
    for name in sorted(os.listdir(root)):
        path = root / name
        if (name in DIRECTORIES and path.is_dir()) or (
            name.startswith(FILES) and path.is_file()
        ):
            continue
        if name != ".claude-plugin" or not path.is_dir():
            raise NotAdmitted(item, name)
        for inner in sorted(os.listdir(path)):
            if inner != "plugin.json" or not (path / inner).is_file():
                raise NotAdmitted(item, f".claude-plugin/{inner}")
        label = ".claude-plugin/plugin.json"
        try:
            document = json.loads((path / "plugin.json").read_text("utf-8"))
        except (OSError, ValueError) as error:
            raise NotAdmitted(item, f"{label} that is not JSON") from error
        keys_check(item, document, KEYS, label)
    if entry is not None:
        keys_check(item, entry, ENTRY_KEYS, "its marketplace entry")
    for document in sorted(root.rglob("*.md")):
        deadline_check(deadline)
        relative = str(document.relative_to(root))
        keys = frontmatter_keys(document)
        if keys is None:
            raise NotAdmitted(item, f"front matter that NOTT cannot read in {relative}")
        if "hooks" in keys:
            raise NotAdmitted(
                item, f"hooks in the front matter of {relative}", front=relative
            )


# Vendor processes ----------------------------------------------------------------


def environment(home: Path, gh: Path, **extra: str) -> dict[str, str]:
    """The environment of a vendor call: no token, and no gh or git credential."""
    found = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(home),
        "TMPDIR": os.environ.get("TMPDIR", "/var/tmp"),
        "GH_CONFIG_DIR": str(gh),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ALLOW_PROTOCOL": "https",
        "npm_config_ignore_scripts": "true",
        "npm_config_userconfig": "/dev/null",
        # A clone writes a symbolic link as a file that holds its target path.
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.symlinks",
        "GIT_CONFIG_VALUE_0": "false",
        "DISABLE_TELEMETRY": "1",
        "DO_NOT_TRACK": "1",
        "NO_COLOR": "1",
    }
    if "LANG" in os.environ:
        found["LANG"] = os.environ["LANG"]
    found.update(extra)
    return found


class Vendor:
    """The vendor calls of one verb: one deadline for all of them. Each child holds
    the install lock too, so that the lock lives as long as any of them."""

    def __init__(self, lock: int, work: Path, deadline: float) -> None:
        self.lock = lock
        self.work = work
        self.deadline = deadline

    def __call__(self, command: list[str], env: Mapping[str, str]) -> Any:
        """The JSON that the call printed. A call that exits with a code other than
        0 is an `InstallFailed` that holds its stderr."""
        found = shutil.which(command[0], path=env["PATH"])
        if found is None:
            raise InstallRefused(f"{command[0]} is not installed on this host")
        deadline_check(self.deadline)
        stopped = False

        def stop_pending(_number: int, _frame: FrameType | None) -> None:
            nonlocal stopped
            stopped = True

        prior = signal.signal(signal.SIGTERM, stop_pending)
        try:
            try:
                process = subprocess.Popen(
                    [found, *command[1:]],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=dict(env),
                    start_new_session=True,
                    pass_fds=(self.lock,),
                )
            except OSError as error:
                reason = "The host stopped the install. " if stopped else ""
                raise InstallFailed(
                    f"{reason}{command[0]} could not start: {error}"
                ) from error
            try:
                signal.signal(signal.SIGTERM, prior)
                if stopped:
                    raise InstallFailed("The host stopped the install.")
                out, err = process.communicate(
                    timeout=max(0.0, self.deadline - time.monotonic())
                )
            except (subprocess.TimeoutExpired, InstallFailed) as error:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate()
                if isinstance(error, InstallFailed):
                    raise
                raise InstallFailed(
                    f"{' '.join(command[:4])} did not finish in {DEADLINE} s; NOTT killed "
                    "its process group"
                ) from None
        finally:
            signal.signal(signal.SIGTERM, prior)
        if process.returncode != 0:
            raise InstallFailed(
                f"{' '.join(command[:4])} exited {process.returncode}: "
                + (err.strip() or out.strip())[-4096:]
            )
        try:
            return json.loads(out)
        except ValueError:
            raise InstallFailed(
                f"{' '.join(command[:4])} exited 0 but printed no JSON"
            ) from None


@contextmanager
def vendor(deadline: float) -> Iterator[Vendor]:
    """One check or install at a time on this host, in a work directory under
    TMPDIR that is deleted when the verb ends, also after a failure."""
    descriptor = os.open(
        private(node_directory()) / "install.lock", os.O_RDWR | os.O_CREAT, 0o600
    )
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise InstallRefused(
                "an install is running on this host; wait for its result"
            ) from None
        work = Path(
            tempfile.mkdtemp(prefix="nspawn-install.", dir=os.environ.get("TMPDIR"))
        )
        try:
            private(work / "gh")
            deadline_check(deadline)

            def install_stop(_number: int, _frame: FrameType | None) -> None:
                raise InstallFailed("The host stopped the install.")

            prior = signal.signal(signal.SIGTERM, install_stop)
            try:
                yield Vendor(descriptor, work, deadline)
            finally:
                signal.signal(signal.SIGTERM, prior)
        finally:
            shutil.rmtree(work, ignore_errors=True)
    finally:
        os.close(descriptor)


def staged(work: Path) -> dict[str, str]:
    """The HOME, `CLAUDE_CONFIG_DIR` and `XDG_STATE_HOME` of a check."""
    return {
        "HOME": str(private(work / "home")),
        "CLAUDE_CONFIG_DIR": str(private(work / "claude")),
        "XDG_STATE_HOME": str(private(work / "state")),
    }


def remove_written(paths: list[Path]) -> None:
    errors = []
    for path in paths:
        try:
            if stat.S_ISDIR(path.lstat().st_mode):
                shutil.rmtree(path)
            else:
                path.unlink()
        except FileNotFoundError:
            pass
        except OSError as error:
            errors.append(f"{path}: {error}")
    if errors:
        raise InstallFailed("NOTT cleanup failed: " + ". ".join(errors))


# Check answers -------------------------------------------------------------------


def shown(tree: Path, deadline: float | None = None) -> dict[str, Any]:
    """The files of `tree`, and the text of each SKILL.md, plugin.json, command and
    agent."""
    files: list[str] = []
    for parent, _, names in os.walk(tree, followlinks=False):
        deadline_check(deadline)
        files.extend(str((Path(parent) / name).relative_to(tree)) for name in names)
    files.sort()
    texts = []
    for path in files:
        deadline_check(deadline)
        parts = PurePosixPath(path).parts
        if (
            parts[-1] == "SKILL.md"
            or path == ".claude-plugin/plugin.json"
            or (parts[0] in ("commands", "agents") and path.endswith(".md"))
        ):
            with (tree / path).open("rb") as stream:
                data = stream.read(TEXT_LIMIT + 1)
            texts.append(
                {
                    "path": path,
                    "text": data[:TEXT_LIMIT].decode("utf-8", "replace"),
                    "truncated": len(data) > TEXT_LIMIT,
                }
            )
    return {"files": files, "texts": texts}


def reach(provider: str) -> str:
    if provider == "codex":
        return (
            "Every Codex process on this host that uses your home reads it. This "
            "includes nodes, nSPAWN runs, reviews and your own Codex runs. AGY processes "
            "with your home as their workspace read it too."
        )
    return (
        "Claude and Qwen nodes on this host load it from their next turn. Audit "
        "parents and the Guará centre do not, and ~/.claude does not change."
    )


def answer(
    kind: str,
    provider: str,
    source: str,
    target: Path,
    items: list[dict[str, Any]],
    deadline: float | None = None,
) -> dict[str, Any]:
    found = {
        "provider": provider,
        "kind": kind,
        "scope": "user",
        "source": source,
        "target": str(target),
        "reach": reach(provider),
        "items": items,
    }
    if len(json.dumps(found).encode()) > ANSWER_LIMIT:
        raise InstallRefused(
            f"{source} holds too much to show in {ANSWER_LIMIT // 1024} KiB; NOTT "
            "installs only what it can show"
        )
    deadline_check(deadline)
    return found


# Skills --------------------------------------------------------------------------


def skill_command(source: str, provider: str, names: list[str]) -> list[str]:
    agent = "claude-code" if provider == "claude" else "codex"
    command = ["npx", "-y", SKILLS, "add", source, "-g", "-a", agent]
    command += ["--copy", "-y", "--json"]
    for name in names:
        command += ["--skill", name]
    return command


def skill_install(
    provider: str, source: str, check: bool, names: list[str]
) -> dict[str, Any]:
    deadline = time.monotonic() + DEADLINE
    provider_check("skill", provider)
    source_check(source)
    for name in names:
        name_check(name)
    if not check and not names:
        raise InstallRefused("install takes the skill names its check showed")
    links_check(source, deadline)
    target = skills_target(provider)
    with vendor(deadline) as call:
        if check:
            env = environment(Path.home(), call.work / "gh", **staged(call.work))
            rows = call(skill_command(source, provider, names), env)
            root = (
                call.work / "claude"
                if provider == "claude"
                else call.work / "home" / ".agents"
            )
            admit(root, item=source, deadline=deadline)
            hashes = {
                Path(row["path"]).name: row.get("hash")
                for row in (rows if isinstance(rows, list) else [])
                if isinstance(row, dict) and isinstance(row.get("path"), str)
            }
            items = []
            for tree in (
                sorted((root / "skills").iterdir())
                if (root / "skills").is_dir()
                else []
            ):
                name_check(tree.name)
                digest = hashes.get(tree.name)
                items.append(
                    {
                        "name": tree.name,
                        "version": None,
                        "hash": digest if isinstance(digest, str) else None,
                    }
                    | shown(tree, deadline)
                )
            return answer("skill", provider, source, target, items, deadline)
        for name in names:
            if os.path.lexists(target / name):
                raise InstallRefused(
                    f"skill {name} already exists at {target / name}; NOTT does not "
                    "replace an installed skill"
                )
        # Only a Codex install needs the user's HOME: its target is in it.
        home, extra = Path.home(), {}
        if provider == "claude":
            root = private(claude_skills())
            manifest = private(root / ".claude-plugin") / "plugin.json"
            if not manifest.exists():
                manifest.write_text(json.dumps({"name": SKILLS_PLUGIN}) + "\n")
            home = private(call.work / "home")
            extra = {
                "CLAUDE_CONFIG_DIR": str(root),
                "XDG_STATE_HOME": str(private(node_directory() / "state")),
            }
        env = environment(home, call.work / "gh", **extra)
        # A failed call can still have written: admission runs after it either way.
        failure: InstallFailed | None = None
        try:
            rows = call(skill_command(source, provider, names), env)
        except InstallFailed as error:
            failure = error
        if provider == "claude":
            try:
                admit(claude_skills(), item=SKILLS_PLUGIN, deadline=deadline)
            except NotAdmitted as error:
                # A source that changed after its check: what it wrote is new.
                written = [
                    target / name for name in names if os.path.lexists(target / name)
                ]
                refusal = f"{', '.join(names)} as installed holds {error.what}"
                try:
                    remove_written(written)
                except InstallFailed as cleanup:
                    causes = ". ".join(
                        str(cause)
                        for cause in (failure, refusal, cleanup)
                        if cause is not None
                    )
                    raise InstallFailed(causes) from cleanup
                refusal += "; NOTT removed " + ", ".join(str(path) for path in written)
                if failure is not None:
                    raise InstallFailed(f"{failure}. {refusal}") from error
                raise InstallRefused(refusal) from error
            except (InstallFailed, OSError) as error:
                failure = InstallFailed(
                    f"{failure}. {error}" if failure is not None else str(error)
                )
        try:
            deadline_check(deadline)
        except InstallFailed as error:
            if failure is None:
                failure = error
        if failure is not None:
            try:
                remove_written([target / name for name in names])
            except InstallFailed as cleanup:
                raise InstallFailed(f"{failure}. {cleanup}") from cleanup
            raise failure
        missing = [str(target / name) for name in names if not (target / name).is_dir()]
        if missing:
            raise InstallFailed(
                f"npx skills add exited 0 but wrote no {', '.join(missing)}"
            )
        return {"installed": rows}


# Plugins -------------------------------------------------------------------------


def read_json(path: Path) -> dict[str, Any]:
    """A Claude record file as a JSON object."""
    try:
        document = json.loads(path.read_text("utf-8"))
    except OSError as error:
        raise InstallFailed(
            f"the Claude plugin record at {path} cannot be read: {error.strerror}"
        ) from error
    except ValueError as error:
        raise InstallRefused(
            f"the Claude plugin record at {path} is not valid JSON"
        ) from error
    if not isinstance(document, dict):
        raise InstallRefused(
            f"the Claude plugin record at {path} does not hold the object Claude writes"
        )
    return document


def plugin_record(config: Path) -> dict[str, dict[str, Any]]:
    """The enabled user-scope entries of `installed_plugins.json` under `config`, by
    plugin id. A missing file holds nothing."""
    path = config / "plugins" / "installed_plugins.json"
    settings = config / "settings.json"
    if not path.exists():
        return {}
    document = read_json(path)
    enabled = read_json(settings).get("enabledPlugins", {}) if settings.exists() else {}
    rows = document.get("plugins")
    if not isinstance(rows, dict) or not isinstance(enabled, dict):
        raise InstallRefused(
            f"the Claude plugin record at {path} does not hold the plugins Claude writes"
        )
    found = {}
    for ident, entries in sorted(rows.items()):
        if enabled.get(ident) is not True:
            continue
        for entry in entries if isinstance(entries, list) else [None]:
            if not isinstance(entry, dict) or not isinstance(entry.get("scope"), str):
                raise InstallRefused(
                    f"the Claude plugin record at {path} does not hold the plugins "
                    "Claude writes"
                )
            if entry["scope"] != "user":
                continue
            if not isinstance(entry.get("installPath"), str):
                raise InstallRefused(
                    f"the Claude plugin record at {path} does not hold the plugins "
                    "Claude writes"
                )
            found[ident] = entry
    return found


def marketplace_entry(config: Path, ident: str) -> Mapping[str, Any] | None:
    """The plugin's entry in its marketplace.json, found through the
    `known_marketplaces.json` under `config`; None when there is no entry."""
    plugin, _, market = ident.partition("@")
    known = config / "plugins" / "known_marketplaces.json"
    row = read_json(known).get(market) if known.exists() else None
    location = row.get("installLocation") if isinstance(row, dict) else None
    if not isinstance(location, str):
        return None
    catalog = Path(location) / ".claude-plugin" / "marketplace.json"
    rows = read_json(catalog).get("plugins") if catalog.is_file() else None
    return next(
        (
            row
            for row in (rows if isinstance(rows, list) else [])
            if isinstance(row, dict) and row.get("name") == plugin
        ),
        None,
    )


def plugin_admitted(
    config: Path, plugin: str, *, deadline: float | None = None
) -> tuple[str, dict[str, Any]]:
    """The id and the record entry of `plugin` under `config`, after `admit`."""
    deadline_check(deadline)
    record = plugin_record(config)
    ident = next((key for key in record if key.partition("@")[0] == plugin), None)
    if ident is None:
        raise InstallFailed(
            f"claude plugin install wrote no enabled record of {plugin}"
        )
    admit(
        Path(record[ident]["installPath"]),
        marketplace_entry(config, ident),
        ident,
        deadline=deadline,
    )
    deadline_check(deadline)
    return ident, record[ident]


def plugin_install(
    provider: str, source: str, plugin: str, check: bool
) -> dict[str, Any]:
    deadline = time.monotonic() + DEADLINE
    provider_check("plugin", provider)
    source_check(source)
    name_check(plugin)
    links_check(source, deadline)
    command = ["claude", "plugin", "install", plugin, "--marketplace", source]
    command += ["-s", "user", "--json"]
    with vendor(deadline) as call:
        if check:
            env = environment(Path.home(), call.work / "gh", **staged(call.work))
            call(command, env | {"DISABLE_AUTOUPDATER": "1"})
            config = call.work / "claude"
            ident, entry = plugin_admitted(config, plugin, deadline=deadline)
            version = entry.get("version")
            item = {
                "name": ident,
                "version": version if isinstance(version, str) else None,
                "hash": None,
            } | shown(Path(entry["installPath"]), deadline)
            return answer("plugin", provider, source, claude_config(), [item], deadline)
        config = private(claude_config())
        if any((config / "plugins/cache").glob(f"*/{plugin}")):
            raise InstallRefused(
                f"plugin {plugin} is already installed. NOTT does not replace an installed plugin."
            )
        settings = config / "settings.json"
        prior = {
            path: read_json(path) if path.exists() else None
            for path in (
                settings,
                config / "plugins/installed_plugins.json",
                config / "plugins/known_marketplaces.json",
            )
        }
        if not isinstance((prior[settings] or {}).get("enabledPlugins", {}), dict):
            raise InstallRefused(
                f"The enabledPlugins value in {settings} is not an object."
            )
        env = environment(
            private(call.work / "home"), call.work / "gh", CLAUDE_CONFIG_DIR=str(config)
        )
        # A failed call can still have written: admission runs after it either way.
        failure: InstallFailed | None = None
        try:
            written = call(command, env | {"DISABLE_AUTOUPDATER": "1"})
        except InstallFailed as error:
            failure = error
        problem: InstallRefused | InstallFailed | None = None
        try:
            plugin_admitted(config, plugin, deadline=deadline)
        except (InstallRefused, InstallFailed) as error:
            problem = error
        except OSError as error:
            problem = InstallFailed(str(error))
        if failure is not None or problem is not None:
            causes = ". ".join(
                str(error) for error in (failure, problem) if error is not None
            )
            try:
                disabled = plugin_disable(config, plugin, prior)
            except InstallFailed as error:
                raise InstallFailed(f"{causes}. {error}.") from error
            refusal = (
                f"{problem.item} as installed holds {problem.what}; NOTT disabled it in {settings}"
                if isinstance(problem, NotAdmitted)
                else f"{problem}. NOTT disabled {', '.join(disabled) or plugin} in {settings}."
                if problem is not None
                else ""
            )
            if failure is not None:
                raise InstallFailed(f"{failure}. {refusal}".strip()) from problem
            if isinstance(problem, InstallFailed):
                raise InstallFailed(refusal) from problem
            raise InstallRefused(refusal) from problem
        return {"installed": written}


def plugin_disable(
    config: Path, plugin: str, prior: dict[Path, dict[str, Any] | None]
) -> list[str]:
    settings = config / "settings.json"
    try:
        document = read_json(settings) if settings.exists() else {}
    except InstallRefused, InstallFailed:
        document = {}
    roots = list((config / "plugins/cache").glob(f"*/{plugin}"))
    enabled = document.get("enabledPlugins", {})
    if not isinstance(enabled, dict):
        enabled = {}
    names = {ident for ident in enabled if ident.partition("@")[0] == plugin} | {
        f"{plugin}@{root.parent.name}" for root in roots
    }
    document = dict(prior[settings] or {})
    document["enabledPlugins"] = document.get("enabledPlugins", {}) | dict.fromkeys(
        names, False
    )
    errors = []
    for path, previous in prior.items():
        if path == settings:
            previous = document
        try:
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                replacement = path.with_name(f".{path.name}.nott")
                replacement.write_text(json.dumps(previous, indent=2) + "\n")
                replacement.replace(path)
        except OSError as error:
            errors.append(f"{path}: {error}")
    try:
        remove_written(roots)
    except InstallFailed as error:
        errors.append(str(error).removeprefix("NOTT cleanup failed: "))
    if errors:
        raise InstallFailed("NOTT cleanup failed: " + ". ".join(errors))
    return sorted(names)


# Readers -------------------------------------------------------------------------


def installed(provider: str, kind: str) -> list[dict[str, Any]]:
    """What is installed for `provider`: each name, version and path. It reads
    files only."""
    provider_check(kind, provider)
    if kind == "skill":
        target = skills_target(provider)
        names = sorted(os.listdir(target)) if target.is_dir() else []
        rows = []
        for name in names:
            row: dict[str, Any] = {
                "name": name,
                "version": None,
                "path": str(target / name),
            }
            document = target / name / "SKILL.md"
            try:
                metadata = metadata_read(document) if document.is_file() else {}
            except InstallRefused:
                metadata = {}
            if metadata:
                row["metadata"] = metadata
            rows.append(row)
        return rows
    return [
        {
            "name": ident,
            "version": entry["version"]
            if isinstance(entry.get("version"), str)
            else None,
            "path": entry["installPath"],
        }
        for ident, entry in plugin_record(claude_config()).items()
    ]


def claude_roots() -> list[Path]:
    """The installed roots a new Claude or Qwen node loads after its node view: the
    skills root when it holds a skill, then each enabled user-scope plugin. It reads
    files only, so no process starts on an activation path."""
    roots = []
    skills = claude_skills()
    if (skills / "skills").is_dir() and any((skills / "skills").iterdir()):
        roots.append(skills)
    roots += [
        Path(entry["installPath"]) for entry in plugin_record(claude_config()).values()
    ]
    return roots
