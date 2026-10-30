"""A projected writer plugin keeps the repository binding of its hooks.

The writer plugin is a projection in the run directory. A hook that computes its
repository from its own path (`../..`) must still reach the source repository,
not the run directory.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from nspawn import child

SOURCE_HOOKS = Path(__file__).resolve().parents[3] / "plugin" / "hooks"
CONTRACT = (
    "#!/usr/bin/env bash\n"
    'touch "$(dirname "$0")/../../ran"\n'
    "echo 'system-prompt-contract: GREEN'\n"
)


def _git(*args: str) -> None:
    subprocess.run(["git", *args], check=True, capture_output=True)


def _repository(root: Path, *, with_hooks: bool) -> Path:
    """A git repository with a law contract that leaves `ran` when it runs."""
    _git("init", "-q", "-b", "main", str(root))
    tests = root / "plugin" / "tests"
    tests.mkdir(parents=True)
    contract = tests / "system-prompt-contract.sh"
    contract.write_text(CONTRACT)
    contract.chmod(0o700)
    (root / "plugin" / "NOTT.md").write_text("# law\n")
    if with_hooks:
        hooks = root / "plugin" / "hooks"
        hooks.mkdir()
        for name in ("hooks.json", "law-contract.sh", "pynott-branch-lock.sh"):
            shutil.copy2(SOURCE_HOOKS / name, hooks / name)
    return root


@pytest.fixture
def projection(tmp_path: Path) -> tuple[Path, Path]:
    source = _repository(tmp_path / "source", with_hooks=True)
    destination = tmp_path / "run" / "writer-plugin-0"
    destination.parent.mkdir()
    child.writer_plugin(source / "plugin", destination)
    return source, destination


def _run_hook(
    projected: Path, script: str, payload: dict[str, object], home: Path
) -> subprocess.CompletedProcess[str]:
    """Run the registered command of `script` as the native client does."""
    document = json.loads((projected / "hooks" / "hooks.json").read_text())
    commands = [
        hook["command"]
        for groups in document["hooks"].values()
        for group in groups
        for hook in group["hooks"]
        if hook["command"].strip('"').endswith("/" + script)
    ]
    assert len(commands) == 1, f"{script} must be registered once: {commands}"
    environment = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "NOTT_STATE_DIR": str(home),
        "CLAUDE_PLUGIN_ROOT": str(projected),
    }
    return subprocess.run(
        ["bash", "-c", commands[0]],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=environment,
        timeout=60,
        check=False,
    )


def test_law_contract_runs_for_the_source_repository(projection, tmp_path) -> None:
    source, projected = projection
    result = _run_hook(
        projected,
        "law-contract.sh",
        {"tool_input": {"file_path": str(source / "plugin" / "NOTT.md")}},
        tmp_path,
    )
    assert result.returncode == 0
    assert (source / "ran").exists(), "the source repository's law contract must run"


def test_law_contract_skips_an_unrelated_clone(projection, tmp_path) -> None:
    _, projected = projection
    other = _repository(tmp_path / "other", with_hooks=False)
    result = _run_hook(
        projected,
        "law-contract.sh",
        {"tool_input": {"file_path": str(other / "plugin" / "NOTT.md")}},
        tmp_path,
    )
    assert result.returncode == 0
    assert not (other / "ran").exists(), (
        "a projected hook ran the contract script of an unrelated clone"
    )


def _bash(cwd: Path, command: str) -> dict[str, object]:
    return {"tool_name": "Bash", "cwd": str(cwd), "tool_input": {"command": command}}


def test_branch_lock_refuses_a_switch_of_the_primary_checkout(
    projection, tmp_path
) -> None:
    source, projected = projection
    clone = _repository(tmp_path / "clone", with_hooks=True)
    result = _run_hook(
        projected,
        "pynott-branch-lock.sh",
        _bash(clone, f"git -C {source} switch lane"),
        tmp_path,
    )
    assert result.returncode == 2, result.stderr
    assert "moves the protected primary checkout off main" in result.stderr


def test_branch_lock_lets_a_writer_switch_in_its_own_clone(
    projection, tmp_path
) -> None:
    _, projected = projection
    clone = _repository(tmp_path / "clone", with_hooks=True)
    result = _run_hook(
        projected, "pynott-branch-lock.sh", _bash(clone, "git switch -c lane"), tmp_path
    )
    assert result.returncode == 0, result.stderr


def test_the_projection_keeps_every_other_hook_entry(projection) -> None:
    source, projected = projection
    names = {entry.name for entry in (source / "plugin" / "hooks").iterdir()}
    assert names == {entry.name for entry in (projected / "hooks").iterdir()}
    assert (projected / "hooks" / "law-contract.sh").resolve() == (
        source / "plugin" / "hooks" / "law-contract.sh"
    )


def test_a_source_path_with_shell_characters_still_runs_its_hook(tmp_path) -> None:
    source = tmp_path / 'we ird$HOME"q`b'
    hooks = source / "plugin" / "hooks"
    hooks.mkdir(parents=True)
    probe = hooks / "probe.sh"
    probe.write_text(
        '#!/usr/bin/env bash\ncd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P\n'
    )
    probe.chmod(0o700)
    (hooks / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": '"${CLAUDE_PLUGIN_ROOT}/hooks/probe.sh"',
                                }
                            ]
                        }
                    ]
                }
            }
        )
    )
    projected = tmp_path / "run" / "writer-plugin-0"
    projected.parent.mkdir()
    child.writer_plugin(source / "plugin", projected)
    result = _run_hook(projected, "probe.sh", {}, tmp_path)
    assert result.stdout.strip() == str(source), result.stderr
