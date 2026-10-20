"""The params ceiling and lossless result spill at the tool-call boundary.

Each call is followed by another the same session answers: a refusal or a
spill never takes the session down."""

import json
import os
from pathlib import Path
from typing import Any

import pytest
from class_dispatch import DECLARED_CLASS
from mcp_fakes import FakeSupervisor, handle

from nspawn import mcp
from nspawn.supervisor import RunAccepted, RunResult, create_run_directory


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> mcp.McpServer:
    fake = FakeSupervisor(execute_result=RunAccepted(run_id="r1"))
    fake._run_root = tmp_path
    assert fake._run_root is not None
    create_run_directory(tmp_path, "r1", dir_fd=fake._run_root)
    monkeypatch.setattr(fake, "shutdown", lambda: None)
    monkeypatch.setattr(fake, "fast_models", lambda cwd: {})
    return mcp.McpServer(supervisor=fake)


def drive(server: mcp.McpServer, params: dict[str, Any]) -> list[dict[str, Any]]:
    frames = [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "nspawn_route_ping", "arguments": {"cwd": "/work"}},
        },
    ]
    answers = [handle(server, frame) for frame in frames]
    assert answers[1]["id"] == 2
    assert json.loads(answers[1]["result"]["content"][0]["text"])["routes"] == []
    return answers


@pytest.mark.parametrize("character", ["x", "é"])
def test_oversized_params_refuse_and_the_stdio_session_answers_next_ping(
    server: mcp.McpServer,
    character: str,
) -> None:
    params = {
        "name": "nspawn_run_start",
        "arguments": {
            "prompt": character * (1024 * 1024 // len(character.encode("utf-8"))),
            "cwd": "/work",
        },
    }
    size = len(
        json.dumps(params, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    )
    assert size > mcp.MAX_MCP_PARAMS_BYTES
    answer = drive(server, params)[0]
    assert answer["id"] == 1
    assert answer["error"]["code"] == -32602
    assert str(1024 * 1024) in answer["error"]["message"]
    assert str(size) in answer["error"]["message"]


@pytest.mark.parametrize("size", [65537, 1024 * 1024 + 1])
def test_large_result_spills_complete_bytes_and_keeps_stdio_alive(
    server: mcp.McpServer, tmp_path: Path, size: int
) -> None:
    fake = server.supervisor
    assert isinstance(fake, FakeSupervisor)
    text = 'é\\"\n' * size
    fake.result_result = RunResult(run_id="r1", final_text=text)
    expected = json.dumps(
        {"run_id": "r1", "final_text": text, "stderr_tail": None},
        separators=(",", ":"),
    ).encode()
    answer = drive(
        server, {"name": "nspawn_result_show", "arguments": {"run_id": "r1"}}
    )[0]
    assert answer["result"]["isError"] is False
    spilled = json.loads(answer["result"]["content"][0]["text"])
    assert spilled["preview"].encode() == expected[:65536]
    assert spilled["bytes"] == len(expected)
    path = Path(spilled["path"])
    assert path.is_absolute()
    assert path.is_relative_to(tmp_path / "r1")
    assert path.read_bytes() == expected
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("character", ["x", "é"])
def test_params_ceiling_at_the_dispatch_boundary(
    server: mcp.McpServer,
    character: str,
) -> None:
    # The class enters BEFORE `overhead` is measured: the ceiling measured here is for
    # frame bytes, and padding must account for the mandatory argument.
    params = {
        "name": "nspawn_run_start",
        "arguments": {"prompt": "", "cwd": "/work", "class": DECLARED_CLASS},
    }
    overhead = len(
        json.dumps(params, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    )
    count, remainder = divmod(1024 * 1024 - overhead, len(character.encode("utf-8")))
    params["arguments"]["prompt"] = character * count + "x" * remainder
    frame = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}
    accepted = handle(server, frame)
    assert accepted is not None and "result" in accepted
    params["arguments"]["prompt"] += "x"
    refused = handle(server, frame)
    assert refused is not None
    assert refused["error"]["code"] == -32602
    assert "1048577" in refused["error"]["message"]
    assert "1048576" in refused["error"]["message"]
    fake = server.supervisor
    assert isinstance(fake, FakeSupervisor)
    assert len(fake.calls) == 1


def test_spill_write_failure_is_typed_and_session_survives(
    server: mcp.McpServer, tmp_path: Path
) -> None:
    fake = server.supervisor
    assert isinstance(fake, FakeSupervisor)
    fake.result_result = RunResult(run_id="r1", final_text="x" * 65537)
    (tmp_path / "r1").rmdir()
    answer = drive(
        server, {"name": "nspawn_result_show", "arguments": {"run_id": "r1"}}
    )[0]
    assert answer["error"]["code"] == mcp.INTERNAL_ERROR
    assert "spill failed" in answer["error"]["message"]
    assert not list(tmp_path.iterdir())


def test_intermediate_ancestor_swap_after_resolve_before_stat_cannot_spill_outside_intended_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ancestor = tmp_path / "ancestor"
    root = ancestor / "runs"
    directory = root / "r1"
    root.mkdir(parents=True)
    fake = FakeSupervisor(execute_result=RunAccepted(run_id="r1"))
    fake._run_root = root
    assert fake._run_root is not None
    create_run_directory(root, "r1", dir_fd=fake._run_root)
    intended_stat = directory.stat()
    moved = tmp_path / "ancestor-original"

    fake.result_result = RunResult(run_id="r1", final_text="x" * 65537)
    monkeypatch.setattr(fake, "shutdown", lambda: None)
    monkeypatch.setattr(fake, "fast_models", lambda cwd: {})
    server = mcp.McpServer(supervisor=fake)

    ancestor.rename(moved)
    directory.mkdir(parents=True)
    answer = drive(
        server, {"name": "nspawn_result_show", "arguments": {"run_id": "r1"}}
    )[0]
    if "error" in answer:
        assert answer["error"]["code"] == mcp.INTERNAL_ERROR
        assert not list(directory.iterdir())
        assert not list((moved / "runs" / "r1").iterdir())
    else:
        payload = json.loads(answer["result"]["content"][0]["text"])
        path = Path(payload["path"])
        assert os.path.samestat(path.parent.stat(), intended_stat)


def test_intermediate_ancestor_swap_cannot_spill_outside_intended_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ancestor = tmp_path / "ancestor"
    root = ancestor / "runs"
    directory = root / "r1"
    root.mkdir(parents=True)
    fake = FakeSupervisor(execute_result=RunAccepted(run_id="r1"))
    fake._run_root = root
    assert fake._run_root is not None
    create_run_directory(root, "r1", dir_fd=fake._run_root)
    intended_stat = directory.stat()
    moved = tmp_path / "ancestor-original"

    fake.result_result = RunResult(run_id="r1", final_text="x" * 65537)
    monkeypatch.setattr(fake, "shutdown", lambda: None)
    monkeypatch.setattr(fake, "fast_models", lambda cwd: {})
    server = mcp.McpServer(supervisor=fake)

    original = os.open
    swapped = False

    def swap_then_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal swapped
        binding = flags & os.O_DIRECTORY and (
            path == directory
            or (path == directory.name and kwargs.get("dir_fd") is not None)
        )
        if not swapped and binding:
            ancestor.rename(moved)
            directory.mkdir(parents=True)
            swapped = True
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap_then_open)
    answer = drive(
        server, {"name": "nspawn_result_show", "arguments": {"run_id": "r1"}}
    )[0]
    assert swapped, "the directory open must be intercepted"
    if "error" in answer:
        assert answer["error"]["code"] == mcp.INTERNAL_ERROR
        assert not list(directory.iterdir())
        assert not list((moved / "runs" / "r1").iterdir())
    else:
        payload = json.loads(answer["result"]["content"][0]["text"])
        path = Path(payload["path"])
        assert os.path.samestat(path.parent.stat(), intended_stat)


@pytest.mark.parametrize("when", ["root_bind", "during_resolve"])
def test_ancestor_swap_before_the_root_hold_cannot_spill_outside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, when: str
) -> None:
    ancestor = tmp_path / "ancestor"
    root = ancestor / "runs"
    directory = root / "r1"
    root.mkdir(parents=True)
    fake = FakeSupervisor(execute_result=RunAccepted(run_id="r1"))
    fake._run_root = root
    assert fake._run_root is not None
    create_run_directory(root, "r1", dir_fd=fake._run_root)
    intended_stat = directory.stat()
    moved = tmp_path / "ancestor-original"

    fake.result_result = RunResult(run_id="r1", final_text="x" * 65537)
    monkeypatch.setattr(fake, "shutdown", lambda: None)
    monkeypatch.setattr(fake, "fast_models", lambda cwd: {})
    server = mcp.McpServer(supervisor=fake)

    original_open = os.open
    original_resolve = Path.resolve
    swapped = False

    def do_swap() -> None:
        ancestor.rename(moved)
        directory.mkdir(parents=True)

    def swap_then_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal swapped
        at_root_bind = (
            when == "root_bind"
            and flags & os.O_DIRECTORY
            and kwargs.get("dir_fd") is None
            and str(path) == str(root)
        )
        if not swapped and at_root_bind:
            do_swap()
            swapped = True
        return original_open(path, flags, *args, **kwargs)

    def swap_then_resolve(self: Path, *args: Any, **kwargs: Any) -> Path:
        nonlocal swapped
        if not swapped and when == "during_resolve" and self == root:
            do_swap()
            swapped = True
        return original_resolve(self, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap_then_open)
    monkeypatch.setattr(Path, "resolve", swap_then_resolve)

    answer = drive(
        server, {"name": "nspawn_result_show", "arguments": {"run_id": "r1"}}
    )[0]
    assert not swapped, f"path-derived root open reintroduced at {when}"

    if "error" in answer:
        assert answer["error"]["code"] == mcp.INTERNAL_ERROR
        assert not list(directory.iterdir())
        assert not list((moved / "runs" / "r1").iterdir())
    else:
        payload = json.loads(answer["result"]["content"][0]["text"])
        path = Path(payload["path"])
        assert os.path.samestat(path.parent.stat(), intended_stat), (
            f"ESCAPED when={when}: published parent inode "
            f"{path.parent.stat().st_ino} != intended {intended_stat.st_ino}; "
            f"intended tree now at {moved!s} holds {list((moved / 'runs' / 'r1').iterdir())}"
        )


@pytest.mark.parametrize("when", ["before_bind", "after_bind"])
def test_descriptor_spill_refuses_directory_replacement(
    server: mcp.McpServer,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    when: str,
) -> None:
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.mkdir()
    directory = tmp_path / "r1"
    moved = tmp_path / "r1-original"
    original = os.open
    swapped = False

    def swap_then_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal swapped
        binding = flags & os.O_DIRECTORY and (
            path == directory
            or (path == directory.name and kwargs.get("dir_fd") is not None)
        )
        creating = kwargs.get("dir_fd") is not None and flags & os.O_CREAT
        if not swapped and (binding if when == "before_bind" else creating):
            directory.rename(moved)
            directory.symlink_to(outside, target_is_directory=True)
            swapped = True
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap_then_open)
    fake = server.supervisor
    assert isinstance(fake, FakeSupervisor)
    fake.result_result = RunResult(run_id="r1", final_text="x" * 65537)
    answer = drive(
        server, {"name": "nspawn_result_show", "arguments": {"run_id": "r1"}}
    )[0]
    assert swapped, "the real descriptor-open boundary must be exercised"
    assert answer["error"]["code"] == mcp.INTERNAL_ERROR
    assert not list(outside.iterdir())
    assert not list(moved.iterdir()), "cleanup must address the bound directory"


def test_result_without_run_allocates_private_spill_directory(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    rendered = {"text": "x" * 65537}
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        answer = mcp._bounded_result(rendered, fd)
    finally:
        os.close(fd)
    payload = json.loads(answer["content"][0]["text"])
    path = Path(payload["path"])
    assert path.parent.parent == root
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert json.loads(path.read_bytes()) == rendered


@pytest.mark.parametrize("run_id", [1, "", "/outside", ".", "..", "bad\x00id"])
def test_unsafe_spill_id_is_refused(tmp_path: Path, run_id: Any) -> None:
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with pytest.raises(mcp.McpError, match="unsafe spill run_id") as error:
            mcp._bounded_result({"run_id": run_id, "text": "x" * 65537}, fd)
    finally:
        os.close(fd)
    assert error.value.code == mcp.INTERNAL_ERROR
    assert not list(tmp_path.iterdir())


def test_existing_symlink_spill_directory_is_refused(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "r1").symlink_to(outside, target_is_directory=True)
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with pytest.raises(mcp.McpError, match="spill run directory is a symlink"):
            mcp._bounded_result({"run_id": "r1", "text": "x" * 65537}, fd)
    finally:
        os.close(fd)
    assert not list(outside.iterdir())


def test_spill_creation_failure_closes_directory_and_preserves_session(
    server: mcp.McpServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = os.open
    bound: list[int] = []

    def refuse_creation(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if kwargs.get("dir_fd") is not None and flags & os.O_CREAT:
            bound.append(kwargs["dir_fd"])
            raise PermissionError("spill creation refused")
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", refuse_creation)
    fake = server.supervisor
    assert isinstance(fake, FakeSupervisor)
    fake.result_result = RunResult(run_id="r1", final_text="x" * 65537)
    answer = drive(
        server, {"name": "nspawn_result_show", "arguments": {"run_id": "r1"}}
    )[0]
    assert answer["error"]["code"] == mcp.INTERNAL_ERROR
    assert "PermissionError" in answer["error"]["message"]
    assert not list((tmp_path / "r1").iterdir())
    assert len(bound) == 1
    with pytest.raises(OSError):
        os.fstat(bound[0])


@pytest.mark.parametrize("replacement", ["removed", "replaced", "unlink_denied"])
def test_spill_cleanup_preserves_replacement_and_original_error(
    server: mcp.McpServer,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement: str,
) -> None:
    original_stat = os.stat
    original_unlink = os.unlink
    touched: list[Path] = []

    def race_on_stat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        if (
            kwargs.get("dir_fd") is not None
            and not touched
            and str(path).startswith("mcp-result-")
        ):
            artifact = tmp_path / "r1" / path
            touched.append(artifact)
            if replacement == "replaced":
                # Keep the original inode alive so replacement cannot reuse it.
                artifact.rename(artifact.with_suffix(".original"))
                artifact.write_text("replacement")
            elif replacement == "removed":
                original_unlink(path, dir_fd=kwargs["dir_fd"])
            else:
                raise OSError("original inspection failure")
        return original_stat(path, *args, **kwargs)

    def refuse_unlink(path: Any, *args: Any, **kwargs: Any) -> None:
        raise PermissionError("cleanup denied")

    monkeypatch.setattr(os, "stat", race_on_stat)
    if replacement == "unlink_denied":
        monkeypatch.setattr(os, "unlink", refuse_unlink)
    fake = server.supervisor
    assert isinstance(fake, FakeSupervisor)
    fake.result_result = RunResult(run_id="r1", final_text="x" * 65537)
    answer = drive(
        server, {"name": "nspawn_result_show", "arguments": {"run_id": "r1"}}
    )[0]
    assert answer["error"]["code"] == mcp.INTERNAL_ERROR
    assert len(touched) == 1
    artifact = touched[0]
    if replacement == "replaced":
        assert artifact.read_text() == "replacement"
        assert "spill directory or file changed" in answer["error"]["message"]
    elif replacement == "removed":
        assert not artifact.exists()
        assert "FileNotFoundError" in answer["error"]["message"]
    else:
        assert json.loads(artifact.read_bytes())["final_text"] == "x" * 65537
        assert answer["error"]["message"] == "result spill failed: OSError"
