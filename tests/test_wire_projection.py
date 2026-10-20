"""Check public wire derivatives without changing the recorded observations."""

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from workspace_root import workspace_path

from nmemory.commands import projection

NAMES = (
    "claude-stream-wire-2.1.250.ndjson",
    "claude-stream-wire-2.1.251.ndjson",
    "codex-appserver-wire-0.153.4.ndjson",
)


def recording(name: str) -> Path:
    directory = workspace_path("mcps/nspawn/tests/fixtures")
    path = directory / name
    return (
        path
        if path.is_file()
        else directory / name.replace(".ndjson", ".public.ndjson")
    )


@pytest.mark.parametrize("name", NAMES)
def test_wire_projection_preserves_observations(name: str, tmp_path: Path) -> None:
    source = recording(name)
    before = source.read_bytes()
    output = tmp_path / "public"
    output.mkdir()
    manifest = projection.wire_fixture_project(source, output)
    derivative = output / name.replace(".ndjson", ".public.ndjson")
    assert source.read_bytes() == before
    assert manifest["derivative"] == {
        "name": derivative.name,
        "sha256": hashlib.sha256(derivative.read_bytes()).hexdigest(),
    }
    assert projection.scan(output) == []
    spec = importlib.util.spec_from_file_location(
        "wire_fixture_data", workspace_path("mcps/nspawn/tests/fixture_data.py")
    )
    assert spec is not None and spec.loader is not None
    fixture_data = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture_data)
    assert manifest["source"]["sha256"] == fixture_data.WIRE_DIGESTS[name][0]
    assert manifest["derivative"]["sha256"] == fixture_data.WIRE_DIGESTS[name][1]
    assert fixture_data.wire_fixture(name, output) == derivative
    expected = [
        json.loads(line) for line in before.splitlines() if line.startswith(b"{")
    ]
    actual = [
        json.loads(line)
        for line in derivative.read_bytes().splitlines()
        if line.startswith(b"{")
    ]
    changed = []

    def compare(left: object, right: object, path: str = "") -> None:
        if isinstance(left, dict):
            assert isinstance(right, dict) and left.keys() == right.keys()
            for key in left:
                compare(left[key], right[key], path + "/" + key)
        elif isinstance(left, list):
            assert isinstance(right, list) and len(left) == len(right)
            for index, value in enumerate(left):
                compare(value, right[index], path + "/" + str(index))
        elif left != right:
            assert path in {
                "/cwd",
                "/memory_paths/auto",
                "/messaging_socket_path",
                "/frame/result/thread/gitInfo/sha",
                "/frame/result/thread/gitInfo/branch",
                "/frame/result/thread/gitInfo/originUrl",
            } or (path.startswith("/plugins/") and path.endswith("/path"))
            assert right in {
                "<CAPTURE_CWD>",
                "<PLUGIN_PATH>",
                "<MEMORY_PATH>",
                "<SOCKET_PATH>",
                "<GIT_SHA>",
                "<GIT_BRANCH>",
                "<GIT_ORIGIN>",
            }
            changed.append(path)

    assert len(expected) == len(actual)
    for left, right in zip(expected, actual, strict=True):
        compare(left, right)
    if not source.name.endswith(".public.ndjson"):
        assert changed
        assert manifest["source"] == {
            "name": name,
            "sha256": hashlib.sha256(before).hexdigest(),
        }
    repeated = tmp_path / "repeated"
    repeated.mkdir()
    assert projection.wire_fixture_project(derivative, repeated) == manifest
    assert derivative.read_bytes() == (repeated / derivative.name).read_bytes()


@pytest.mark.parametrize("relative", ["mcps/nspawn", ".", "packages/nspawn"])
def test_wire_projection_selects_each_export_layout(
    relative: str, tmp_path: Path
) -> None:
    fixtures = tmp_path / relative / "tests/fixtures"
    fixtures.mkdir(parents=True)
    for name in NAMES:
        source = recording(name)
        shutil.copy2(source, fixtures)
        if source.name.endswith(".public.ndjson"):
            shutil.copy2(source.with_suffix(".projection.json"), fixtures)
    projection.public_wire_fixtures(tmp_path, {"mcps/nspawn": relative})
    assert projection.scan(tmp_path) == []
    assert all(not (fixtures / name).exists() for name in NAMES)
    assert all(
        (fixtures / name.replace(".ndjson", ".public.ndjson")).is_file()
        for name in NAMES
    )
    helper = fixtures / "projection.py"
    assert helper.is_file()
    derivative = fixtures / NAMES[0].replace(".ndjson", ".public.ndjson")
    reproduced = tmp_path / "reproduced"
    result = subprocess.run(
        [sys.executable, "-I", str(helper), str(derivative), str(reproduced)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert (reproduced / derivative.name).read_bytes() == derivative.read_bytes()
    assert (
        reproduced / derivative.with_suffix(".projection.json").name
    ).read_bytes() == derivative.with_suffix(".projection.json").read_bytes()


# These credential shapes use only public, synthetic bytes. No key exists.
@pytest.mark.parametrize("rule", ["credential", "private-key"])
def test_wire_projection_refuses_credentials_before_changes(
    rule: str, tmp_path: Path
) -> None:
    source = tmp_path / NAMES[0]
    source.write_text(
        json.dumps(
            {
                "type": "system",
                "subtype": "init",
                "cwd": (
                    "ghp_" + "0" * 36
                    if rule == "credential"
                    else "-----BEGIN " + "PRIVATE KEY-----"
                ),
            }
        )
        + "\n"
    )
    output = tmp_path / "output"
    output.mkdir()
    with pytest.raises(projection.Finding, match=rule):
        projection.wire_fixture_project(source, output)
    assert list(output.iterdir()) == []


def test_wire_projection_keeps_result_content(tmp_path: Path) -> None:
    source = tmp_path / NAMES[0]
    source.write_text('{"type":"result","result":"PONG"}\n')
    output = tmp_path / "output"
    output.mkdir()
    projection.wire_fixture_project(source, output)
    derivative = output / NAMES[0].replace(".ndjson", ".public.ndjson")
    assert derivative.read_bytes() == source.read_bytes()


def test_wire_projection_refuses_decoded_credentials(tmp_path: Path) -> None:
    source = tmp_path / NAMES[0]
    text = json.dumps({"type": "system", "subtype": "init", "cwd": "ghp_" + "0" * 36})
    source.write_text(text.replace("ghp_", "\\u0067hp_") + "\n")
    output = tmp_path / "output"
    with pytest.raises(projection.Finding, match="credential"):
        projection.wire_fixture_project(source, output)
    assert not output.exists()


@pytest.mark.parametrize("escaped", [False, True])
def test_wire_projection_refuses_content_outside_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, escaped: bool
) -> None:
    import re

    # The marker is synthetic. This additional detector cannot remove a real guard.
    monkeypatch.setitem(
        projection.PRIVATE, "synthetic-content", re.compile("PRIVATE_FIXTURE_CONTENT")
    )
    source = tmp_path / NAMES[0]
    text = '{"type":"result","result":"PRIVATE_FIXTURE_CONTENT"}\n'
    source.write_text(text.replace("PRIVATE_", "\\u0050RIVATE_") if escaped else text)
    output = tmp_path / "output"
    with pytest.raises(projection.Finding, match="synthetic-content"):
        projection.wire_fixture_project(source, output)
    assert not output.exists()
