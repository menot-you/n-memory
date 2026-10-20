"""Apply the shared public source policy without changing executable values."""

from __future__ import annotations

import ast
import hashlib
import io
import json
import re
import subprocess
import sys
import tokenize
from pathlib import Path
from typing import Any

PRIVATE = {
    "private-host-path": re.compile(r"(?<![\w}~$./-])/(?:guara|nott)/(?:[\w./-]+)"),
    "personal-home": re.compile(r"/(?:home|Users)/nott(?:/|\b)"),
    "private-link": re.compile(
        r"https?://(?:docs[.]nott[.]ing|github[.]com/menot-you/pynott)(?:[/\w.#?=-]*)"
    ),
    "private-record": re.compile(r"\bcap-(?!1\b)\d+\b"),
    "credential": re.compile(
        r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|sk-(?:live-|proj-)?[A-Za-z0-9_-]{24,})\b"
    ),
    "private-key": re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----"),
}


class Refusal(ValueError):
    """The source does not meet the public export contract."""


class Finding(ValueError):
    """The complete source projection contains private data."""


def scan(root: Path) -> list[dict[str, str | int]]:
    """Check every emitted file. Do not return a matched value."""
    findings: list[dict[str, str | int]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        data = path.read_bytes().decode("utf-8", errors="replace")
        for number, line in enumerate(data.splitlines(), 1):
            for rule, pattern in PRIVATE.items():
                if pattern.search(line):
                    findings.append(
                        {
                            "path": path.relative_to(root).as_posix(),
                            "line": number,
                            "rule": rule,
                        }
                    )
    return findings


def private(text: str) -> bool:
    return any(pattern.search(text) for pattern in PRIVATE.values())


def executable_ast(text: str) -> str:
    """Return the Python AST without documentation or location fields."""
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if (
            isinstance(
                node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            )
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        ):
            node.body.pop(0)
            if not node.body and not isinstance(node, ast.Module):
                node.body.append(ast.Pass())
    return ast.dump(tree, include_attributes=False)


def public_python(path: Path) -> None:
    """Remove private commentary. Preserve the executable Python AST."""
    text = path.read_text()
    credentials(path, text)
    edits: list[tuple[int, int, str]] = []
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    for node in ast.walk(ast.parse(text)):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        if not node.body or not isinstance(node.body[0], ast.Expr):
            continue
        value = node.body[0].value
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            continue
        if private(value.value):
            paragraphs = []
            for raw_part in re.split(r"\n[ \t]*\n", value.value):
                part = re.sub(r"\s*\([^()]*\bcap-\d+\b[^()]*\)", "", raw_part)
                if PRIVATE["private-record"].search(part):
                    sentences = [
                        s
                        for s in re.split(r"(?<=[.!?])\s+(?=[A-Z`\"'])", part)
                        if not PRIVATE["private-record"].search(s)
                    ]
                    part = " ".join(sentences)
                if part.strip() and not private(part):
                    paragraphs.append(part)
            public = "\n\n".join(paragraphs)
            if not public.strip():
                public = re.sub(
                    r"\s*\([^()]*\bcap-\d+\b[^()]*\)", "", value.value.splitlines()[0]
                )
                if not public.strip() or private(public):
                    public = ""
            target: ast.expr | ast.stmt = value
            if "\n" in public:
                escaped = (
                    public.removesuffix('"')
                    .replace("\\", "\\\\")
                    .replace('"""', '\\"\\"\\"')
                )
                if public.endswith('"'):
                    escaped += '\\"'
                replacement = '"""' + escaped + '"""'
            else:
                replacement = repr(public)
            if not public.strip():
                target = node.body[0]
                replacement = (
                    "pass"
                    if len(node.body) == 1 and not isinstance(node, ast.Module)
                    else ""
                )
            assert target.end_lineno is not None and target.end_col_offset is not None
            start = offsets[target.lineno - 1] + len(
                lines[target.lineno - 1].encode()[: target.col_offset].decode()
            )
            end = offsets[target.end_lineno - 1] + len(
                lines[target.end_lineno - 1].encode()[: target.end_col_offset].decode()
            )
            if not replacement and (
                separator := re.match(r"[ \t]*;[ \t]*", text[end:])
            ):
                end += separator.end()
            edits.append((start, end, replacement))
    comments = [
        token
        for token in tokenize.generate_tokens(io.StringIO(text).readline)
        if token.type == tokenize.COMMENT
    ]
    groups: list[list[tokenize.TokenInfo]] = []
    for token in comments:
        if groups and token.start[0] == groups[-1][-1].end[0] + 1:
            groups[-1].append(token)
        else:
            groups.append([token])
    for group in groups:
        if any(private(token.string) for token in group):
            for token in group:
                start = offsets[token.start[0] - 1] + token.start[1]
                end = offsets[token.end[0] - 1] + token.end[1]
                if not lines[token.start[0] - 1][: token.start[1]].strip():
                    start = offsets[token.start[0] - 1]
                    end = offsets[token.end[0]]
                edits.append((start, end, ""))
    for start, end, replacement in sorted(edits, reverse=True):
        text = text[:start] + replacement + text[end:]
    if executable_ast(text) != executable_ast(path.read_text()):
        raise Refusal("public commentary removal changed executable Python")
    path.write_text(text)


def public_runtime(stage: Path, mapping: dict[str, str] | None = None) -> None:
    """Remove producer aliases and private references from runtime messages."""
    for source in sorted(stage.rglob("*")):
        if source.is_file():
            credentials(
                source.relative_to(stage),
                source.read_bytes().decode("utf-8", errors="replace"),
            )
    mapping = mapping or {
        "mcps/nmemory": "mcps/nmemory",
        "mcps/nspawn": "mcps/nspawn",
        "evals/nmemory": "evals/nmemory",
    }

    def selected(relative: str) -> Path:
        for origin, target in mapping.items():
            if relative.startswith(origin + "/"):
                return stage / target / relative[len(origin) + 1 :]
        return stage / relative

    path = selected("mcps/nmemory/nmemory/project.py")
    if path.is_file():
        text = path.read_text()
        tree = ast.parse(text)
        aliases = [
            node
            for node in tree.body
            if isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "PROJECT_ALIASES"
        ]
        if len(aliases) != 1 or not isinstance(aliases[0].value, ast.Dict):
            raise Refusal("the project alias contract changed")
        value = aliases[0].value
        assert value.end_lineno is not None
        lines = text.splitlines(keepends=True)
        lines[value.lineno - 1 : value.end_lineno] = [
            "PROJECT_ALIASES: dict[str, str] = {}\n"
        ]
        path.write_text("".join(lines))
    metadata = selected("mcps/nmemory/pyproject.toml")
    if metadata.is_file():
        metadata.write_text(
            re.sub(
                r'(?m)^\s*"../../harness/nott_harness",?\n', "", metadata.read_text()
            )
        )
    for relative in (
        "mcps/nmemory/nmemory/tools/validation.py",
        "mcps/nspawn/nspawn/owner_roster.py",
    ):
        path = selected(relative)
        if not path.is_file():
            continue
        text = path.read_text()
        if relative.endswith("validation.py"):
            text = PRIVATE["private-record"].sub("cap-<number>", text)
        else:
            text = re.sub(r" \(cap-\d+\)", "", text)
        path.write_text(text)


def credentials(path: Path, text: str) -> None:
    """Refuse credentials before a transform can remove them."""
    for rule in ("credential", "private-key"):
        if PRIVATE[rule].search(text):
            raise Finding(f"public source scan refused: {path.name}: {rule}")


def public_toml(path: Path) -> None:
    """Remove private TOML comments. Preserve all parsed values."""
    import tomllib

    original = path.read_text()
    credentials(path, original)
    result: list[str] = []
    quote = ""
    index = 0
    while index < len(original):
        if quote:
            if original.startswith(quote, index):
                result.append(quote)
                index += len(quote)
                quote = ""
            elif quote[0] == '"' and original[index] == "\\":
                result.append(original[index : index + 2])
                index += 2
            else:
                result.append(original[index])
                index += 1
        elif original[index] in "\"'":
            quote = original[index]
            if original.startswith(quote * 3, index):
                quote *= 3
            result.append(quote)
            index += len(quote)
        elif original[index] == "#":
            end = original.find("\n", index)
            if end < 0:
                end = len(original)
            comment = original[index:end]
            if not private(comment):
                result.append(comment)
            index = end
        else:
            result.append(original[index])
            index += 1
    text = "".join(result)
    if tomllib.loads(original) != tomllib.loads(text):
        raise Refusal("public commentary removal changed TOML values")
    path.write_text(text)


def commentary(stage: Path) -> None:
    """Apply one commentary policy to every selected source file."""
    for path in sorted(stage.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            if path.suffix == ".py":
                public_python(path)
            elif path.suffix == ".toml":
                public_toml(path)
        except Refusal as error:
            raise Refusal(f"{path.relative_to(stage)}: {error}") from None


def public_contributing(stage: Path, command: str) -> None:
    """Replace private portal checks with the selected public check."""
    guide = stage / "CONTRIBUTING.md"
    if not guide.is_file():
        return
    old = (
        "Documentation changes need local link checks, figure inspection, and the documentation build.\n"
        "The source workspace builds its portal with `bash tools/document.sh build`.\n"
        "That private portal also requires its configured memory export and build assets.\n"
        "A public export must not include private portal content."
    )
    new = (
        "For public documentation, check local links with this command:\n\n"
        f"```bash\n{command}\n```\n\n"
        "Inspect each changed SVG figure in a viewer."
    )
    text = guide.read_text()
    guide.write_text(text.replace(old, new))


def format_source(stage: Path) -> None:
    """Format public Python with the export runtime's Ruff."""
    result = subprocess.run(
        [sys.executable, "-I", "-m", "ruff", "format", "--no-cache", str(stage)],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if result.returncode:
        raise Refusal("public formatting requires Ruff in the export Python runtime")


WIRE_FIXTURES = (
    "claude-stream-wire-2.1.250.ndjson",
    "claude-stream-wire-2.1.251.ndjson",
    "codex-appserver-wire-0.153.4.ndjson",
)
WIRE_METADATA = {
    "/cwd": "<CAPTURE_CWD>",
    "/plugins/*/path": "<PLUGIN_PATH>",
    "/memory_paths/auto": "<MEMORY_PATH>",
    "/messaging_socket_path": "<SOCKET_PATH>",
    "/frame/result/thread/gitInfo/sha": "<GIT_SHA>",
    "/frame/result/thread/gitInfo/branch": "<GIT_BRANCH>",
    "/frame/result/thread/gitInfo/originUrl": "<GIT_ORIGIN>",
    "# Binary": "<CLAUDE_BINARY>",
}


def wire_fixture_project(source: Path, output: Path) -> dict[str, Any]:
    """Create a declared public derivative from an owned recording.

    Run this file with INPUT and OUTPUT arguments to reproduce a derivative.
    INPUT can be an owned recording or a shipped public derivative with its manifest.
    This procedure changes only the declared metadata fields. It preserves all events.
    A different recording requires review and new replay digest pins before adoption.
    """
    if source.is_symlink():
        raise Refusal("wire projection refuses a source link")
    data = source.read_bytes()
    original = data.decode("utf-8")
    credentials(source, original)
    for line in original.splitlines():
        if line.strip() and not line.startswith("#"):
            try:
                decoded = json.loads(line)
            except ValueError:
                raise Refusal(
                    "wire projection requires valid JSON event lines"
                ) from None
            credentials(source, json.dumps(decoded, ensure_ascii=False))
    name = source.name.replace(".public.ndjson", ".ndjson")
    if name not in WIRE_FIXTURES:
        raise Refusal("wire projection requires a declared recording version")
    public_name = name.replace(".ndjson", ".public.ndjson")
    manifest: dict[str, Any]
    if source.name == public_name:
        manifest = json.loads(source.with_suffix(".projection.json").read_text())
        if (
            manifest.get("schema") != 1
            or manifest.get("procedure") != "nspawn-wire-public-v1"
            or manifest.get("source", {}).get("name") != name
            or manifest.get("metadata_fields") != WIRE_METADATA
            or manifest.get("derivative")
            != {
                "name": public_name,
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        ):
            raise Refusal("wire derivative provenance does not match its content")
        text = original
    else:
        lines = []
        for line in original.splitlines(keepends=True):
            if line.startswith("# Binary:") and name.startswith("claude-"):
                lines.append("# Binary: <CLAUDE_BINARY>\n")
                continue
            if not line.strip() or line.startswith("#"):
                lines.append(line)
                continue
            frame = json.loads(line)
            before = json.dumps(frame)
            if name.startswith("claude-") and (
                frame.get("type"),
                frame.get("subtype"),
            ) == ("system", "init"):
                for field, replacement in (
                    ("cwd", "<CAPTURE_CWD>"),
                    ("messaging_socket_path", "<SOCKET_PATH>"),
                ):
                    if field in frame:
                        frame[field] = replacement
                for plugin in frame.get("plugins", []):
                    if "path" in plugin:
                        plugin["path"] = "<PLUGIN_PATH>"
                if "auto" in frame.get("memory_paths", {}):
                    frame["memory_paths"]["auto"] = "<MEMORY_PATH>"
            elif name.startswith("codex-"):
                git = (
                    frame.get("frame", {})
                    .get("result", {})
                    .get("thread", {})
                    .get("gitInfo")
                )
                if isinstance(git, dict):
                    for field in ("sha", "branch", "originUrl"):
                        if field in git:
                            git[field] = WIRE_METADATA[
                                "/frame/result/thread/gitInfo/" + field
                            ]
            after = json.dumps(frame)
            lines.append(line if before == after else after + "\n")
        text = "".join(lines)
        manifest = {
            "schema": 1,
            "procedure": "nspawn-wire-public-v1",
            "source": {
                "name": name,
                "sha256": hashlib.sha256(data).hexdigest(),
            },
            "derivative": {
                "name": public_name,
                "sha256": hashlib.sha256(text.encode()).hexdigest(),
            },
            "metadata_fields": WIRE_METADATA,
        }
    encoded = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    decoded_public = "\n".join(
        json.dumps(json.loads(line), ensure_ascii=False)
        for line in text.splitlines()
        if line.strip() and not line.startswith("#")
    )
    for rule, pattern in PRIVATE.items():
        if any(pattern.search(value) for value in (text, encoded, decoded_public)):
            raise Finding(f"wire projection refused: {public_name}: {rule}")
    output.mkdir(parents=True, exist_ok=True)
    destination = output / public_name
    provenance = destination.with_suffix(".projection.json")
    for path, content in ((destination, text), (provenance, encoded)):
        if path.is_symlink() or (path.exists() and path.read_text() != content):
            raise Refusal("wire projection refuses a conflicting destination")
    destination.write_text(text)
    provenance.write_text(encoded)
    return manifest


def public_wire_fixtures(stage: Path, mapping: dict[str, str] | None = None) -> None:
    """Project only the three declared wire recordings in the export stage."""
    relative = (mapping or {}).get("mcps/nspawn", "mcps/nspawn")
    directory = stage / relative / "tests/fixtures"
    if not directory.is_dir():
        return
    for name in WIRE_FIXTURES:
        source = directory / name
        if source.is_file():
            wire_fixture_project(source, directory)
            source.unlink()
        else:
            derivative = directory / name.replace(".ndjson", ".public.ndjson")
            if derivative.is_file():
                wire_fixture_project(derivative, directory)
    # This generated copy lets public contributors run the same selected policy.
    helper = directory / "projection.py"
    content = Path(__file__).read_bytes()
    if helper.is_symlink() or (helper.exists() and helper.read_bytes() != content):
        raise Refusal("wire projection refuses a conflicting procedure")
    helper.write_bytes(content)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Create a public wire derivative from an owned input."
    )
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    arguments = parser.parse_args()
    wire_fixture_project(arguments.input, arguments.output)
