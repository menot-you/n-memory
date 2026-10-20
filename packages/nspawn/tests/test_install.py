"""`nspawn skill install` and `nspawn plugin install`.

These tests run the real `npx skills` and `claude` against local fixture sources,
with a disposable HOME and TMPDIR. They never skip: a host without `npx` or `claude`
fails them, because a fake vendor proves nothing about the vendor. They fetch
skills@1.7.2 from the npm registry.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from nspawn import cli, install


@pytest.fixture(scope="module")
def npm_cache(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One npm cache that holds the npx skills release, copied into each HOME."""
    for name in ("npx", "claude"):
        if shutil.which(name) is None:
            pytest.fail(f"{name} is not installed on this host; these tests need it")
    home = tmp_path_factory.mktemp("npm-home")
    subprocess.run(
        ["npx", "-y", install.SKILLS, "--version"],
        env={
            "PATH": os.environ["PATH"],
            "HOME": str(home),
            "TMPDIR": os.environ.get("TMPDIR", "/var/tmp"),
        },
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=240,
        check=True,
    )
    return home / ".npm"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, npm_cache: Path) -> Path:
    path = tmp_path / "home"
    shutil.copytree(npm_cache, path / ".npm", symlinks=True)
    (tmp_path / "tmp").mkdir()
    monkeypatch.setenv("HOME", str(path))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    for name in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "XDG_STATE_HOME"):
        monkeypatch.delenv(name, raising=False)
    return path


Answer = tuple[int, Any, str]


def nspawn(capsys: pytest.CaptureFixture[str], *arguments: str) -> Answer:
    code = cli.main(list(arguments))
    captured = capsys.readouterr()
    return (
        code,
        json.loads(captured.out) if captured.out.strip() else None,
        captured.err,
    )


def skill_source(root: Path, name: str = "demo", front: str = "") -> Path:
    tree = root / "skills" / name
    tree.mkdir(parents=True)
    (tree / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: The {name} skill.\n{front}---\n# {name}\n"
    )
    (tree / "notes.txt").write_text("notes\n")
    return root


def plugin_tree(root: Path, name: str, version: str = "0.1.0") -> None:
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin/plugin.json").write_text(
        json.dumps({"name": name, "version": version, "description": "A plugin."})
    )
    (root / "commands").mkdir()
    (root / "commands/hi.md").write_text("---\ndescription: Say hi.\n---\nSay hi.\n")
    (root / "agents").mkdir()
    (root / "agents/helper.md").write_text(
        "---\nname: helper\ndescription: Helps.\n---\nHelp.\n"
    )
    (root / "skills/tip").mkdir(parents=True)
    (root / "skills/tip/SKILL.md").write_text(
        "---\nname: tip\ndescription: A tip.\n---\nTip.\n"
    )
    (root / "README.md").write_text("# Read me\n")


def catalog(root: Path, rows: list[dict[str, Any]]) -> None:
    (root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (root / ".claude-plugin/marketplace.json").write_text(
        json.dumps({"name": "probe", "owner": {"name": "t"}, "plugins": rows})
    )


def marketplace(root: Path, *names: str, **entries: dict[str, Any]) -> Path:
    """A local marketplace "probe" whose plugins live under ./plugins/<name>."""
    for name in names:
        plugin_tree(root / "plugins" / name, name)
    catalog(
        root,
        [
            {"name": name, "source": f"./plugins/{name}", "version": "0.1.0"}
            | entries.get(name, {})
            for name in names
        ],
    )
    return root


def node(home: Path) -> Path:
    return home / ".nott" / "node"


def record(home: Path) -> dict[str, Any]:
    path = node(home) / "claude/plugins/installed_plugins.json"
    return dict(json.loads(path.read_text())["plugins"]) if path.exists() else {}


def tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def everything(root: Path) -> list[str]:
    """Each path under `root`, except the npm cache that the fixture copied in."""
    return sorted(
        str(path.relative_to(root))
        for path in root.rglob("*")
        if path.relative_to(root).parts[0] != ".npm"
    )


# 1-3: skills -------------------------------------------------------------------------


def test_a_claude_skill_installs_only_into_the_node_target(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = skill_source(tmp_path / "source")
    code, check, err = nspawn(
        capsys, "skill", "install", "claude", str(source), "--check"
    )
    assert code == 0, err
    target = node(home) / "claude-skills/skills"
    assert check["provider"] == "claude" and check["kind"] == "skill"
    assert check["scope"] == "user" and check["target"] == str(target)
    assert check["reach"] == install.reach("claude")
    (item,) = check["items"]
    assert item["name"] == "demo" and item["version"] is None
    assert item["files"] == ["SKILL.md", "notes.txt"]
    assert [text["path"] for text in item["texts"]] == ["SKILL.md"]
    assert item["texts"][0]["text"].startswith("---\nname: demo")
    assert item["hash"] is not None
    assert everything(home) == [".nott", ".nott/node", ".nott/node/install.lock"]
    code, done, err = nspawn(
        capsys, "skill", "install", "claude", str(source), "--skill", "demo"
    )
    assert code == 0, err
    assert [row["name"] for row in done["installed"]] == ["demo"]
    assert done["installed"][0]["hash"] == item["hash"]
    assert everything(home) == [
        ".nott",
        ".nott/node",
        ".nott/node/claude-skills",
        ".nott/node/claude-skills/.claude-plugin",
        ".nott/node/claude-skills/.claude-plugin/plugin.json",
        ".nott/node/claude-skills/skills",
        ".nott/node/claude-skills/skills/demo",
        ".nott/node/claude-skills/skills/demo/SKILL.md",
        ".nott/node/claude-skills/skills/demo/notes.txt",
        ".nott/node/install.lock",
        ".nott/node/state",
        ".nott/node/state/skills",
        ".nott/node/state/skills/.skill-lock.json",
    ]
    assert json.loads(
        (node(home) / "claude-skills/.claude-plugin/plugin.json").read_text()
    ) == {"name": "installed"}
    assert (node(home) / "claude-skills").stat().st_mode & 0o777 == 0o700
    assert install.installed("claude", "skill") == [
        {"name": "demo", "version": None, "path": str(target / "demo")}
    ]
    assert install.claude_roots() == [node(home) / "claude-skills"]


def test_an_installed_claude_skill_is_never_replaced(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    first = skill_source(tmp_path / "first")
    code, _, err = nspawn(
        capsys, "skill", "install", "claude", str(first), "--skill", "demo"
    )
    assert code == 0, err
    target = node(home) / "claude-skills/skills/demo"
    before = tree_bytes(target)
    second = skill_source(tmp_path / "second")
    (second / "skills/demo/notes.txt").write_text("other notes\n")
    code, _, err = nspawn(
        capsys, "skill", "install", "claude", str(second), "--skill", "demo"
    )
    assert code == 2
    assert err.strip() == (
        f"skill demo already exists at {target}; NOTT does not replace an "
        "installed skill"
    )
    assert tree_bytes(target) == before


def test_a_codex_skill_installs_into_the_user_agents_skills_and_is_never_replaced(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = skill_source(tmp_path / "source")
    code, check, err = nspawn(
        capsys, "skill", "install", "codex", str(source), "--check"
    )
    assert code == 0, err
    assert check["target"] == str(home / ".agents/skills")
    assert check["reach"] == install.reach("codex")
    assert str(home) not in check["reach"]
    assert "uses your home" in check["reach"]
    assert not (home / ".agents").exists()
    code, _, err = nspawn(
        capsys, "skill", "install", "codex", str(source), "--skill", "demo"
    )
    assert code == 0, err
    assert tree_bytes(home / ".agents/skills/demo") == tree_bytes(
        source / "skills/demo"
    )
    assert (home / ".agents/.skill-lock.json").is_file()
    assert not (node(home) / "claude-skills").exists()
    assert install.installed("codex", "skill") == [
        {"name": "demo", "version": None, "path": str(home / ".agents/skills/demo")}
    ]
    code, _, err = nspawn(
        capsys, "skill", "install", "codex", str(source), "--skill", "demo"
    )
    assert code == 2
    assert err.strip().startswith(
        f"skill demo already exists at {home / '.agents/skills/demo'}"
    )


# 4: admission ------------------------------------------------------------------------


def write(root: Path, path: str, text: str) -> None:
    (root / path).parent.mkdir(parents=True, exist_ok=True)
    (root / path).write_text(text)


def manifest(**keys: Any) -> Callable[[Path], None]:
    def change(root: Path) -> None:
        path = root / ".claude-plugin/plugin.json"
        path.write_text(json.dumps(json.loads(path.read_text()) | keys))

    return change


def front(path: str) -> Callable[[Path], None]:
    def change(root: Path) -> None:
        text = (root / path).read_text()
        (root / path).write_text(text.replace("---\n", "---\nhooks:\n  Stop: []\n", 1))

    return change


def refusal(what: str) -> str:
    return (
        f"alpha@probe holds {what}; NOTT nodes admit only skills, commands and agents "
        "from an installed plugin"
    )


def front_refusal(path: str) -> str:
    return (
        f"alpha@probe/{path} declares hooks in its front matter; NOTT nodes run only "
        "the hooks of their own plugin"
    )


#: A change to the plugin tree, and the refusal that the check gives it.
TREE_REFUSALS: list[tuple[str, Callable[[Path], None], str]] = [
    ("hooks", lambda r: write(r, "hooks/hooks.json", '{"hooks": {}}'), "hooks"),
    ("mcp", lambda r: write(r, ".mcp.json", '{"mcpServers": {}}'), ".mcp.json"),
    ("lsp", lambda r: write(r, ".lsp.json", "{}"), ".lsp.json"),
    ("monitors", lambda r: write(r, "monitors/monitors.json", "[]"), "monitors"),
    ("bin", lambda r: write(r, "bin/tool", "#!/bin/sh\n"), "bin"),
    ("workflows", lambda r: write(r, "workflows/w.md", "w\n"), "workflows"),
    ("settings", lambda r: write(r, "settings.json", "{}"), "settings.json"),
    ("unknown", lambda r: write(r, "later.toml", "x = 1\n"), "later.toml"),
    (
        "manifest-hooks",
        manifest(hooks={"Stop": []}),
        "the key hooks in .claude-plugin/plugin.json",
    ),
    (
        "manifest-mcp",
        manifest(mcpServers={"x": {"command": "x"}}),
        "the key mcpServers in .claude-plugin/plugin.json",
    ),
    (
        "manifest-lsp-inline",
        manifest(
            lspServers={"x": {"command": "x", "extensionToLanguage": {".x": "x"}}}
        ),
        "the key lspServers in .claude-plugin/plugin.json",
    ),
    (
        "manifest-lsp-path",
        manifest(lspServers="./lsp.json"),
        "the key lspServers in .claude-plugin/plugin.json",
    ),
    (
        "manifest-monitors",
        manifest(monitors="./monitors.json"),
        "the key monitors in .claude-plugin/plugin.json",
    ),
    (
        "manifest-settings",
        manifest(settings={"x": 1}),
        "the key settings in .claude-plugin/plugin.json",
    ),
    (
        "manifest-output-styles",
        manifest(outputStyles="./styles"),
        "the key outputStyles in .claude-plugin/plugin.json",
    ),
    (
        "commands-outside",
        manifest(commands="./README.md"),
        'the commands path "./README.md" in .claude-plugin/plugin.json',
    ),
    (
        "commands-parent",
        manifest(commands=["./commands/../../x.md"]),
        'the commands path "./commands/../../x.md" in .claude-plugin/plugin.json',
    ),
    ("symlink", lambda r: (r / "commands/link.md").symlink_to("hi.md"), ""),
]

#: Front matter hooks, refused with their own message.
FRONT_REFUSALS = ["skills/tip/SKILL.md", "commands/hi.md", "agents/helper.md"]


def plugin_check(
    capsys: pytest.CaptureFixture[str], source: Path, plugin: str = "alpha"
) -> Answer:
    return nspawn(capsys, "plugin", "install", "claude", str(source), plugin, "--check")


@pytest.mark.parametrize(
    ("change", "what"),
    [(change, what) for _, change, what in TREE_REFUSALS],
    ids=[name for name, _, _ in TREE_REFUSALS],
)
def test_the_check_refuses_a_plugin_that_holds_more_than_skills_commands_and_agents(
    home: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    change: Callable[[Path], None],
    what: str,
) -> None:
    source = marketplace(tmp_path / "market", "alpha")
    change(source / "plugins/alpha")
    code, out, err = plugin_check(capsys, source)
    assert (code, out) == (2, None), err
    expected = (
        refusal(what)
        if what
        else f"{source} holds the symbolic link plugins/alpha/commands/link.md; NOTT "
        "nodes admit only skills, commands and agents from an installed plugin"
    )
    assert err.strip() == expected
    assert not (node(home) / "claude").exists()


@pytest.mark.parametrize("path", FRONT_REFUSALS)
def test_the_check_refuses_hooks_in_any_front_matter(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], path: str
) -> None:
    source = marketplace(tmp_path / "market", "alpha")
    front(path)(source / "plugins/alpha")
    code, _, err = plugin_check(capsys, source)
    assert code == 2
    assert err.strip() == front_refusal(path)


def test_the_check_refuses_hooks_in_an_inline_marketplace_entry(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = marketplace(
        tmp_path / "market",
        "alpha",
        alpha={"strict": False, "hooks": {"Stop": []}},
    )
    code, _, err = plugin_check(capsys, source)
    assert code == 2
    assert err.strip() == refusal("the key hooks in its marketplace entry")


def test_the_check_refuses_front_matter_whose_keys_are_indented(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """YAML reads an indented mapping as the top level, so `hooks` can hide there."""
    source = tmp_path / "source"
    (source / "skills/demo").mkdir(parents=True)
    (source / "skills/demo/SKILL.md").write_text(
        "---\n  name: demo\n  description: The demo skill.\n  hooks:\n    Stop: []\n"
        "---\n# demo\n"
    )
    code, _, err = nspawn(capsys, "skill", "install", "claude", str(source), "--check")
    assert code == 2
    assert err.strip() == (
        f"{source} holds front matter that NOTT cannot read in "
        "skills/demo/SKILL.md; NOTT nodes admit only skills, commands and agents "
        "from an installed plugin"
    )


@pytest.mark.parametrize(
    "head", ["  # Metadata\n", "   \n", "\n"], ids=["comment", "spaces", "blank"]
)
def test_an_indented_comment_or_blank_line_is_not_an_indented_mapping(
    tmp_path: Path, head: str
) -> None:
    path = tmp_path / "SKILL.md"
    path.write_text(f"---\n{head}name: demo\ndescription: d\n---\n# demo\n")
    assert install.frontmatter_keys(path) == ["name", "description"]


def test_the_check_refuses_hooks_in_a_skill_front_matter(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = skill_source(tmp_path / "source", front="hooks:\n  Stop: []\n")
    for provider in ("claude", "codex"):
        code, _, err = nspawn(
            capsys, "skill", "install", provider, str(source), "--check"
        )
        assert code == 2
        assert err.strip() == (
            f"{source}/skills/demo/SKILL.md declares hooks in its front matter; NOTT "
            "nodes run only the hooks of their own plugin"
        )


def test_a_clean_plugin_installs_and_its_install_path_is_a_claude_root(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = marketplace(tmp_path / "market", "alpha")
    code, check, err = plugin_check(capsys, source)
    assert code == 0, err
    assert check["kind"] == "plugin" and check["target"] == str(node(home) / "claude")
    (item,) = check["items"]
    assert (item["name"], item["version"], item["hash"]) == (
        "alpha@probe",
        "0.1.0",
        None,
    )
    assert item["files"] == [
        ".claude-plugin/plugin.json",
        "README.md",
        "agents/helper.md",
        "commands/hi.md",
        "skills/tip/SKILL.md",
    ]
    assert [text["path"] for text in item["texts"]] == [
        ".claude-plugin/plugin.json",
        "agents/helper.md",
        "commands/hi.md",
        "skills/tip/SKILL.md",
    ]
    assert not (node(home) / "claude").exists()
    code, done, err = nspawn(
        capsys, "plugin", "install", "claude", str(source), "alpha"
    )
    assert code == 0, err
    assert done["installed"]["pluginId"] == "alpha@probe"
    (entry,) = record(home)["alpha@probe"]
    path = Path(entry["installPath"])
    assert path.is_relative_to(node(home) / "claude/plugins/cache")
    assert install.claude_roots() == [path]
    assert install.installed("claude", "plugin") == [
        {"name": "alpha@probe", "version": "0.1.0", "path": str(path)}
    ]


@pytest.mark.parametrize(
    ("change", "what"),
    [
        (lambda r: write(r, "hooks/hooks.json", '{"hooks": {}}'), "hooks"),
        (lambda r: write(r, "monitors/monitors.json", "[]"), "monitors"),
    ],
    ids=["hooks", "monitors"],
)
def test_an_install_that_admission_refuses_is_disabled(
    home: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    change: Callable[[Path], None],
    what: str,
) -> None:
    """No check ran, as when the source changed after it: the vendor writes the
    plugin, the admission after it refuses, and the plugin is disabled."""
    source = marketplace(tmp_path / "market", "alpha")
    change(source / "plugins/alpha")
    code, _, err = nspawn(capsys, "plugin", "install", "claude", str(source), "alpha")
    settings = node(home) / "claude/settings.json"
    assert code == 2
    assert err.strip() == (
        f"alpha@probe as installed holds {what}; NOTT disabled it in {settings}"
    )
    assert json.loads(settings.read_text())["enabledPlugins"] == {"alpha@probe": False}
    assert install.claude_roots() == []
    assert install.installed("claude", "plugin") == []


@pytest.mark.parametrize("kind", ["plugin", "skill"])
@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_admission_runs_after_a_vendor_call_that_failed(
    kind: str,
    cleanup_fails: bool,
    home: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The vendor writes and then fails: what it wrote is still admitted."""
    real = install.Vendor.__call__

    def failing(self: install.Vendor, command: list[str], env: Any) -> Any:
        real(self, command, env)
        raise install.InstallFailed(f"{command[0]} exited 0 but printed no JSON")

    monkeypatch.setattr(install.Vendor, "__call__", failing)
    if cleanup_fails:
        real_remove = shutil.rmtree

        def removal(path: Any, *args: Any, **kwargs: Any) -> None:
            if Path(path).name in {"alpha", "demo"}:
                raise OSError("fixture cleanup failure")
            real_remove(path, *args, **kwargs)

        monkeypatch.setattr(shutil, "rmtree", removal)
    if kind == "plugin":
        source = marketplace(tmp_path / "market", "alpha")
        write(source / "plugins/alpha", "hooks/hooks.json", '{"hooks": {}}')
        arguments = ["alpha"]
    else:
        source = skill_source(tmp_path / "skills", front="hooks:\n  Stop: []\n")
        arguments = ["--skill", "demo"]
    code, _, err = nspawn(capsys, kind, "install", "claude", str(source), *arguments)
    assert code == 1
    assert "exited 0 but printed no JSON" in err
    if cleanup_fails:
        assert "fixture cleanup failure" in err
        assert "NOTT removed" not in err
        assert err.count("NOTT cleanup failed:") == 1
        return
    if kind == "plugin":
        assert "alpha@probe as installed holds hooks; NOTT disabled" in err
        assert install.claude_roots() == []
    else:
        assert "hooks in the front matter" in err
        assert "NOTT removed" in err
        assert not (node(home) / "claude-skills/skills/demo").exists()


def test_a_skill_install_that_admission_refuses_is_removed(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = skill_source(tmp_path / "source", front="hooks:\n  Stop: []\n")
    code, _, err = nspawn(
        capsys, "skill", "install", "claude", str(source), "--skill", "demo"
    )
    target = node(home) / "claude-skills/skills/demo"
    assert code == 2
    assert err.strip() == (
        "demo as installed holds hooks in the front matter of skills/demo/SKILL.md; "
        f"NOTT removed {target}"
    )
    assert not target.exists()
    assert install.claude_roots() == []


# 5: no prune -------------------------------------------------------------------------


def test_no_installed_plugin_version_is_pruned(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A moves to 0.2.0 in its marketplace, with autoUpdate on, and the next install
    (B) does not replace or delete the version that A's nodes load."""
    source = tmp_path / "market"
    plugin_tree(source / "plugins/alpha", "alpha")
    plugin_tree(source / "plugins/beta", "beta")
    rows = [
        {"name": name, "source": f"./plugins/{name}", "version": "0.1.0"}
        for name in ("alpha", "beta")
    ]
    catalog(source, rows[:1])
    code, _, err = nspawn(capsys, "plugin", "install", "claude", str(source), "alpha")
    assert code == 0, err
    config = node(home) / "claude"
    for path, key in (
        (config / "settings.json", "extraKnownMarketplaces"),
        (config / "plugins/known_marketplaces.json", None),
    ):
        document = json.loads(path.read_text())
        (document[key] if key else document)["probe"]["autoUpdate"] = True
        path.write_text(json.dumps(document))
    (first,) = record(home)["alpha@probe"]
    before = tree_bytes(Path(first["installPath"]))
    shutil.rmtree(source / "plugins/alpha")
    plugin_tree(source / "plugins/alpha", "alpha", "0.2.0")
    (source / "plugins/alpha/commands/hi.md").write_text("Say hi again.\n")
    catalog(source, [rows[0] | {"version": "0.2.0"}, rows[1]])
    code, _, err = nspawn(capsys, "plugin", "install", "claude", str(source), "beta")
    assert code == 0, err
    assert tree_bytes(Path(first["installPath"])) == before
    assert record(home)["alpha@probe"][0]["installPath"] == first["installPath"]
    assert not (Path(first["installPath"]) / ".orphaned_at").exists()


# 6: credentials ----------------------------------------------------------------------


@pytest.fixture
def stubs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A `gh` and a `git` first on PATH that record their environment and fail."""
    calls = tmp_path / "calls"
    calls.mkdir()
    bin_ = tmp_path / "stubs"
    bin_.mkdir()
    for name in ("gh", "git"):
        (bin_ / name).write_text(f'#!/bin/sh\nenv -0 > "{calls}/{name}.$$"\nexit 1\n')
        (bin_ / name).chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_}:{os.environ['PATH']}")
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "ANTHROPIC_API_KEY", "NPM_TOKEN"):
        monkeypatch.setenv(name, "planted-secret")
    return calls


def test_no_vendor_call_sees_a_token_or_the_owners_credentials(
    home: Path, stubs: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for arguments in (
        ["skill", "install", "claude", "owner/repo", "--check"],
        ["skill", "install", "codex", "owner/repo", "--skill", "demo"],
        ["skill", "install", "claude", "https://example.invalid/r.git", "--check"],
        ["plugin", "install", "claude", "owner/repo", "alpha", "--check"],
        ["plugin", "install", "claude", "owner/repo", "alpha"],
    ):
        code, _, err = nspawn(capsys, *arguments)
        assert code == 1, (arguments, err)
    recorded = sorted(stubs.iterdir())
    # npx skills calls gh only when gh answers; git is called on every clone.
    assert "git" in {path.name.split(".")[0] for path in recorded}
    for path in recorded:
        found = dict(
            line.split("=", 1) for line in path.read_text().split("\0") if "=" in line
        )
        assert "planted-secret" not in found.values(), path.name
        assert found["GIT_CONFIG_GLOBAL"] == "/dev/null"
        assert found["GIT_CONFIG_NOSYSTEM"] == "1"
        assert found["GIT_TERMINAL_PROMPT"] == "0"
        assert (found["GIT_CONFIG_KEY_0"], found["GIT_CONFIG_VALUE_0"]) == (
            "core.symlinks",
            "false",
        )
        assert not Path(found["GH_CONFIG_DIR"]).is_relative_to(home)
        assert "FORCE_AUTOUPDATE_PLUGINS" not in found


# 7-8: readers and staging ------------------------------------------------------------


def test_the_readers_follow_a_home_changed_after_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("one", "two"):
        home = tmp_path / name
        skills = home / ".nott/node/claude-skills/skills" / f"skill-{name}"
        skills.mkdir(parents=True)
        (home / ".agents/skills" / f"codex-{name}").mkdir(parents=True)
        config = home / ".nott/node/claude"
        cache = config / "plugins/cache/probe" / name / "0.1.0"
        cache.mkdir(parents=True)
        (config / "plugins/installed_plugins.json").write_text(
            json.dumps(
                {
                    "version": 2,
                    "plugins": {
                        f"{name}@probe": [
                            {
                                "scope": "user",
                                "installPath": str(cache),
                                "version": "0.1.0",
                            }
                        ]
                    },
                }
            )
        )
        (config / "settings.json").write_text(
            json.dumps({"enabledPlugins": {f"{name}@probe": True}})
        )
    for name in ("one", "two"):
        home = tmp_path / name
        monkeypatch.setenv("HOME", str(home))
        assert [row["name"] for row in install.installed("claude", "skill")] == [
            f"skill-{name}"
        ]
        assert [row["name"] for row in install.installed("codex", "skill")] == [
            f"codex-{name}"
        ]
        assert [row["name"] for row in install.installed("claude", "plugin")] == [
            f"{name}@probe"
        ]
        assert install.claude_roots() == [
            home / ".nott/node/claude-skills",
            home / f".nott/node/claude/plugins/cache/probe/{name}/0.1.0",
        ]


def staging(tmp: Path) -> list[str]:
    """The staging directories under TMPDIR; node keeps its compile cache there."""
    return [name for name in os.listdir(tmp) if name.startswith("nspawn-install.")]


def test_the_staging_directory_is_gone_after_a_check_and_after_a_failed_check(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    good = skill_source(tmp_path / "good")
    bad = skill_source(tmp_path / "bad", front="hooks:\n  Stop: []\n")
    for source, expected in ((good, 0), (bad, 2), (tmp_path / "missing", 1)):
        code, _, err = nspawn(
            capsys, "skill", "install", "claude", str(source), "--check"
        )
        assert code == expected, err
        assert staging(tmp_path / "tmp") == []
    code, _, err = plugin_check(capsys, marketplace(tmp_path / "market", "alpha"))
    assert code == 0, err
    assert staging(tmp_path / "tmp") == []


# 9: refusals -------------------------------------------------------------------------


REFUSALS: list[tuple[list[str], str]] = [
    (
        ["skill", "install", "qwen", "/x", "--check"],
        (
            "skill install takes provider claude or codex; qwen nodes load what is "
            "installed for claude"
        ),
    ),
    (
        ["plugin", "install", "qwen", "/x", "a", "--check"],
        (
            "plugin install takes provider claude; qwen nodes load what is installed "
            "for claude"
        ),
    ),
    (
        ["skill", "install", "agy", "/x", "--skill", "a"],
        (
            "agy reads global skills from ~/.gemini/config/skills, and npx skills "
            "writes no AGY skill there; NOTT installs no AGY skill"
        ),
    ),
    (
        ["plugin", "install", "agy", "/x", "a"],
        "agy plugin install takes only a directory; NOTT installs no AGY plugin",
    ),
    (
        ["plugin", "install", "codex", "/x", "a", "--check"],
        (
            "a codex plugin install or marketplace upgrade deletes the version that "
            "open Codex sessions use; NOTT installs no Codex plugin"
        ),
    ),
    (
        ["skill", "install", "claude", "-x", "--check"],
        'a source must not start with "-" or hold a control character',
    ),
    (
        ["plugin", "install", "claude", "/x\nb", "a"],
        'a source must not start with "-" or hold a control character',
    ),
    *[
        (
            ["skill", "install", "claude", source, "--check"],
            (
                "a source must be an https URL, an owner/repo name or a local path; "
                "NOTT sends no ssh key"
            ),
        )
        for source in (
            "git@github.com:owner/repo.git",
            "ssh://git@github.com/owner/repo",
            "http://example.com/repo",
            "git://example.com/repo",
        )
    ],
    (
        ["plugin", "install", "claude", "/x", "a/../b"],
        'a skill or plugin name holds only letters, digits, ".", "_" and "-"',
    ),
    (
        ["skill", "install", "claude", "/x", "--skill", "-rf"],
        'a skill or plugin name holds only letters, digits, ".", "_" and "-"',
    ),
    (
        ["skill", "install", "claude", "/x"],
        "install takes the skill names its check showed",
    ),
]


@pytest.fixture
def recording(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An `npx` and a `claude` first on PATH that record each call."""
    calls = tmp_path / "calls"
    bin_ = tmp_path / "recording"
    bin_.mkdir()
    for name in ("npx", "claude"):
        (bin_ / name).write_text(f'#!/bin/sh\necho {name} "$@" >> {calls}\n')
        (bin_ / name).chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_}:{os.environ['PATH']}")
    return calls


@pytest.mark.parametrize(("arguments", "message"), REFUSALS)
def test_each_refusal_exits_2_with_its_message_and_calls_no_vendor(
    home: Path,
    recording: Path,
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
    message: str,
) -> None:
    code, out, err = nspawn(capsys, *arguments)
    assert (code, out) == (2, None)
    assert err.strip() == message
    assert not recording.exists()
    assert everything(home) == []


def test_a_second_install_while_one_runs_is_refused(
    home: Path, recording: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lock = install.private(node(home)) / "install.lock"
    with lock.open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        code, _, err = nspawn(capsys, "skill", "install", "claude", "/x", "--check")
    assert code == 2
    assert err.strip() == "an install is running on this host; wait for its result"
    assert not recording.exists()


def test_a_vendor_that_exits_0_without_its_answer_fails(
    home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bin_ = tmp_path / "silent"
    bin_.mkdir()
    (bin_ / "npx").write_text("#!/bin/sh\nexit 0\n")
    (bin_ / "npx").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_}:{os.environ['PATH']}")
    source = skill_source(tmp_path / "source")
    for arguments in (["--check"], ["--skill", "demo"]):
        code, _, err = nspawn(
            capsys, "skill", "install", "claude", str(source), *arguments
        )
        assert code == 1
        assert err.strip() == (
            "nspawn skill install failed: npx -y skills@1.7.2 add exited 0 but printed "
            "no JSON"
        )
    assert not (node(home) / "claude-skills/skills/demo").exists()


def test_a_missing_vendor_cli_is_refused_by_name(
    home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    source = tmp_path / "source"
    source.mkdir()
    for arguments, name in (
        (["skill", "install", "claude", str(source), "--check"], "npx"),
        (["plugin", "install", "claude", str(source), "a", "--check"], "claude"),
    ):
        code, _, err = nspawn(capsys, *arguments)
        assert code == 2
        assert err.strip() == f"{name} is not installed on this host"


def test_vendor_environment_blocks_npm_lifecycle_scripts(
    home: Path, tmp_path: Path
) -> None:
    source = tmp_path / "package"
    source.mkdir()
    marker = tmp_path / "preinstall-ran"
    (source / "package.json").write_text(
        json.dumps(
            {
                "name": "fixture-script",
                "version": "1.0.0",
                "scripts": {"preinstall": f"touch {marker}"},
            }
        )
    )
    archive = tmp_path / "fixture.tgz"
    with tarfile.open(archive, "w:gz") as packed:
        packed.add(source / "package.json", arcname="package/package.json")
    result = subprocess.run(
        [
            "npm",
            "install",
            str(archive),
            "--prefix",
            str(tmp_path / "target"),
            "--no-audit",
            "--no-fund",
        ],
        env=install.environment(home, tmp_path / "gh"),
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()
    assert not marker.exists()


@pytest.mark.parametrize(
    "fence", ["---\ufeff", "--- \ufeff\t\ufeff", "\ufeff\ufeff---"]
)
def test_admission_refuses_a_unicode_frontmatter_fence(
    tmp_path: Path, fence: str
) -> None:
    root = skill_source(tmp_path / "source", front="hooks:\n  Stop: []\n")
    document = root / "skills/demo/SKILL.md"
    document.write_text(document.read_text().replace("---\n", fence + "\n", 1))
    with pytest.raises(install.NotAdmitted):
        install.admit(root)


@pytest.mark.parametrize("failed", [False, True])
def test_invalid_marketplace_after_a_vendor_write_disables_the_plugin(
    home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failed: bool,
) -> None:
    source = marketplace(tmp_path / "market", "alpha")
    real = install.Vendor.__call__

    def changed(self: install.Vendor, command: list[str], env: Any) -> Any:
        result = real(self, command, env)
        known = json.loads(
            (
                Path(env["CLAUDE_CONFIG_DIR"]) / "plugins/known_marketplaces.json"
            ).read_text()
        )
        catalog = (
            Path(known["probe"]["installLocation"]) / ".claude-plugin/marketplace.json"
        )
        catalog.write_bytes(b"\xff")
        if failed:
            raise install.InstallFailed("fixture vendor failure")
        return result

    monkeypatch.setattr(install.Vendor, "__call__", changed)
    code, _, err = nspawn(capsys, "plugin", "install", "claude", str(source), "alpha")
    assert code == (1 if failed else 2)
    assert "not valid JSON" in err
    assert "NOTT disabled" in err
    assert not json.loads((node(home) / "claude/settings.json").read_text())[
        "enabledPlugins"
    ]["alpha@probe"]
    assert install.claude_roots() == []


def test_failed_plugin_validation_preserves_an_installed_plugin(
    home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    old = marketplace(tmp_path / "old", "beta", "alpha")
    code, _, err = nspawn(capsys, "plugin", "install", "claude", str(old), "beta")
    assert code == 0, err
    roots = install.claude_roots()
    before = tree_bytes(roots[0])
    real = install.Vendor.__call__

    def changed(self: install.Vendor, command: list[str], env: Any) -> Any:
        result = real(self, command, env)
        record = Path(env["CLAUDE_CONFIG_DIR"]) / "plugins/installed_plugins.json"
        record.write_bytes(b"\xff")
        return result

    monkeypatch.setattr(install.Vendor, "__call__", changed)
    code, _, err = nspawn(capsys, "plugin", "install", "claude", str(old), "alpha")
    assert code == 2
    assert "not valid JSON" in err
    assert install.claude_roots() == roots
    assert tree_bytes(roots[0]) == before


def test_a_cleanup_failure_keeps_the_vendor_failure(
    home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = marketplace(tmp_path / "market", "alpha")
    real_call = install.Vendor.__call__
    real_remove = shutil.rmtree

    def failed(self: install.Vendor, command: list[str], env: Any) -> Any:
        real_call(self, command, env)
        raise install.InstallFailed("fixture vendor failure")

    def removal(path: Any, *args: Any, **kwargs: Any) -> None:
        if Path(path).name == "alpha":
            raise OSError("fixture cleanup failure")
        real_remove(path, *args, **kwargs)

    monkeypatch.setattr(install.Vendor, "__call__", failed)
    monkeypatch.setattr(shutil, "rmtree", removal)
    code, _, err = nspawn(capsys, "plugin", "install", "claude", str(source), "alpha")
    assert code == 1
    assert "fixture vendor failure" in err
    assert "fixture cleanup failure" in err
    assert err.count("NOTT cleanup failed:") == 1
    assert install.claude_roots() == []


def test_a_stop_during_process_creation_kills_the_created_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    ready, release, marker = (tmp_path / name for name in ("ready", "release", "late"))
    processes: list[subprocess.Popen[str]] = []
    real = subprocess.Popen

    def stopped(*args: Any, **kwargs: Any) -> subprocess.Popen[str]:
        process = real(*args, **kwargs)
        processes.append(process)
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()
        os.kill(os.getpid(), signal.SIGTERM)
        return process

    monkeypatch.setattr(subprocess, "Popen", stopped)
    child = (
        "import pathlib,time; "
        f"pathlib.Path({str(ready)!r}).touch(); "
        f"release=pathlib.Path({str(release)!r}); "
        "deadline=time.monotonic()+5\n"
        "while not release.exists() and time.monotonic()<deadline: time.sleep(.01)\n"
        f"pathlib.Path({str(marker)!r}).touch(); print('{{}}')"
    )
    with (
        install.vendor(time.monotonic() + 10) as call,
        pytest.raises(install.InstallFailed, match="host stopped"),
    ):
        call(
            [sys.executable, "-c", child],
            install.environment(tmp_path / "home", call.work / "gh"),
        )
    release.touch()
    processes[0].wait(timeout=5)
    assert not marker.exists()


def test_cleanup_reports_a_real_path_access_error(tmp_path: Path) -> None:
    parent = tmp_path / "locked"
    target = parent / "skill"
    target.mkdir(parents=True)
    parent.chmod(0)
    try:
        with pytest.raises(install.InstallFailed, match="Permission denied"):
            install.remove_written([target])
    finally:
        parent.chmod(0o700)


def test_a_failed_spawn_keeps_the_pending_stop_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    def failed(*args: Any, **kwargs: Any) -> subprocess.Popen[str]:
        os.kill(os.getpid(), signal.SIGTERM)
        raise OSError("fixture spawn failure")

    monkeypatch.setattr(subprocess, "Popen", failed)
    with (
        install.vendor(time.monotonic() + 10) as call,
        pytest.raises(
            install.InstallFailed, match="host stopped.*fixture spawn failure"
        ),
    ):
        call(
            [sys.executable, "-c", "pass"],
            install.environment(tmp_path / "home", call.work / "gh"),
        )


def test_plugin_rollback_attempts_each_record_and_cache_after_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config"
    settings = config / "settings.json"
    record = config / "plugins/installed_plugins.json"
    known = config / "plugins/known_marketplaces.json"
    root = config / "plugins/cache/probe/alpha"
    root.mkdir(parents=True)
    settings.write_text(json.dumps({"enabledPlugins": {"alpha@probe": True}}))
    record.write_bytes(b"\xff")
    known.write_bytes(b"\xff")
    prior = {settings: {}, record: {"plugins": {}}, known: {}}
    real = Path.replace

    def failed(path: Path, target: Any) -> Path:
        if Path(target) == settings:
            raise OSError("fixture settings failure")
        return real(path, target)

    monkeypatch.setattr(Path, "replace", failed)
    with pytest.raises(install.InstallFailed, match="fixture settings failure"):
        install.plugin_disable(config, "alpha", prior)
    assert json.loads(record.read_text()) == prior[record]
    assert json.loads(known.read_text()) == prior[known]
    assert not root.exists()


def test_skill_rollback_removes_other_skills_and_keeps_all_causes(
    home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = skill_source(tmp_path / "source")
    skill_source(source, "second")
    real_call, real_remove = install.Vendor.__call__, shutil.rmtree

    def failed(self: install.Vendor, command: list[str], env: Any) -> Any:
        real_call(self, command, env)
        raise install.InstallFailed("fixture vendor failure")

    def removal(path: Any, *args: Any, **kwargs: Any) -> None:
        if Path(path).name == "demo":
            raise OSError("fixture skill cleanup failure")
        real_remove(path, *args, **kwargs)

    monkeypatch.setattr(install.Vendor, "__call__", failed)
    monkeypatch.setattr(shutil, "rmtree", removal)
    code, _, err = nspawn(
        capsys,
        "skill",
        "install",
        "claude",
        str(source),
        "--skill",
        "demo",
        "--skill",
        "second",
    )
    assert code == 1
    assert "fixture vendor failure" in err
    assert "fixture skill cleanup failure" in err
    assert not (install.skills_target("claude") / "second").exists()


@pytest.mark.parametrize("kind", ["skill", "plugin"])
def test_timeout_rolls_back_admitted_partial_content(
    home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    kind: str,
) -> None:
    source = (
        skill_source(tmp_path / "source")
        if kind == "skill"
        else marketplace(tmp_path / "source", "alpha")
    )
    real = install.Vendor.__call__
    monkeypatch.setattr(install, "DEADLINE", 5)

    def timed_out(self: install.Vendor, command: list[str], env: Any) -> Any:
        real(self, command, env)
        self.deadline = time.monotonic() + 0.05
        return real(self, ["sleep", "5"], env)

    monkeypatch.setattr(install.Vendor, "__call__", timed_out)
    arguments = ["--skill", "demo"] if kind == "skill" else ["alpha"]
    code, _, err = nspawn(capsys, kind, "install", "claude", str(source), *arguments)
    assert code == 1
    assert "did not finish" in err
    assert install.claude_roots() == []
    assert install.installed("claude", kind) == []


def test_source_traversal_uses_the_verbs_deadline(
    home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recording: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = skill_source(tmp_path / "source")
    real = install.links_check

    def slow(*args: Any) -> None:
        real(*args)
        time.sleep(0.05)

    monkeypatch.setattr(install, "links_check", slow)
    monkeypatch.setattr(install, "DEADLINE", 0.01)

    def called(*args: Any) -> Any:
        recording.touch()
        raise install.InstallFailed("fixture vendor called")

    monkeypatch.setattr(install.Vendor, "__call__", called)
    code, _, _ = nspawn(capsys, "skill", "install", "claude", str(source), "--check")
    assert code == 1
    assert not recording.exists()


@pytest.mark.parametrize(
    ("name", "text", "message"),
    [
        ("installed_plugins.json", "{not json", "is not valid JSON"),
        ("settings.json", "[]", "does not hold the object Claude writes"),
        (
            "installed_plugins.json",
            '{"version": 2, "plugins": {"a@probe": {"scope": "user"}}}',
            "does not hold the plugins Claude writes",
        ),
        (
            "installed_plugins.json",
            '{"version": 2, "plugins": {"a@probe": [{"scope": "user"}]}}',
            "does not hold the plugins Claude writes",
        ),
        (
            "installed_plugins.json",
            '{"version": 2, "plugins": {"a@probe": [{"installPath": "/x"}]}}',
            "does not hold the plugins Claude writes",
        ),
    ],
)
def test_a_bad_claude_plugin_record_is_refused_by_its_path(
    home: Path, name: str, text: str, message: str
) -> None:
    config = node(home) / "claude"
    write(config, "plugins/installed_plugins.json", '{"version": 2, "plugins": {}}')
    write(config, "settings.json", '{"enabledPlugins": {"a@probe": true}}')
    path = config / ("plugins/" + name if name.startswith("installed") else name)
    path.write_text(text)
    for read in (install.claude_roots, lambda: install.installed("claude", "plugin")):
        with pytest.raises(install.InstallRefused) as refused:
            read()
        assert str(refused.value) == f"the Claude plugin record at {path} {message}"
