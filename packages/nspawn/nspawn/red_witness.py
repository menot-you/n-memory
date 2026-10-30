"""Shared RED grammar, bounded bundle snapshots and read-only clone observation.

Local producer artifacts are not signed attestations. Nothing in this reader executes
commands or test source contained in a bundle.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from tempfile import TemporaryFile
from types import MappingProxyType
from typing import Any, Final


def validate_reference(ident: str, file: str, name: str) -> None:
    for value in (ident, name):
        if not value or any(c.isspace() for c in value):
            raise ValueError("test id and name must contain no whitespace")
    pure = PurePosixPath(file)
    if (
        pure.is_absolute()
        or str(pure) != file
        or ".." in pure.parts
        or file.startswith("-")
        or "\\" in file
        or "\x00" in file
        or file == "."
        or not file.endswith(".py")
    ):
        raise ValueError("test file must be a relative .py path in normal form")
    if name.startswith(":") or "/" in name or "\\" in name:
        raise ValueError("test name must be a pytest node inside the file")


@dataclass(frozen=True, slots=True)
class TestReference:
    id: str
    file: str
    name: str

    def __post_init__(self) -> None:
        validate_reference(self.id, self.file, self.name)


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RedRefused("the RED record has duplicate keys")
        result[key] = value
    return result


SCHEMA: Final = "nott.plan.red.v1"
WITNESS_KEYS: Final = frozenset(
    {"file", "name", "command", "exit", "assertion", "base"}
)
RECORD_KEYS: Final = frozenset(
    {"schema", "tests", "tests_sha256", "witnesses", "refused"}
)
#: The private ref that the fetched branch gets inside the disposable clone only.
FETCHED: Final = "refs/nott/red/branch"
#: Transports that the fetch may use; `ext::` and others run commands.
PROTOCOLS: Final = "file:git:http:https:ssh"
FIX: Final = "start the test-judge again with these lines"

SHA: Final = re.compile(r"[0-9a-f]{40}")
_DIGEST: Final = re.compile(r"[0-9a-f]{64}")
UNIT: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_TEST_FILE: Final = re.compile(r"test_[^/]*\.py|[^/]*_test\.py")
_ITEM: Final = re.compile(r"- (input|failure) (.+)")
_TOUCH: Final = re.compile(r"- touch `([^`]+)`")
_TOUCH_NONE: Final = re.compile(r"- touch none: \S.*")
_CALL: Final = re.compile(r"- call `([^`]+):([1-9][0-9]*)` `([^`]+)`(?: -> (.*))?")
_FIELD: Final = re.compile(r"- `(file|name|covers|fails today)`: (\S.*)")
_FENCE: Final = re.compile(r"(`{3,})[\w+-]*")
FIELDS: Final = ("file", "name", "covers", "fails today")
DIRECTORY: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class RedRefused(Exception):
    """The set, the base or a record is refused before any run: exit 2."""


class RedCodeRefused(RedRefused):
    """An unsafe code block is not RED: exit 3, with no record change."""


class RedUnavailable(Exception):
    """The clone, the fetch, the environment or the sandbox failed: exit 1."""


@dataclass(frozen=True, slots=True)
class Call:
    path: str
    line: int
    pattern: str


@dataclass(frozen=True, slots=True)
class JudgedSet:
    tests: tuple[TestReference, ...]
    code: Mapping[str, str]
    touch: tuple[str, ...]
    calls: tuple[Call, ...]


def printable(text: str) -> str:
    return "".join(c if c.isprintable() else "?" for c in text)


def refuse(problems: list[str]) -> RedRefused:
    """One refusal; each line quotes agent text, so no control character reaches a
    terminal."""
    lines = (line for problem in problems for line in problem.split("\n"))
    return RedRefused("\n".join([*map(printable, lines), FIX]))


def _path_problem(path: str) -> str | None:
    pure = PurePosixPath(path)
    if (
        pure.is_absolute()
        or str(pure) != path
        or any(part in ("..", "") or part.startswith((".", "-")) for part in pure.parts)
        or "\\" in path
        or "\x00" in path
    ):
        return (
            f"{path} must be a repository path in normal form with no hidden or .. part"
        )
    if not _TEST_FILE.fullmatch(pure.name):
        return f"{path} is not a test file; name it test_*.py or *_test.py"
    return None


def _mapping(text: str, number: int, problems: list[str]) -> list[str]:
    if text.startswith("none:"):
        if not text[5:].strip():
            problems.append(f"line {number}: `none:` needs a reason")
        return []
    ids = [ident.strip() for ident in text.split(",")]
    if not all(ids) or any(" " in ident for ident in ids):
        problems.append(
            f"line {number}: map to `<test id>, <test id>` or `none: <reason>`"
        )
    return [ident for ident in ids if ident]


def parse(text: str) -> JudgedSet:
    """The judged set, or `RedRefused` with every problem and its line."""
    problems: list[str] = []
    mapped: list[tuple[int, str]] = []
    kinds = {"input": 0, "failure": 0, "touch": 0}
    fields: dict[str, dict[str, str]] = {}
    code: dict[str, str] = {}
    touch: list[str] = []
    calls: dict[Call, int] = {}
    spaces = 0
    state, owner, fence, body = "", "", "", list[str]()
    for number, line in enumerate(text.split("\n"), 1):
        line = line.rstrip("\r")
        if state == "fence":
            if line.strip().strip("`") == "" and len(line.strip()) >= len(fence):
                code[owner] = "\n".join(body) + "\n"
                state = "after"
            else:
                body.append(line)
            continue
        if line.startswith("## "):
            if state == "code":
                problems.append(f"## Code {owner} has no fenced code")
            heading = line[3:].strip()
            state, owner = "", ""
            if heading == "Failure space":
                state, spaces = "space", spaces + 1
            elif heading.startswith("Test "):
                state, owner = "test", heading[5:].strip()
                if owner in fields:
                    problems.append(f"line {number}: test id {owner} is not unique")
                fields[owner] = {}
            elif heading.startswith("Code "):
                state, owner = "code", heading[5:].strip()
                if owner in code:
                    problems.append(f"line {number}: a second ## Code {owner}")
            continue
        if not line.strip() or not state:
            continue
        if state == "space":
            if item := _ITEM.fullmatch(line):
                kinds[item[1]] += 1
                described, arrow, target = item[2].partition(" -> ")
                if not arrow or not described.strip():
                    problems.append(
                        f"line {number}: `{line}` has no mapping; add `-> <test id>` or `-> none: <reason>`"
                    )
                else:
                    mapped += [(number, i) for i in _mapping(target, number, problems)]
            elif touched := _TOUCH.fullmatch(line):
                kinds["touch"] += 1
                touch.append(touched[1])
            elif _TOUCH_NONE.fullmatch(line):
                kinds["touch"] += 1
            elif (call := _CALL.fullmatch(line)) and call[4] is not None:
                site = Call(call[1], int(call[2]), call[3])
                where = PurePosixPath(site.path)
                if (
                    where.is_absolute()
                    or ".." in where.parts
                    or str(where) != site.path
                ):
                    problems.append(f"line {number}: {site.path} is not in normal form")
                if site in calls:
                    problems.append(f"line {number}: a second call item for {line}")
                calls[site] = number
                mapped += [(number, i) for i in _mapping(call[4], number, problems)]
            elif call:
                problems.append(
                    f"line {number}: `{line}` has no mapping; add `-> <test id>` or `-> none: <reason>`"
                )
            else:
                problems.append(f"line {number}: `{line}` is not a failure-space item")
        elif state == "test":
            if (field := _FIELD.fullmatch(line)) is None:
                problems.append(f"line {number}: `{line}` is not a test field")
            elif field[1] in fields[owner]:
                problems.append(f"line {number}: a second `{field[1]}` for {owner}")
            else:
                fields[owner][field[1]] = field[2].strip()
        elif state == "code" and (opened := _FENCE.fullmatch(line.strip())):
            state, fence, body = "fence", opened[1], []
        else:
            problems.append(
                f"line {number}: `{line}` is outside the fence of ## Code {owner}"
                if state == "after"
                else f"line {number}: ## Code {owner} must start with a fence"
            )
    if state == "fence":
        problems.append(f"## Code {owner}: the fence {fence} is not closed")
    elif state == "code":
        problems.append(f"## Code {owner} has no fenced code")
    if spaces != 1:
        problems.append("the set needs exactly one ## Failure space section")
    for kind, count in kinds.items():
        if spaces and not count:
            problems.append(
                f"the failure space has no `{kind}` item"
                + ("; add `- touch none: <reason>`" if kind == "touch" else "")
            )
    tests: list[TestReference] = []
    for ident, given in fields.items():
        if missing := [name for name in FIELDS if name not in given]:
            problems.append(f"test {ident} has no {', '.join(missing)}")
            continue
        if problem := _path_problem(given["file"]):
            problems.append(f"test {ident}: {problem}")
            continue
        try:
            tests.append(TestReference(ident, given["file"], given["name"]))
        except ValueError as error:
            problems.append(str(error))
    if not fields:
        problems.append("the set has no ## Test section")
    for path in code:
        if problem := _path_problem(path):
            problems.append(f"## Code {path}: {problem}")
    problems += [
        f"line {number}: {ident} is no ## Test of this set"
        for number, ident in mapped
        if ident not in fields
    ]
    if problems:
        raise refuse(problems)
    return JudgedSet(
        tuple(tests), MappingProxyType(dict(code)), tuple(touch), tuple(calls)
    )


def record(data: bytes) -> dict[str, Any]:
    """A RED record's content, or `RedRefused` when its shape is not `SCHEMA`."""
    try:
        value = json.loads(data, object_pairs_hook=_unique)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise RedRefused("the RED record is not bounded JSON") from error
    if not isinstance(value, dict) or set(value) != RECORD_KEYS:
        raise RedRefused(f"the RED record must have exactly {sorted(RECORD_KEYS)}")
    witnesses, refused = value["witnesses"], value["refused"]
    if (
        value["schema"] != SCHEMA
        or not isinstance(value["tests"], str)
        or not isinstance(value["tests_sha256"], str)
        or not _DIGEST.fullmatch(value["tests_sha256"])
        or not isinstance(witnesses, dict)
        or not isinstance(refused, dict)
        or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in refused.items()
        )
    ):
        raise RedRefused(f"the RED record is not {SCHEMA}")
    for ident, witness in witnesses.items():
        if (
            not isinstance(witness, dict)
            or set(witness) != WITNESS_KEYS
            or type(witness["exit"]) is not int
            or not all(
                isinstance(witness[key], str) and witness[key].strip()
                for key in WITNESS_KEYS - {"exit"}
            )
            or not SHA.fullmatch(witness["base"])
        ):
            raise RedRefused(f"the RED record's witness {ident!r} is not {SCHEMA}")
    return value


@dataclass(frozen=True, slots=True)
class Bundle:
    record_path: str
    tests_path: str
    unit: str
    base: str
    tests: tuple[TestReference, ...]
    record_bytes: bytes
    tests_bytes: bytes


def validate_bundle(
    record_bytes: bytes,
    tests_bytes: bytes,
    *,
    unit: str,
    base: str | None = None,
    record_path: str = "",
    tests_path: str = "",
) -> Bundle:
    if max(len(record_bytes), len(tests_bytes)) > 256 * 1024:
        raise RedRefused("RED files exceed the bounded read limit")
    value = record(record_bytes)
    if not UNIT.fullmatch(unit) or value["tests"] != f"tests/{unit}.md":
        raise RedRefused("RED record path and judged-set unit differ")
    if value["refused"] or not value["witnesses"]:
        raise RedRefused("RED record contains refused tests or no witnesses")
    if hashlib.sha256(tests_bytes).hexdigest() != value["tests_sha256"]:
        raise RedRefused(f"RED judged-set {value['tests']} sha256 digest differs")
    try:
        judged = parse(tests_bytes.decode("utf-8"))
    except UnicodeError as error:
        raise RedRefused("RED judged set must be UTF-8") from error
    if {test.id for test in judged.tests} != set(value["witnesses"]):
        raise RedRefused("RED witness ids differ from the judged set")
    bases: set[str] = set()
    for test in judged.tests:
        witness = value["witnesses"][test.id]
        if witness["file"] != test.file or witness["name"] != test.name:
            raise RedRefused("RED witness file or node differs from the judged set")
        if witness["exit"] != 1:
            raise RedRefused(f"RED witness {test.id} exit must be 1")
        for field in ("command", "assertion"):
            text = witness[field]
            if not text.strip() or text.splitlines() != [text]:
                raise RedRefused(
                    "RED witness command and assertion must be nonblank single lines"
                )
        bases.add(witness["base"])
    if len(bases) != 1:
        raise RedRefused("RED witnesses must name one base")
    commit = next(iter(bases))
    if base is not None and commit != base:
        raise RedRefused("RED record base " + commit + " differs from clone HEAD")
    return Bundle(
        record_path, tests_path, unit, commit, judged.tests, record_bytes, tests_bytes
    )


def bundle_paths(locator: str) -> tuple[str, str]:
    pure = PurePosixPath(locator)
    parts = pure.parts
    if (
        str(pure) != locator
        or len(parts) != 5
        or parts[:2] != (".nott", "plans")
        or parts[3] != "red"
        or not parts[4].endswith(".json")
        or not UNIT.fullmatch(parts[2])
        or not UNIT.fullmatch(pure.stem)
        or any(p in (".", "..", ".git") for p in parts)
        or "\\" in locator
    ):
        raise RedRefused(
            "RED record path must be .nott/plans/<plan>/red/<unit>.json in normal form"
        )
    return str(pure.parent.parent / "tests" / (pure.stem + ".md")), pure.stem


def read_regular(root: Path, relative: str) -> bytes:
    parts = PurePosixPath(relative).parts
    if (
        not parts
        or PurePosixPath(relative).is_absolute()
        or ".." in parts
        or str(PurePosixPath(relative)) != relative
    ):
        raise RedRefused("RED path must be relative and in normal form")
    descriptors: list[int] = []
    try:
        descriptors.append(os.open(root, DIRECTORY))
        for part in parts[:-1]:
            descriptors.append(os.open(part, DIRECTORY, dir_fd=descriptors[-1]))
        descriptors.append(
            os.open(
                parts[-1],
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=descriptors[-1],
            )
        )
        fd = descriptors[-1]
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > 256 * 1024:
            raise RedRefused("RED path must name a bounded regular file")
        data = bytearray()
        while len(data) <= 256 * 1024:
            chunk = os.read(fd, min(65536, 256 * 1024 + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        after = os.fstat(fd)
        if (
            len(data) > 256 * 1024
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_ctime_ns != after.st_ctime_ns
        ):
            raise RedRefused("RED file changed or exceeds the bounded read limit")
        return bytes(data)
    except (OSError, ValueError) as error:
        raise RedRefused(
            "RED path is unsafe or unreadable; materialize bounded regular files"
        ) from error
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def read_bundle(root: Path, locator: str, *, base: str | None = None) -> Bundle:
    tests, unit = bundle_paths(locator)
    raw = read_regular(root, locator)
    judged = read_regular(root, tests)
    return validate_bundle(
        raw, judged, unit=unit, base=base, record_path=locator, tests_path=tests
    )


def _git(root: Path, *args: str) -> str:
    environment = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    environment.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_NO_REPLACE_OBJECTS="1",
        GIT_NO_LAZY_FETCH="1",
        GIT_TERMINAL_PROMPT="0",
    )
    try:
        with TemporaryFile() as output:
            result = subprocess.run(
                [
                    "git",
                    "--no-optional-locks",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-c",
                    "core.fsmonitor=false",
                    "-C",
                    str(root),
                    *args,
                ],
                env=environment,
                stdout=output,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
            output.seek(0)
            raw = output.read(256 * 1024 + 1)
            if result.returncode or len(raw) > 256 * 1024:
                raise RedRefused("clone HEAD observation failed or exceeds its limit")
            return raw.decode("utf-8").strip()
    except (OSError, UnicodeError, subprocess.TimeoutExpired) as error:
        raise RedRefused("clone HEAD observation unavailable") from error


def clone_head(root: Path) -> str:
    if not (root / ".git").is_dir() or (root / ".git").is_symlink():
        raise RedRefused("writer requires a standalone clone")
    if Path(_git(root, "rev-parse", "--show-toplevel")) != root.resolve():
        raise RedRefused("writer cwd must be the clone root")
    head = _git(root, "rev-parse", "--verify", "HEAD^{commit}")
    if not SHA.fullmatch(head):
        raise RedRefused("clone HEAD is not a full commit")
    return head


def origin_branch(root: Path, head: str) -> str | None:
    try:
        names = _git(
            root,
            "for-each-ref",
            "--format=%(refname:strip=3)",
            "--contains=" + head,
            "refs/remotes/origin",
        ).splitlines()
    except RedRefused:
        return None
    safe = [
        name
        for name in names
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9/._-]*", name) and name != "HEAD"
    ]
    return "main" if "main" in safe else next(iter(safe), None)
