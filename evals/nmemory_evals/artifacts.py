"""Deterministic artifact bundle writer."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from enum import Enum
from pathlib import Path
from typing import Any

from nmemory_evals.path import directory_open, file_replace

BENCH_VERSION = "0.1.0"


@dataclasses.dataclass(frozen=True)
class ConfigArtifact:
    bench: str
    seed: int
    binary_path: str
    binary_sha256: str
    services: int
    engineers: int
    events: int
    questions: int
    oow_questions: int
    k: int
    bench_version: str


class ArtifactError(Exception):
    """Artifact I/O or serialization failure."""


def file_sha256(path: Path | str) -> str:
    """The SHA-256 of one file, streamed in 64KB chunks."""
    p = Path(path)
    hasher = hashlib.sha256()
    try:
        with open(p, "rb") as f:
            while chunk := f.read(64 * 1024):
                hasher.update(chunk)
    except OSError as err:
        raise ArtifactError(f"artifact I/O failed at {p}: {err}") from err
    return hasher.hexdigest()


def hex_digest(digest: bytes) -> str:
    """Lowercase hex string representation of bytes."""
    return digest.hex()


def _to_serializable(obj: Any) -> Any:
    """Recursively convert dataclasses and enums to JSON-serializable primitives."""
    if isinstance(obj, Enum):
        return obj.value
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {
            k: _to_serializable(v)
            for k, v in dataclasses.asdict(obj).items()
            if v is not None
        }
    if isinstance(obj, (list, tuple)):
        return [_to_serializable(item) for item in obj]
    if isinstance(obj, dict):
        return {str(k): _to_serializable(v) for k, v in obj.items() if v is not None}
    return obj


def write_json(path: Path, value: Any) -> None:
    """Write pretty JSON with 2-space indent and trailing newline."""
    try:
        serializable = _to_serializable(value)
        content = json.dumps(serializable, indent=2) + "\n"
        with directory_open(path.parent, create=True) as directory:
            file_replace(directory, path.name, [content])
    except OSError as err:
        raise ArtifactError(f"artifact I/O failed at {path}: {err}") from err


def write_transcript(path: Path, rows: list[Any]) -> None:
    """Write NDJSON transcript rows with trailing newline per entry."""
    try:
        with directory_open(path.parent, create=True) as directory:
            file_replace(
                directory,
                path.name,
                (
                    json.dumps(_to_serializable(row), separators=(",", ":")) + "\n"
                    for row in rows
                ),
            )
    except OSError as err:
        raise ArtifactError(f"artifact I/O failed at {path}: {err}") from err


def write_bundle(
    out: Path | str,
    config: ConfigArtifact,
    world: Any,
    questions: list[Any],
    transcript: list[Any],
    scores: Any,
) -> None:
    """Write standard n1 evaluation artifact bundle."""
    out_path = Path(out)
    write_json(out_path / "config.json", config)
    write_json(out_path / "world.json", world)
    write_json(out_path / "qa.json", questions)
    write_transcript(out_path / "transcript.jsonl", transcript)
    write_json(out_path / "scores.json", scores)


def write_report(path: Path | str, value: Any) -> None:
    """Write single pretty JSON report with trailing newline."""
    write_json(Path(path), value)
