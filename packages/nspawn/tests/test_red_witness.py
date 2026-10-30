"""Pure reader guards; producer execution stays in the harness."""

from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from nspawn.red_witness import (
    RedRefused,
    bundle_paths,
    read_bundle,
    read_regular,
    record,
    validate_bundle,
)

HEAD = "a" * 40
LOCATOR = ".nott/plans/reader/red/unit.json"
JUDGED = b"""## Failure space
- input request -> guard
- failure omission -> guard
- touch none: pure reader

## Test guard
- `file`: tests/test_reader.py
- `name`: test_guard
- `covers`: omission
- `fails today`: missing admission, reader.py:1
"""


def producer() -> bytes:
    return (
        json.dumps(
            {
                "schema": "nott.plan.red.v1",
                "tests": "tests/unit.md",
                "tests_sha256": hashlib.sha256(JUDGED).hexdigest(),
                "witnesses": {
                    "guard": {
                        "file": "tests/test_reader.py",
                        "name": "test_guard",
                        "command": "pytest tests/test_reader.py::test_guard",
                        "exit": 1,
                        "assertion": "assert admitted is False",
                        "base": HEAD,
                    }
                },
                "refused": {},
            },
            indent=2,
        ).encode()
        + b"\n"
    )


def test_snapshot_preserves_exact_bytes_and_is_immutable(tmp_path: Path) -> None:
    tests, _ = bundle_paths(LOCATOR)
    for name, raw in ((LOCATOR, producer()), (tests, JUDGED)):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    bundle = read_bundle(tmp_path, LOCATOR, base=HEAD)
    assert bundle.record_bytes == producer()
    assert bundle.tests_bytes == JUDGED
    assert bundle.base == HEAD
    assert tuple(test.id for test in bundle.tests) == ("guard",)
    with pytest.raises(FrozenInstanceError):
        bundle.base = "b" * 40


@pytest.mark.parametrize(
    "raw", [b"[" * 10000, b"{", b"\xff", b'{"schema": 1, "schema": 2}']
)
def test_malformed_json_is_a_typed_refusal(raw: bytes) -> None:
    with pytest.raises(RedRefused):
        record(raw)


@pytest.mark.parametrize("field", ["record", "tests"])
def test_in_memory_snapshots_obey_the_file_limit(field: str) -> None:
    raw, tests = producer(), JUDGED
    if field == "record":
        raw = b" " * (256 * 1024 + 1)
    else:
        tests = b" " * (256 * 1024 + 1)
    with pytest.raises(RedRefused, match="bounded"):
        validate_bundle(raw, tests, unit="unit", base=HEAD)


@pytest.mark.parametrize("target", ["leaf", "directory"])
def test_no_symlink_component_is_followed(tmp_path: Path, target: str) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / "file").write_bytes(b"source")
    if target == "leaf":
        (tmp_path / "link").symlink_to(real / "file")
        relative = "link"
    else:
        (tmp_path / "link").symlink_to(real, target_is_directory=True)
        relative = "link/file"
    with pytest.raises(RedRefused):
        read_regular(tmp_path, relative)


def test_reader_never_executes_witness_commands(tmp_path: Path) -> None:
    marker = tmp_path / "executed"
    data = json.loads(producer())
    data["witnesses"]["guard"]["command"] = f"touch {marker}"
    bundle = validate_bundle(json.dumps(data).encode(), JUDGED, unit="unit", base=HEAD)
    assert bundle.base == HEAD
    assert not marker.exists()


def test_changed_judged_bytes_are_not_the_same_bundle() -> None:
    with pytest.raises(RedRefused, match="digest"):
        validate_bundle(producer(), JUDGED + b"\n", unit="unit", base=HEAD)
