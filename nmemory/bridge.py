"""Deterministic Markdown splitting for the context corpus (`nmemory.context`).

A document splits at the highest heading level that occurs two or more times,
else at the highest level that occurs once, else at blank lines. Front matter is
skipped, a fenced block never splits, and each chunk keeps the 1-based line where
it starts.
"""

from __future__ import annotations

MAX_MARKDOWN_CHUNK_CHARS = 4096


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
