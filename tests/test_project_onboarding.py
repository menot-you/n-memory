"""Project setup preserves client settings and uses the installed runtime."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from nmemory.cli import main


@pytest.fixture
def config_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace only the consumer probe for configuration transaction tests."""
    from nmemory.commands import project

    monkeypatch.setattr(
        project, "_probe", lambda *a, **k: {"mcp": True, "system1": False}
    )


def bootstrap(
    root: Path, capsys: pytest.CaptureFixture[str], *extra: str
) -> dict[str, Any]:
    code = main(
        ["project", "bootstrap", "--root", str(root), "--project", "sample", *extra]
    )
    captured = capsys.readouterr()
    assert code == 0, captured.err
    return json.loads(captured.out)


@pytest.mark.parametrize(
    "previous", [None, b"/keep.txt\n", b"# Keep this rule\n!/bootstrap.json"]
)
def test_bootstrap_private_state_stays_out_of_git(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    config_probe: None,
    previous: bytes | None,
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    environment = {
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "home/config"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }

    def git(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *arguments],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            check=check,
        )

    git("init")
    owned = root / ".nott/nmemory"
    owned.mkdir(parents=True)
    config = owned / "config.toml"
    config.write_text('[github]\nrepo = "example/project"\n')
    git("add", ".nott/nmemory/config.toml")
    root_ignore = root / ".gitignore"
    root_ignore.write_bytes(b"/keep-root.txt\n")
    ignore = owned / ".gitignore"
    if previous is not None:
        ignore.write_bytes(previous)
        ignore.chmod(0o640)
    (owned / "keep.txt").touch()
    (owned / "visible.txt").touch()
    paths = [
        f".nott/nmemory/{name}"
        for name in ("bootstrap.json", "bootstrap.lock", "runtime.py", "memory.toml")
    ]
    first = bootstrap(root, capsys)
    status = git("status", "--porcelain", "--untracked-files=all").stdout
    assert all(path not in status for path in paths), status
    assert git("check-ignore", "--", *paths).stdout.splitlines() == paths
    assert "A  .nott/nmemory/config.toml" in status
    assert "?? .nott/nmemory/visible.txt" in status
    assert git("check-ignore", ".nott/nmemory/config.toml", check=False).returncode == 1
    assert config.read_text() == '[github]\nrepo = "example/project"\n'
    assert root_ignore.read_bytes() == b"/keep-root.txt\n"
    if previous == b"/keep.txt\n":
        assert git("check-ignore", ".nott/nmemory/keep.txt").returncode == 0
    git("add", "--all", ".nott/nmemory")
    staged = git("diff", "--cached", "--name-only").stdout.splitlines()
    assert all(path not in staged for path in paths), staged
    assert ".nott/nmemory/config.toml" in staged
    installed = ignore.read_bytes()
    receipt = Path(first["receipt_path"])
    saved = receipt.read_bytes()
    assert bootstrap(root, capsys)["status"] == "unchanged"
    assert ignore.read_bytes() == installed
    assert receipt.read_bytes() == saved
    bootstrap(root, capsys, "--restore")
    if previous is None:
        assert not ignore.exists()
    else:
        assert ignore.read_bytes() == previous
        assert ignore.stat().st_mode & 0o777 == 0o640
    assert not receipt.exists()


def test_bootstrap_preserves_settings_and_restores(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], config_probe: None
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    settings = root / ".claude/settings.json"
    settings.parent.mkdir()
    before = {
        "permissions": {"allow": ["Read"]},
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo keep"}]}]},
    }
    settings.write_text(json.dumps(before))
    mcp = root / ".mcp.json"
    mcp.write_text('{"mcpServers":{"other":{"command":"other"}}}')
    original = {path: path.read_bytes() for path in (settings, mcp)}
    first = bootstrap(root, capsys)
    assert first["system1"] == {"mode": "lexical", "ready": False}
    assert first["probe"]["mcp"] is True
    assert first["hooks"]["enabled"] is True
    assert json.loads(mcp.read_text())["mcpServers"]["other"] == {"command": "other"}
    assert (
        json.loads(settings.read_text())["hooks"]["Stop"][0]
        == before["hooks"]["Stop"][0]
    )
    installed = {path: path.read_bytes() for path in (settings, mcp)}
    second = bootstrap(root, capsys)
    assert second["status"] == "unchanged"
    assert {path: path.read_bytes() for path in installed} == installed
    restored = bootstrap(root, capsys, "--restore")
    assert restored["status"] == "restored"
    assert {path: path.read_bytes() for path in original} == original


@pytest.mark.parametrize("existed", [False, True])
@pytest.mark.parametrize("change", ["existing", "addition", "field"])
@pytest.mark.parametrize("action", ["repeat", "restore"])
def test_bootstrap_preserves_later_foreign_mcp_changes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    config_probe: None,
    existed: bool,
    change: str,
    action: str,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    target = root / ".mcp.json"
    if existed:
        target.write_text('{"mcpServers":{"keep":{"command":"before"}}}')
    first = bootstrap(root, capsys)
    receipt = Path(first["receipt_path"])
    saved_receipt = receipt.read_bytes()
    database = Path(first["database"]["path"])
    saved_database = database.read_bytes()
    config = json.loads(target.read_text())
    if change == "field":
        config["foreign"] = {"keep": True}
    else:
        name = "keep" if change == "existing" else "later"
        config["mcpServers"][name] = {"command": "changed"}
    target.write_text(json.dumps(config))
    current = target.read_bytes()
    if action == "repeat":
        assert bootstrap(root, capsys)["status"] == "unchanged"
        assert target.read_bytes() == current
        assert receipt.read_bytes() == saved_receipt
    else:
        assert bootstrap(root, capsys, "--restore")["status"] == "restored"
        del config["mcpServers"]["nmemory"]
        if not existed and not config["mcpServers"]:
            del config["mcpServers"]
        assert json.loads(target.read_text()) == config
        assert not receipt.exists()
        assert not (root / ".nott/nmemory/runtime.py").exists()
        assert not (root / ".nott/nmemory/memory.toml").exists()
        assert not (root / ".claude/settings.json").exists()
    assert database.read_bytes() == saved_database


@pytest.mark.parametrize(
    "defect",
    ["command", "args", "remove", "launcher", "config", "receipt", "link", "mode"],
)
def test_restore_refuses_owned_changes_with_later_foreign_mcp_data(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    config_probe: None,
    defect: str,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    first = bootstrap(root, capsys)
    target = root / ".mcp.json"
    config = json.loads(target.read_text())
    config["mcpServers"]["keep"] = {"command": "later"}
    if defect == "remove":
        del config["mcpServers"]["nmemory"]
    elif defect in {"command", "args"}:
        config["mcpServers"]["nmemory"][defect] = "changed"
    target.write_text(json.dumps(config))
    receipt = Path(first["receipt_path"])
    if defect in {"launcher", "config"}:
        name = "runtime.py" if defect == "launcher" else "memory.toml"
        receipt.with_name(name).write_text("Changed owned file.\n")
    elif defect == "receipt":
        document = json.loads(receipt.read_text())
        next(item for item in document["files"] if item["path"] == str(target))[
            "after"
        ] = "invalid"
        receipt.write_text(json.dumps(document))
    elif defect == "link":
        saved = root / "foreign.json"
        target.rename(saved)
        target.symlink_to(saved)
    elif defect == "mode":
        target.chmod(0o666)
    paths = [Path(item["path"]) for item in json.loads(receipt.read_text())["files"]]
    before = {path: path.read_bytes() for path in [receipt, *paths]}
    assert main(["project", "bootstrap", "--root", str(root), "--restore"]) == 2
    assert {path: path.read_bytes() for path in before} == before
    capsys.readouterr()


@pytest.mark.parametrize(
    "target", [".claude/settings.json", ".nott/nmemory/.gitignore"]
)
@pytest.mark.parametrize("restore", [False, True])
def test_bootstrap_refuses_concurrent_changes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    config_probe: None,
    target: str,
    restore: bool,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    bootstrap(root, capsys)
    settings = root / target
    settings.write_text('{"changed":true}')
    receipt = root / ".nott/nmemory/bootstrap.json"
    saved = receipt.read_bytes()
    assert (
        main(
            [
                "project",
                "bootstrap",
                "--root",
                str(root),
                "--project",
                "sample",
                *(["--restore"] if restore else []),
            ]
        )
        == 2
    )
    assert settings.read_text() == '{"changed":true}'
    assert receipt.read_bytes() == saved
    assert "changed" in capsys.readouterr().err


def test_bootstrap_refuses_changed_options(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], config_probe: None
) -> None:
    bootstrap(tmp_path, capsys)
    receipt = tmp_path / ".nott/nmemory/bootstrap.json"
    saved = receipt.read_bytes()
    assert (
        main(
            [
                "project",
                "bootstrap",
                "--root",
                str(tmp_path),
                "--project",
                "other",
            ]
        )
        == 2
    )
    assert receipt.read_bytes() == saved
    assert "restore the previous setup" in capsys.readouterr().err


@pytest.mark.parametrize("previous", [None, b"/keep.txt\n"])
def test_bootstrap_failure_restores_prior_files(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    previous: bytes | None,
) -> None:
    from nmemory.commands import project

    root = tmp_path / "project"
    root.mkdir()
    original = b'{"mcpServers":{"other":{"command":"other"}}}'
    (root / ".mcp.json").write_bytes(original)
    ignore = root / ".nott/nmemory/.gitignore"
    if previous is not None:
        ignore.parent.mkdir(parents=True)
        ignore.write_bytes(previous)

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise project.ConfigError("consumer probe failed")

    monkeypatch.setattr(project, "_probe", fail)
    assert (
        main(["project", "bootstrap", "--root", str(root), "--project", "sample"]) == 2
    )
    assert (root / ".mcp.json").read_bytes() == original
    assert not (root / ".claude/settings.json").exists()
    assert not (root / ".nott/nmemory/bootstrap.json").exists()
    if previous is None:
        assert not ignore.exists()
    else:
        assert ignore.read_bytes() == previous
    assert "consumer probe failed" in capsys.readouterr().err


def test_local_launcher_does_not_inherit_cloud_routes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from nmemory.commands import project

    monkeypatch.setattr(
        project, "_probe", lambda *a, **k: {"mcp": True, "system1": True}
    )
    root = tmp_path / "project"
    root.mkdir()
    report = bootstrap(
        root,
        capsys,
        "--system1",
        "local",
        "--endpoint",
        "http://127.0.0.1:8080/v1/chat/completions",
        "--model",
        "local-test",
    )
    script = Path(report["launcher"]).read_text()
    # Capture the exec environment without starting another model or printing a value.
    capture = tmp_path / "capture.py"
    capture.write_text(script.replace("os.execve(", "capture("))
    prefix = (
        "import os, json\ndef capture(*args):\n print(json.dumps(sorted(args[2])))\n"
    )
    capture.write_text(prefix + capture.read_text())
    child = subprocess.run(
        [sys.executable, str(capture), "mcp", "serve"],
        env={
            **os.environ,
            "OPENAI_API_KEY": "fixture",
            "NOTT_SYSTEM1_TAINT_API_KEY": "fixture",
            "NOTT_SYSTEM1_TAINT_ENDPOINT": "https://cloud.invalid",
            "NOTT_SYSTEM1_TAINT_MODEL": "cloud",
        },
        text=True,
        capture_output=True,
        check=True,
    )
    names = json.loads(child.stdout)
    assert "OPENAI_API_KEY" not in names
    assert "NOTT_SYSTEM1_TAINT_API_KEY" not in names
    assert "NOTT_SYSTEM1_TAINT_ENDPOINT" not in names


def test_bootstrap_stdio_consumer(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Run this proof where local IPC is permitted."""
    root = tmp_path / "consumer"
    root.mkdir()
    result = bootstrap(root, capsys)
    assert result["probe"]["mcp"] is True


def test_codex_setup_preserves_other_tables(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], config_probe: None
) -> None:
    import tomllib

    root = tmp_path / "codex-project"
    target = root / ".codex/config.toml"
    target.parent.mkdir(parents=True)
    original = 'model = "existing"\n[mcp_servers.other]\ncommand = "other"\n'
    target.write_text(original)
    result = bootstrap(root, capsys, "--client", "codex")
    assert result["hooks"] == {"enabled": False, "events": []}
    document = tomllib.loads(target.read_text())
    assert document["model"] == "existing"
    assert document["mcp_servers"]["other"] == {"command": "other"}
    assert document["mcp_servers"]["nmemory"]["args"][-1] == "sample"
    appended = '\n# Team search\n[mcp_servers.later]\ncommand = "later"\n'
    target.write_text(target.read_text() + appended)
    repeated = bootstrap(root, capsys, "--client", "codex")
    assert repeated["status"] == "unchanged"
    bootstrap(root, capsys, "--client", "codex", "--restore")
    assert target.read_text() == original + appended
    bootstrap(root, capsys, "--client", "codex")
    target.write_text(
        target.read_text().replace(
            "\n[mcp_servers.nmemory]\n", "[mcp_servers.nmemory]\n", 1
        )
    )
    bootstrap(root, capsys, "--client", "codex", "--restore")
    assert target.read_text() == original + appended


def test_partial_write_failure_restores_configuration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from nmemory.commands import project

    root = tmp_path / "partial"
    root.mkdir()
    mcp = root / ".mcp.json"
    original = b'{"mcpServers":{"other":{"command":"other"}}}'
    mcp.write_bytes(original)
    write = project._write

    def fail_settings(path: Path, data: bytes, mode: int = 0o600) -> None:
        if path == root / ".claude/settings.json":
            raise OSError("fixture write failure")
        write(path, data, mode)

    monkeypatch.setattr(project, "_write", fail_settings)
    assert (
        main(["project", "bootstrap", "--root", str(root), "--project", "sample"]) == 1
    )
    assert mcp.read_bytes() == original
    assert not (root / ".nott/nmemory/runtime.py").exists()
    assert not (root / ".nott/nmemory/bootstrap.json").exists()
    assert not (root / ".nott/nmemory/.gitignore").exists()
    assert "fixture write failure" in capsys.readouterr().err


def test_runtime_uses_the_installed_module(tmp_path: Path) -> None:
    from nmemory.commands.project import _runtime

    root = tmp_path / "runtime-check"
    root.mkdir()
    (root / "nmemory.py").write_text(
        'raise AssertionError("project code was imported")'
    )
    launcher = root / "runtime.py"
    launcher.write_bytes(_runtime("sample", root / "memory.toml", "lexical", root))
    result = subprocess.run(
        [sys.executable, "-I", str(launcher), "--version"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.startswith("nmemory ")


def test_restore_refuses_an_unowned_target(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], config_probe: None
) -> None:
    import hashlib

    root = tmp_path / "receipt-scope"
    root.mkdir()
    report = bootstrap(root, capsys)
    untouched = root / "untouched.txt"
    untouched.write_text("Keep this file.")
    receipt = Path(report["receipt_path"])
    document = json.loads(receipt.read_text())
    record = document["files"][0]
    record["path"] = str(untouched)
    record["after"] = hashlib.sha256(untouched.read_bytes()).hexdigest()
    receipt.write_text(json.dumps(document))
    assert main(["project", "bootstrap", "--root", str(root), "--restore"]) == 2
    assert untouched.read_text() == "Keep this file."
    assert "outside the project" in capsys.readouterr().err


def test_existing_memory_configuration_stays_intact(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], config_probe: None
) -> None:
    root = tmp_path / "existing-config"
    config = root / ".nott/nmemory/memory.toml"
    config.parent.mkdir(parents=True)
    original = (
        '[github]\nrepo = "example/project"\nproject = "sample"\nefforts = ["cap-1"]\n'
    )
    config.write_text(original)
    bootstrap(root, capsys)
    assert config.read_text() == original
    bootstrap(root, capsys, "--restore")
    assert config.read_text() == original
