"""U3: extra readable trees never widen a child's write authority."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from class_dispatch import DECLARED_CLASS
from mcp_fakes import FakeSupervisor, handle

from nspawn import child
from nspawn.mcp import INVALID_PARAMS, NSPAWN_TOOL_CONTRACTS, McpServer
from nspawn.supervisor import RunAccepted, Supervisor

READ_PATH_LIMIT = 8


def _contract(name: str) -> dict[str, Any]:
    return next(
        contract.input_schema
        for contract in NSPAWN_TOOL_CONTRACTS
        if contract.name == name
    )


def _request(server: McpServer, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    answer = handle(
        server,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    assert answer is not None
    return answer


def _read_trees(tmp_path: Path) -> list[str]:
    paths = (tmp_path / "donor", tmp_path / "a path;printf HACKED")
    for path in paths:
        path.mkdir()
    return [str(path) for path in paths]


def test_read_paths_are_declared_bounded_and_explain_the_cage() -> None:
    for name in ("nspawn_run_start", "nspawn_review_start", "nspawn_batch_start"):
        properties = _contract(name)["properties"]
        read_paths = properties["read_paths"]
        assert read_paths["type"] == "array"
        assert read_paths["minItems"] == 1
        assert read_paths["maxItems"] == READ_PATH_LIMIT
        assert read_paths["items"] == {"type": "string", "minLength": 1}
        assert "cwd" in properties["cwd"]["description"]
        assert "read_paths" in properties["cwd"]["description"]

    execute = _contract("nspawn_run_start")["properties"]
    assert "provider wins" in execute["provider"]["description"]
    assert "provider wins" in execute["class"]["description"]


def test_mcp_routes_read_paths_only_to_caged_dispatches(tmp_path: Path) -> None:
    source = tmp_path / "readable-source"
    source.mkdir()
    donor = str(source)

    execute = FakeSupervisor(execute_result=RunAccepted(run_id="execute-1"))
    answer = _request(
        McpServer(supervisor=execute),
        "nspawn_run_start",
        {
            "prompt": "audit",
            "cwd": "/worktree",
            # Auditoria de leitura: trabalho mecanico numa arvore engaiolada.
            "class": DECLARED_CLASS,
            "read_only": True,
            "read_paths": [donor],
        },
    )
    assert "error" not in answer
    # U1: the arguments cross the driver frame as JSON and are re-normalised to
    # the tuple `execute` takes, exactly as `nspawn_batch_start` always handed them.
    assert execute.calls[0][1]["read_paths"] == (donor,)
    assert execute.calls[0][1]["read_only"] is True

    review = FakeSupervisor(review_result=RunAccepted(run_id="review-1"))
    answer = _request(
        McpServer(supervisor=review),
        "nspawn_review_start",
        {"branch": "lane/x", "cwd": "/worktree", "read_paths": [donor]},
    )
    assert "error" not in answer
    assert review.calls[0][1]["read_paths"] == (donor,)

    batch = FakeSupervisor(execute_result=RunAccepted(run_id="batch-1"))
    answer = _request(
        McpServer(supervisor=batch),
        "nspawn_batch_start",
        {
            "prompt": "compare",
            "cwd": "/worktree",
            "providers": ["x"],
            "read_paths": [donor],
        },
    )
    assert "error" not in answer
    assert batch.calls[0][1]["read_paths"] == (donor,)
    assert batch.calls[0][1]["read_only"] is True

    writable = FakeSupervisor(execute_result=RunAccepted(run_id="unreachable"))
    refused = _request(
        McpServer(supervisor=writable),
        "nspawn_run_start",
        {
            "prompt": "write",
            "cwd": "/worktree",
            "class": DECLARED_CLASS,
            "read_paths": [donor],
        },
    )
    assert refused["error"]["code"] == INVALID_PARAMS
    assert "requires read_only=true" in refused["error"]["message"]
    assert writable.calls == []


@pytest.mark.parametrize("path", ["relative/tree", "../outside"])
def test_read_paths_refuse_relative_entries(path: str) -> None:
    with pytest.raises(child.ChildArgvError, match="absolute"):
        child.validate_read_paths([path], read_only=True)


def test_read_paths_refuse_parent_components_in_absolute_paths() -> None:
    with pytest.raises(child.ChildArgvError, match="parent traversal"):
        child.validate_read_paths(["/tmp/../etc"], read_only=True)


def test_read_paths_refuse_missing_trees(tmp_path: Path) -> None:
    with pytest.raises(child.ChildArgvError, match="existing directories"):
        child.validate_read_paths([str(tmp_path / "missing")], read_only=True)


def test_a_file_entry_is_refused_as_caller_input_not_as_a_spawn_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The binary was fine. The cages are proven for directory grants only, so a file stays refused,
    but the refusal names the cause first.
    """
    diff = tmp_path / "review.diff"
    diff.write_text("--- a\n+++ b\n", encoding="utf-8")
    cause = "read_paths entry is a file; grant its directory"

    with pytest.raises(child.ChildArgvError) as direct:
        child.validate_read_paths([str(diff)], read_only=True)
    assert str(direct.value).startswith(cause), str(direct.value)
    assert "spawn failed" not in str(direct.value)
    assert str(tmp_path) in str(direct.value)

    monkeypatch.setattr(Supervisor, "_admit", lambda *_args, **_kw: None)
    bare = Supervisor.__new__(Supervisor)
    for start in (
        lambda: Supervisor.execute(
            bare, "audit", "/worktree", read_only=True, read_paths=[str(diff)]
        ),
        lambda: Supervisor.review(bare, "lane/x", "/worktree", read_paths=[str(diff)]),
    ):
        with pytest.raises(child.ChildArgvError) as refused:
            start()
        assert str(refused.value).startswith(cause), str(refused.value)


def test_supervisor_validates_before_admission_and_preserves_each_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[dict[str, object]] = []

    def _capture(
        self: Supervisor, prompt: str, cwd: str, **kwargs: object
    ) -> RunAccepted:
        seen.append(kwargs)
        return RunAccepted(run_id=f"run-{len(seen)}")

    monkeypatch.setattr(Supervisor, "_admit", _capture)
    bare = Supervisor.__new__(Supervisor)
    paths = _read_trees(tmp_path)

    Supervisor.execute(bare, "audit", "/worktree", read_only=True, read_paths=paths)
    Supervisor.review(bare, "lane/x", "/worktree", read_paths=paths)

    assert seen[0]["read_paths"] == tuple(paths)
    assert seen[0]["read_only"] is True
    assert seen[1]["read_paths"] == tuple(paths)
    assert seen[1]["read_only"] is True
    with pytest.raises(child.ChildArgvError, match="requires read_only"):
        Supervisor.execute(bare, "write", "/worktree", read_paths=paths)
    assert len(seen) == 2


def test_child_argv_grants_exact_paths_beside_an_unbroken_cage(
    tmp_path: Path,
) -> None:
    paths = _read_trees(tmp_path)

    argv = child.compose_child_argv("/bin/claude", read_only=True, read_paths=paths)

    add_dir_indexes = [
        index for index, argument in enumerate(argv) if argument == "--add-dir"
    ]
    assert [argv[index + 1] for index in add_dir_indexes] == paths
    assert len(add_dir_indexes) == len(paths)
    assert argv.index("--restricted") < add_dir_indexes[0]
    assert "--tools" in argv
    assert "--" not in argv  # the prompt rides on stdin

    with pytest.raises(child.ChildArgvError, match="requires read_only"):
        child.compose_child_argv("/bin/claude", read_paths=paths)
    with pytest.raises(child.ChildArgvError, match="at most 8"):
        child.compose_child_argv(
            "/bin/claude",
            read_only=True,
            read_paths=[f"/read/{index}" for index in range(READ_PATH_LIMIT + 1)],
        )


def test_no_extra_trees_is_not_an_error_at_any_hop() -> None:
    """`validate_read_paths` must accept its OWN answer for "nothing granted".

    Measured 2026-08-29 on rel-7039467: a caged run with no extra trees died
    with `read_paths must be a non-empty sequence`. The supervisor validates
    once, gets `()` back, carries it down, and `compose_child_argv` validates
    the SAME value again — where the emptiness check rejects it. The verb was
    unusable and `nspawn_batch_start`, caged by construction, was unusable with it.
    An empty grant is not a malformed grant; it is no grant, exactly like
    `None`.
    """
    assert child.validate_read_paths(None, read_only=True) == ()
    assert child.validate_read_paths((), read_only=True) == ()
    assert child.validate_read_paths([], read_only=False) == ()

    argv = child.compose_child_argv("/bin/claude", read_only=True)
    assert "--add-dir" not in argv
    assert (
        child.compose_child_argv("/bin/claude", read_only=True, read_paths=()) == argv
    )

    # The guards that must SURVIVE the change: a string is not a sequence of
    # paths, and a real grant still demands the cage.
    with pytest.raises(child.ChildArgvError, match="not a string"):
        child.validate_read_paths("/read/one", read_only=True)
    with pytest.raises(child.ChildArgvError, match="requires read_only"):
        child.validate_read_paths(["/read/one"], read_only=False)


def test_validate_read_paths_refuses_invalid_path_shapes(tmp_path: Path) -> None:
    from nspawn.child import ChildArgvError, validate_read_paths

    with pytest.raises(ChildArgvError, match="must be absolute directory paths"):
        validate_read_paths(["relative/path"], read_only=True)

    with pytest.raises(ChildArgvError, match="cannot contain parent traversal"):
        validate_read_paths([str(tmp_path / "a" / ".." / "b")], read_only=True)

    non_dir = tmp_path / "file.txt"
    non_dir.write_text("not a dir")
    with pytest.raises(ChildArgvError, match="is a file; grant its directory"):
        validate_read_paths([str(non_dir)], read_only=True)
