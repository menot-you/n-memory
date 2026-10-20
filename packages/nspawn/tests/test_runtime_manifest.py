"""Passive public SDK package discovery binds actual files without inference."""

import hashlib
import json
from types import SimpleNamespace

import pytest

from nspawn import route
from nspawn.route import RouteRefused, runtime_read


def sdk_package(tmp_path):
    package = tmp_path / "agent" / "node_modules" / "@anthropic-ai" / "claude-agent-sdk"
    package.mkdir(parents=True)
    (package / "package.json").write_text(
        json.dumps(
            {
                "name": "@anthropic-ai/claude-agent-sdk",
                "version": "0.3.232",
                "exports": {".": {"default": "./sdk.mjs", "types": "./sdk.d.ts"}},
            }
        )
    )
    (package / "sdk.mjs").write_text(
        "export function query() { throw new Error('must remain passive'); }\nexport function resolveSettings() {}\n"
    )
    (package / "sdk.d.ts").write_text("export interface SessionStore {}\n")
    adapter = tmp_path / "agent" / "dist" / "index.js"
    adapter.parent.mkdir()
    adapter.write_text("// stock ACP entry\n")
    native = tmp_path / "native"
    native.write_text("#!/bin/sh\nexit 17\n")
    native.chmod(0o700)
    return package, adapter, native


def test_darwin_runtime_refuses_the_bundled_cli_without_the_product_executable(
    tmp_path, monkeypatch
):
    package, adapter, native = sdk_package(tmp_path)
    bundled = package.parent / "claude-agent-sdk-darwin-arm64" / "claude"
    bundled.parent.mkdir()
    native.rename(bundled)
    node = tmp_path / "node"
    node.write_bytes(b"fixture-node")
    monkeypatch.setattr(route.sys, "platform", "darwin")
    monkeypatch.setattr(route.os, "uname", lambda: SimpleNamespace(machine="arm64"))
    monkeypatch.setattr(
        route, "adapter_command", lambda *args: [str(node), str(adapter)]
    )
    with pytest.raises(RouteRefused, match="selected product executable"):
        runtime_read({})


def test_runtime_manifest_discovers_the_public_package_and_complete_files_without_process_creation(
    tmp_path, monkeypatch
):
    package, adapter, native = sdk_package(tmp_path)
    monkeypatch.setattr(
        "nspawn.route.adapter_command", lambda *args: ["/usr/bin/node", str(adapter)]
    )
    result = runtime_read({"CLAUDE_CODE_EXECUTABLE": str(native)})
    assert result["version"] == 1
    assert result["sdk_version"] == "0.3.232"
    assert result["runtime"]["sdk"]["path"] == str(package / "sdk.mjs")
    assert result["runtime"]["sdk_package"]["path"] == str(package / "package.json")
    assert {item["path"] for item in result["runtime"]["files"]} >= {
        str(path) for path in package.iterdir()
    }
    assert (
        result["runtime"]["native"]["digest"]
        == "sha256:" + hashlib.sha256(native.read_bytes()).hexdigest()
    )


def test_a_stock_executable_without_the_public_package_has_no_owned_consumer_capability(
    tmp_path, monkeypatch
):
    executable = tmp_path / "stock"
    executable.write_text("#!/bin/sh\n")
    monkeypatch.setattr("nspawn.route.adapter_command", lambda *args: [str(executable)])
    assert runtime_read({}) is None


def test_public_package_export_cannot_redirect_to_a_proprietary_bridge(
    tmp_path, monkeypatch
):
    package, adapter, native = sdk_package(tmp_path)
    raw = json.loads((package / "package.json").read_text())
    raw["exports"]["."]["default"] = "./bridge.mjs"
    (package / "package.json").write_text(json.dumps(raw))
    monkeypatch.setattr(
        "nspawn.route.adapter_command", lambda *args: ["/usr/bin/node", str(adapter)]
    )
    with pytest.raises(RouteRefused, match="public SDK"):
        runtime_read({"CLAUDE_CODE_EXECUTABLE": str(native)})
