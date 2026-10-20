"""Repository scans preserve admission, source evidence, and proposal review."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from nspawn.mcp import McpError
from test_curator_nspawn import native_curator as native_scan_fixture

from nmemory.curator import CuratorClient, CuratorError
from nmemory.server import MemoryServer
from nmemory.store import Store

native_curator = native_scan_fixture


def scanner() -> Any:
    return importlib.import_module("nmemory.project_scan").scan_project


@pytest.mark.parametrize("payload", ["envelope", "facts", "nul_path"])
def test_scan_malformed_output_raises_curator_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: str
) -> None:
    server = MemoryServer(
        store=Store.open_disposable_test_store(), anchor_root=str(tmp_path)
    )
    depth = sys.getrecursionlimit() + 100
    nested = "[" * depth + "0" + "]" * depth
    output = (
        nested
        if payload == "envelope"
        else json.dumps(
            {
                "result": nested
                if payload == "facts"
                else json.dumps(
                    [{"path": "bad\u0000path", "fact": "A fact.", "quote": "A quote."}]
                )
            }
        )
    )
    client = CuratorClient()
    monkeypatch.setattr(client, "_invoke", lambda *a, **k: output)
    original_loads = json.loads

    def bounded_loads(text: str, *args: Any, **kwargs: Any) -> Any:
        # Python versions use different decoder depth limits.
        if text == nested:
            raise RecursionError("JSON nesting exceeds the decoder limit")
        return original_loads(text, *args, **kwargs)

    monkeypatch.setattr(json, "loads", bounded_loads)
    with pytest.raises(CuratorError, match="repository scan"):
        scanner()(server.handler_state(), tmp_path, "scan", client)


def test_scan_dispatch_and_caller_root_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    class Owner:
        def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
            calls.append((name, args))
            payload = (
                {"run_id": "scan-1"}
                if name.endswith("start")
                else {"phase": "completed"}
                if name.endswith("show")
                else {"lookup": "fenced"}
            )
            return {"content": [{"text": json.dumps(payload)}]}

        def nspawn_result_show(self, run_id: str) -> Any:
            return SimpleNamespace(
                final_text="[]", structured_output=None, failure_reason=None
            )

        def close(self) -> None:
            pass

    monkeypatch.setattr("nspawn.mcp.McpServer", Owner)
    monkeypatch.setattr(
        "nmemory.curator.selected_curator_route",
        lambda: SimpleNamespace(target="fixture"),
    )
    client = CuratorClient(custom_hook=lambda _: None)  # type: ignore[arg-type,return-value]
    monkeypatch.setattr(client, "child_env", dict)
    result = client._invoke("Read repository facts.", None, cwd=tmp_path)
    assert json.loads(result)["result"] == "[]"
    request = calls[0][1]
    assert request["cwd"] == str(tmp_path)
    assert request["read_only"] is True
    assert request["read_paths"] == [str(tmp_path)]
    assert "inference_profile" not in request
    assert tmp_path.is_dir()


def test_scan_admission_failure_preserves_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    class Owner:
        def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
            calls.append(name)
            if name.endswith("start"):
                raise McpError(-32000, "scan dispatch refused")
            return {"content": [{"text": '{"lookup":"fenced"}'}]}

        def close(self) -> None:
            pass

    monkeypatch.setattr("nspawn.mcp.McpServer", Owner)
    monkeypatch.setattr(
        "nmemory.curator.selected_curator_route",
        lambda: SimpleNamespace(target="fixture"),
    )
    client = CuratorClient(custom_hook=lambda _: None)  # type: ignore[arg-type,return-value]
    monkeypatch.setattr(client, "child_env", dict)
    with pytest.raises(CuratorError, match="scan dispatch refused"):
        client._invoke("Read repository facts.", None, cwd=tmp_path)
    assert calls == ["nspawn_run_start", "nspawn_run_close"]
    assert tmp_path.is_dir()


def test_scan_root_mismatch_refuses_before_dispatch(tmp_path: Path) -> None:
    server = MemoryServer(
        store=Store.open_disposable_test_store(), anchor_root=str(tmp_path)
    )
    with pytest.raises(CuratorError, match="anchor root"):
        scanner()(server.handler_state(), tmp_path / "other", "scan", CuratorClient())


@pytest.mark.parametrize("verdict", ["ratified", "rejected"])
def test_scan_verified_sources_are_proposals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verdict: str
) -> None:
    (tmp_path / "README.md").write_text("Header\n" * 50 + "The package uses SQLite.\n")
    store = Store.open_disposable_test_store()
    server = MemoryServer(store=store, anchor_root=str(tmp_path), project_id="scan")
    candidates = [
        {
            "path": "README.md",
            "fact": "The package uses SQLite.",
            "quote": "uses SQLite",
        },
        {"path": "../escape", "fact": "Escaped fact.", "quote": "escape"},
        {"path": "README.md", "fact": "Unsupported fact.", "quote": "uses Postgres"},
    ]
    client = CuratorClient()
    monkeypatch.setattr(
        client,
        "_invoke",
        lambda *a, **k: json.dumps({"result": json.dumps(candidates)}),
    )
    ids = scanner()(server.handler_state(), tmp_path, "scan", client)
    assert len(ids) == 1
    cid = ids[0]
    assert store.reviews.review_verdict(cid) == "proposed"
    assert store.capsules.get(cid).capsule.provenance.anchor == "README.md:51"
    assert store.classifications.get_classification(cid).kind.value == "fact"

    def recalled() -> list[Any]:
        result = server.call_tool(
            "nmemory_capsule_retrieve", {"terms": ["SQLite"], "project_id": "scan"}
        )
        payload = json.loads(result["result"]["content"][0]["text"])
        return payload.get("capsules", payload.get("results", []))

    assert recalled() == []
    assert store.epistemics.get_source_quote(cid)["verdict"] == "verified"
    reply = server.call_tool(
        "nmemory_proposal_review",
        {
            "capsule_id": cid,
            "verdict": verdict,
            "actor": "owner",
            "reason": "Source checked.",
        },
    )
    assert not reply["result"]["isError"]
    assert store.reviews.review_verdict(cid) == verdict
    assert bool(recalled()) is (verdict == "ratified")


def test_scan_command_reports_dispatch_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import nmemory.cli

    store = Store.open_disposable_test_store()
    monkeypatch.setattr(
        "nmemory.commands.project.open_store_from_args", lambda _: store
    )
    monkeypatch.setenv("NOTT_NMEMORY_ANCHOR_ROOT", str(tmp_path))

    def refused(*args: Any, **kwargs: Any) -> str:
        raise CuratorError("scan dispatch refused")

    monkeypatch.setattr(CuratorClient, "_invoke", refused)
    result = nmemory.cli.main(
        ["project", "scan", "--root", str(tmp_path), "--project", "scan"]
    )
    assert result == 2
    assert "scan dispatch refused" in capsys.readouterr().err
    assert tmp_path.is_dir()


def test_scan_has_one_read_only_native_run(
    native_curator: Path, tmp_path: Path
) -> None:
    import sys

    from nspawn.registry import Registry, store_path

    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"nmemory": {"command": "/bin/false", "args": []}}})
    )
    roster = tmp_path / "spawn.toml"
    roster.write_text(
        "[defaults]\nplugins = [" + json.dumps(str(plugin)) + "]\n" + roster.read_text()
    )
    binary = native_curator.with_name("claude")
    binary.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "if sys.argv[1:] == ['--version']:\n"
        " print('2.1.296 (Claude Code)')\n"
        "else:\n"
        " sys.stdin.read()\n"
        " print(json.dumps({'type':'result','subtype':'success',"
        "'is_error':False,'terminal_reason':'completed','result':'[]',"
        "'duration_ms':1,'duration_api_ms':1,'num_turns':1,'usage':{},"
        "'total_cost_usd':0}))\n"
    )
    server = MemoryServer(
        store=Store.open_disposable_test_store(), anchor_root=str(tmp_path)
    )
    assert (
        scanner()(server.handler_state(), tmp_path, "scan", CuratorClient(timeout_s=20))
        == []
    )
    registry = Registry(store_path(), "scan-proof-reader").connect()
    try:
        rows = registry.list_runs()
        assert len(rows) == 1
        assert rows[0].worktree == str(tmp_path)
        assert registry.inference_profile(rows[0].run_id) is None
    finally:
        registry.close()
    assert tmp_path.is_dir()


def test_scan_ingest_failure_rolls_back_the_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nmemory import project_scan
    from nmemory.tools.ingest import handle

    (tmp_path / "README.md").write_text("The package uses SQLite.\n")
    store = Store.open_disposable_test_store()
    state = MemoryServer(
        store=store, anchor_root=str(tmp_path), project_id="scan"
    ).handler_state()
    client = CuratorClient()
    candidate = {
        "path": "README.md",
        "fact": "The package uses SQLite.",
        "quote": "uses SQLite",
    }
    monkeypatch.setattr(
        client,
        "_invoke",
        lambda *args, **kwargs: json.dumps({"result": json.dumps([candidate])}),
    )

    def fail_after_write(*args: Any, **kwargs: Any) -> dict[str, Any]:
        handle(*args, **kwargs)
        return {"isError": True, "content": [{"text": "fixture ingest refusal"}]}

    monkeypatch.setattr(project_scan, "ingest_handle", fail_after_write)
    with pytest.raises(CuratorError, match="fixture ingest refusal"):
        scanner()(state, tmp_path, "scan", client)
    assert store.capsules.list() == []


def test_scan_command_submits_staged_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import nmemory.cli

    (tmp_path / "README.md").write_text("The package uses SQLite.\n")
    store = Store.open_disposable_test_store()
    monkeypatch.setattr(
        "nmemory.commands.project.open_store_from_args", lambda _: store
    )
    monkeypatch.setenv("NOTT_NMEMORY_ANCHOR_ROOT", str(tmp_path))
    candidate = {
        "path": "README.md",
        "fact": "The package uses SQLite.",
        "quote": "uses SQLite",
    }
    monkeypatch.setattr(
        CuratorClient,
        "_invoke",
        lambda *args, **kwargs: json.dumps({"result": json.dumps([candidate])}),
    )
    assert (
        nmemory.cli.main(
            ["project", "scan", "--root", str(tmp_path), "--project", "scan"]
        )
        == 0
    )
    [capsule] = store.capsules.list()
    assert store.reviews.review_verdict(capsule.id) == "proposed"
    output = capsys.readouterr().out
    assert capsule.id in output
    assert "nmemory_proposal_review" in output
