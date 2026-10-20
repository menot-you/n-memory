"""Governed readers over the owner's file-based memory (W1, bridge).

Closed sources, deterministic markdown splitting, fail-closed symlink fences.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

MAX_MARKDOWN_CHUNK_CHARS = 4096


class BridgeSource(StrEnum):
    USER_CLAUDE_MD = "user-claude-md"
    PROJECT_CLAUDE_MD = "project-claude-md"
    PROJECT_AGENTS_MD = "project-agents-md"
    MEMORY_DIR = "memory-dir"

    @classmethod
    def from_str(cls, value: str) -> BridgeSource:
        try:
            return cls(value.strip().lower().replace("_", "-"))
        except ValueError:
            raise ValueError(f"unknown bridge source: {value}") from None

    @property
    def source_label(self) -> str:
        return self.value


@dataclass(frozen=True)
class BridgeCandidate:
    content: str
    anchor: str
    source_label: str


class BridgeError(Exception):
    pass


class BridgeSourceMissing(BridgeError):
    def __init__(self, source_label: str, tried: list[Path]) -> None:
        self.source_label = source_label
        self.tried = tried
        tried_strs = [str(p) for p in tried]
        super().__init__(f"bridge source '{source_label}' missing: tried {tried_strs}")


class BridgeSymlinkRejected(BridgeError):
    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(
            f"bridge rejected '{path}': whitelisted leaf is a symlink, never followed"
        )


class BridgeNotADirectory(BridgeError):
    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(f"bridge memory dir '{path}' is not a directory")


class BridgeIoError(BridgeError):
    def __init__(self, path: Path, message: str) -> None:
        self.path = path
        self.message = message
        super().__init__(f"bridge read failed at '{path}': {message}")


def is_fence_marker(line: str) -> bool:
    t = line.lstrip()
    return t.startswith(("```", "~~~"))


def heading_level(line: str) -> int | None:
    stripped = line.rstrip("\r\n")
    hashes = len(stripped) - len(stripped.lstrip("#"))
    if hashes == 0 or hashes > 6:
        return None
    rest = stripped[hashes:]
    if not rest or rest[0] in (" ", "\t"):
        return hashes
    return None


def body_start(lines: list[str]) -> int:
    if not lines:
        return 0
    if lines[0].rstrip() != "---":
        return 0
    for i in range(1, len(lines)):
        t = lines[i].rstrip()
        if t in ("---", "..."):
            return i + 1
    return 0


def split_markdown(content: str) -> list[tuple[int, str]]:
    content = content.removeprefix("\ufeff")
    lines = content.splitlines()
    start = body_start(lines)

    # Annotate body lines with (1-based line number, text, inside_fence)
    body: list[tuple[int, str, bool]] = []
    in_fence = False
    for idx in range(start, len(lines)):
        text = lines[idx]
        if in_fence:
            body.append((idx + 1, text, True))
            if is_fence_marker(text):
                in_fence = False
        elif is_fence_marker(text):
            body.append((idx + 1, text, True))
            in_fence = True
        else:
            body.append((idx + 1, text, False))

    # Headings outside fences
    headings: list[tuple[int, int]] = []
    for i, (_, text, fenced) in enumerate(body):
        if not fenced:
            lvl = heading_level(text)
            if lvl is not None:
                headings.append((i, lvl))

    counts = [0] * 6
    for _, lvl in headings:
        counts[lvl - 1] += 1

    split_level: int | None = None
    for i, c in enumerate(counts):
        if c >= 2:
            split_level = i + 1
            break
    if split_level is None:
        for i, c in enumerate(counts):
            if c >= 1:
                split_level = i + 1
                break

    regions: list[tuple[int, int]] = []
    if split_level is not None:
        starts = [0]
        for i, l in headings:
            if l == split_level and i != 0:
                starts.append(i)
        starts.append(len(body))
        for j in range(len(starts) - 1):
            regions.append((starts[j], starts[j + 1]))
    else:
        block_start: int | None = None
        for i, (_, text, fenced) in enumerate(body):
            if fenced or text.strip():
                if block_start is None:
                    block_start = i
            elif block_start is not None:
                regions.append((block_start, i))
                block_start = None
        if block_start is not None:
            regions.append((block_start, len(body)))

    out: list[tuple[int, str]] = []
    for reg_start, reg_end in regions:
        slice_body = body[reg_start:reg_end]
        first_idx = None
        last_idx = None
        for k, (_, t, _) in enumerate(slice_body):
            if t.strip():
                if first_idx is None:
                    first_idx = k
                last_idx = k
        if first_idx is None or last_idx is None:
            continue
        kept = slice_body[first_idx : last_idx + 1]
        pending: list[str] = []
        pending_start = kept[0][0]
        pending_size = 0
        for line_no, line, _ in kept:
            if len(line) > MAX_MARKDOWN_CHUNK_CHARS:
                if pending:
                    out.append((pending_start, "\n".join(pending)))
                    pending = []
                    pending_size = 0
                for offset in range(0, len(line), MAX_MARKDOWN_CHUNK_CHARS):
                    out.append(
                        (
                            line_no,
                            line[offset : offset + MAX_MARKDOWN_CHUNK_CHARS],
                        )
                    )
                continue
            added = len(line) + (1 if pending else 0)
            if pending and pending_size + added > MAX_MARKDOWN_CHUNK_CHARS:
                out.append((pending_start, "\n".join(pending)))
                pending = []
                pending_size = 0
                added = len(line)
            if not pending:
                pending_start = line_no
            pending.append(line)
            pending_size += added
        if pending:
            out.append((pending_start, "\n".join(pending)))

    return out


def anchor_path(path: Path, anchor_root: Path) -> Path:
    try:
        return path.relative_to(anchor_root)
    except ValueError:
        return path


def candidates_from(
    content: str,
    path: Path,
    label: str,
    anchor_root: Path,
) -> list[BridgeCandidate]:
    base = anchor_path(path, anchor_root)
    chunks = split_markdown(content)
    return [
        BridgeCandidate(
            content=text,
            anchor=f"{base}:{start_line}",
            source_label=label,
        )
        for start_line, text in chunks
    ]


def read_regular_file_bytes(path: Path) -> bytes | None:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as e:
        raise BridgeIoError(path, str(e)) from e

    if stat.S_ISLNK(st.st_mode):
        raise BridgeSymlinkRejected(path)
    if not stat.S_ISREG(st.st_mode):
        raise BridgeIoError(path, "not a regular file")

    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return None
    except OSError as e:
        raise BridgeIoError(path, str(e)) from e


def read_regular_file(path: Path) -> str | None:
    raw_bytes = read_regular_file_bytes(path)
    if raw_bytes is None:
        return None
    try:
        return raw_bytes.decode("utf-8")
    except UnicodeDecodeError as e:
        raise BridgeIoError(path, str(e)) from e


def read_single(
    path: Path,
    label: str,
    anchor_root: Path,
) -> list[BridgeCandidate]:
    content = read_regular_file(path)
    if content is None:
        raise BridgeSourceMissing(label, [path])
    return candidates_from(content, path, label, anchor_root)


def read_memory_dir(
    dir_path: Path,
    label: str,
    anchor_root: Path,
) -> list[BridgeCandidate]:
    if not dir_path.exists():
        raise BridgeSourceMissing(label, [dir_path])
    if not dir_path.is_dir():
        raise BridgeNotADirectory(dir_path)

    try:
        entries = os.listdir(dir_path)
    except OSError as e:
        raise BridgeIoError(dir_path, str(e)) from e

    files: list[tuple[str, Path]] = []
    for entry in entries:
        full_path = dir_path / entry
        try:
            st = os.lstat(full_path)
        except OSError as e:
            raise BridgeIoError(full_path, str(e)) from e

        if not stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode):
            continue
        if not entry.endswith(".md"):
            continue
        files.append((entry, full_path))

    files.sort(key=lambda x: x[0])

    out: list[BridgeCandidate] = []
    for _, path in files:
        content = read_regular_file(path)
        if content is not None:
            out.extend(candidates_from(content, path, label, anchor_root))
    return out


def read_source(
    source: BridgeSource | str,
    base_dir: Path | str,
    anchor_root: Path | str,
    memory_dir: str | Path | None = None,
) -> list[BridgeCandidate]:
    src_obj = BridgeSource.from_str(source) if isinstance(source, str) else source
    base = Path(base_dir)
    root = Path(anchor_root)
    label = src_obj.source_label

    if src_obj == BridgeSource.USER_CLAUDE_MD:
        tried = [
            base / ".claude3" / "CLAUDE.md",
            base / ".claude2" / "CLAUDE.md",
            base / ".claude" / "CLAUDE.md",
        ]
        for p in tried:
            content = read_regular_file(p)
            if content is not None:
                return candidates_from(content, p, label, root)
        raise BridgeSourceMissing(label, tried)

    elif src_obj == BridgeSource.PROJECT_CLAUDE_MD:
        return read_single(base / "CLAUDE.md", label, root)

    elif src_obj == BridgeSource.PROJECT_AGENTS_MD:
        return read_single(base / "AGENTS.md", label, root)

    elif src_obj == BridgeSource.MEMORY_DIR:
        if memory_dir is None:
            raise ValueError("source 'memory-dir' requires memory_dir")
        target_dir = Path(memory_dir)
        if not target_dir.is_absolute():
            target_dir = base / target_dir
        return read_memory_dir(target_dir, label, root)

    raise ValueError(f"unknown bridge source: {source}")


def import_block_source_key(source_label: str, anchor: str) -> str:
    path = anchor.rsplit(":", 1)[0] if ":" in anchor else anchor
    return f"{source_label}\n{path}"
