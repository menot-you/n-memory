"""Check the standalone source export without an installed source fallback."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import subprocess
import sys
import tomllib
from pathlib import Path
from types import ModuleType

import pytest

MEMBERS = {
    "mcps/nmemory": "nmemory",
    "libs/database": "nott_database",
    "libs/netdoor": "nott_netdoor",
    "libs/system1": "nott_system1",
    "evals/nmemory": "nmemory_evals",
    "mcps/nspawn": "nspawn",
    "mcps/nbrowser": "nbrowser",
    "mcps/napple": "napple",
    "libs/socket": "nott_socket",
    "libs/spine": "nott_spine",
}
HOOKS = (
    "hooks.json",
    "nmemory-session.sh",
    "nmemory-recall.py",
    "nmemory-capture-gate.py",
    "renamed.py",
    "renamed.sh",
    "system1-suggest.py",
    "system1.py",
)


def exporter() -> ModuleType:
    name = "nmemory.commands.release"
    assert importlib.util.find_spec(name) is not None, (
        "The standalone release exporter is absent."
    )
    return importlib.import_module(name)


def put(root: Path, name: str, text: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_public_formatting_keeps_tool_caches_outside_the_source(tmp_path: Path) -> None:
    policy = importlib.import_module("nmemory.commands.projection")
    put(tmp_path, "pyproject.toml", '[tool.ruff]\ntarget-version = "py314"\n')
    put(tmp_path, "example.py", "value=1\n")
    policy.format_source(tmp_path)
    assert (tmp_path / "example.py").read_text() == "value = 1\n"
    assert not list(tmp_path.rglob(".ruff_cache"))


@pytest.fixture
def source(tmp_path: Path) -> Path:
    root = tmp_path / "arbitrary-producer"
    put(root, ".python-version", "3.14.7\n")
    put(root, "LICENSE", "Synthetic test license.\n")
    put(root, "pyproject.toml", "[dependency-groups]\ndev = []\n")
    for member, module in MEMBERS.items():
        dependencies = '["nspawn"]' if module == "nmemory" else "[]"
        metadata = (
            f'[project]\nname = "{module.replace("_", "-")}"\n'
            f'version = "0.1.0"\nrequires-python = ">=3.14"\n'
            f'dependencies = {dependencies}\nreadme = "README.md"\n'
        )
        if module == "nmemory":
            metadata += "\n[tool.uv.sources]\nnspawn = { workspace = true }\n"
        put(root, f"{member}/pyproject.toml", metadata)
        put(root, f"{member}/README.md", "# Synthetic package\n")
        put(root, f"{member}/{module}/__init__.py", 'ORIGIN = "explicit source"\n')
        put(
            root,
            f"{member}/tests/test_example.py",
            "def test_example():\n    assert True\n",
        )
    put(
        root,
        "mcps/nmemory/nmemory/commands/projection.py",
        Path(exporter().__file__).with_name("projection.py").read_text(),
    )
    put(
        root,
        "mcps/nmemory/nmemory/commands/layout.py",
        Path(exporter().__file__).with_name("layout.py").read_text(),
    )
    put(
        root,
        "mcps/nmemory/nmemory/project.py",
        "PROJECT_ALIASES: dict[str, str] = {}\n",
    )
    for hook in HOOKS:
        put(root, f"plugin/hooks/{hook}", "# Synthetic hook\n")
    put(root, "libs/netdoor/http-door-auth.tsv", "method\tstatus\n")
    for name in (
        "conftest.py",
        "workspace_root.py",
        "test_determinism.py",
        "test_metrics.py",
        "test_driver.py",
        "test_abstention.py",
    ):
        put(root, f"evals/nmemory/tests/{name}", "# Portable test\n")
    put(root, "plugin/evals/native/run.sh", "# Private runner\n")
    put(root, "evals/nmemory/nmemory_evals/world.py", "# Synthetic producer\n")
    put(root, "evals/nmemory/nmemory_evals/capture/stream-01.jsonl", "PRIVATE\n")
    put(root, "evals/nmemory/nmemory_evals/capture_gold_set.json", "PRIVATE\n")
    put(root, "evals/nmemory/tests/fixtures/private.jsonl", "PRIVATE\n")
    put(root, "evals/nmemory/tests/test_capture_gap.py", "PRIVATE\n")
    put(root, "docs/design/public/changelog.md", "# Public changes\n")
    put(root, "CHANGELOG.md", "PRIVATE\n")
    put(root, "docs/design/public/architecture.md", "# Architecture\n")
    put(
        root,
        "mcps/nmemory/README.md",
        "[Architecture](../../docs/design/public/architecture.md#memory)\n",
    )
    put(root, "mcps/nmemory/RULEBOOK.md", "# Rules\n")
    put(
        root,
        "docs/design/public/export-map.json",
        json.dumps(
            {
                "schema_version": 1,
                "targets": {
                    "menot-you/n-memory": {
                        "LICENSE": "LICENSE",
                        "mcps/nmemory/README.md": "mcps/nmemory/README.md",
                        "mcps/nmemory/RULEBOOK.md": "mcps/nmemory/RULEBOOK.md",
                        "docs/design/public/architecture.md": "docs/design/public/architecture.md",
                        "docs/design/public/changelog.md": "docs/design/public/changelog.md",
                    }
                },
                "root_readmes": {"menot-you/n-memory": "mcps/nmemory/README.md"},
                "root_changelog": "docs/design/public/changelog.md",
            }
        ),
    )
    contract = json.loads(
        '{\n  "schema": 1,\n  "components": {\n    "nmemory": {\n      "module": "nmemory",\n      "primary": "mcps/nmemory",\n      "packages": {\n        "libs/database": "packages/database",\n        "libs/netdoor": "packages/netdoor",\n        "libs/system1": "packages/system1",\n        "evals/nmemory": "evals",\n        "mcps/nspawn": "packages/nspawn",\n        "libs/socket": "packages/socket"\n      }\n    },\n    "nspawn": {\n      "module": "nspawn",\n      "primary": "mcps/nspawn",\n      "packages": {\n        "libs/database": "packages/database",\n        "libs/socket": "packages/socket",\n        "libs/system1": "packages/system1"\n      }\n    },\n    "nbrowser": {\n      "module": "nbrowser",\n      "primary": "mcps/nbrowser",\n      "packages": {\n        "libs/database": "packages/database",\n        "libs/netdoor": "packages/netdoor",\n        "libs/spine": "packages/spine"\n      }\n    },\n    "plugin": {\n      "primary": "plugin",\n      "packages": {\n        "mcps/nmemory": "packages/nmemory",\n        "mcps/nspawn": "packages/nspawn",\n        "mcps/nbrowser": "packages/nbrowser",\n        "libs/database": "packages/database",\n        "libs/netdoor": "packages/netdoor",\n        "libs/system1": "packages/system1",\n        "libs/socket": "packages/socket",\n        "libs/spine": "packages/spine",\n        "evals/nmemory": "evals"\n      }\n    }\n  },\n  "documents": {\n    "root": [\n      "README.md",\n      "LICENSE",\n      "CHANGELOG.md",\n      "CONTRIBUTING.md",\n      "CODE_OF_CONDUCT.md",\n      "SECURITY.md",\n      "SUPPORT.md",\n      "CITATION.cff"\n    ],\n    "docs": [\n      "docs/README.md",\n      "docs/RUNBOOK.md",\n      "docs/ARCHITECTURE.md",\n      "docs/REFERENCE.md",\n      "docs/INSTALLATION.md",\n      "docs/PRIVACY.md",\n      "docs/UPGRADE.md",\n      "docs/TROUBLESHOOTING.md",\n      "docs/ROADMAP.md"\n    ]\n  },\n  "repositories": {\n    "nmemory": "menot-you/n-memory",\n    "nspawn": "menot-you/n-spawn",\n    "nbrowser": "menot-you/n-browser",\n    "plugin": "menot-you/plugin"\n  }\n}\n'
    )
    contract["components"]["napple"] = {
        "module": "napple",
        "primary": "mcps/napple",
        "packages": {},
    }
    contract["repositories"]["napple"] = "menot-you/n-apple"
    put(root, "ops/mirror/projects.json", json.dumps(contract))
    for component, entry in contract["components"].items():
        if component == "plugin":
            continue
        target = contract["repositories"][component]
        document = json.loads((root / "docs/design/public/export-map.json").read_text())
        mapping = {
            name: name
            for name in contract["documents"]["root"]
            if name not in {"README.md", "CHANGELOG.md"}
        }
        for name in contract["documents"]["docs"]:
            if name == "docs/ARCHITECTURE.md":
                continue
            origin = f"mcps/{component}/docs/{Path(name).name}"
            put(root, origin, "# Public guide\n")
            mapping[origin] = name
        mapping[f"{entry['primary']}/README.md"] = "README.md"
        mapping["docs/design/public/architecture.md"] = "docs/ARCHITECTURE.md"
        mapping["docs/design/public/changelog.md"] = "CHANGELOG.md"
        mapping["docs/design/public/export-map.json"] = (
            "docs/design/public/export-map.json"
        )
        for name in mapping:
            if not (root / name).exists():
                put(root, name, "# Public document\n")
        document["targets"][target] = mapping
        document["root_readmes"][target] = f"{entry['primary']}/README.md"
        put(root, "docs/design/public/export-map.json", json.dumps(document))
    for name in ("npm/package.json", "npm/bin/nbrowser.js", "python/README.md"):
        put(
            root,
            f"mcps/nbrowser/{name}",
            "{}\n" if name.endswith("json") else "# Resource\n",
        )
    return root


def digest(root: Path) -> dict[str, tuple[str, int]]:
    return {
        str(path.relative_to(root)): (
            hashlib.sha256(path.read_bytes()).hexdigest(),
            path.stat().st_mode & 0o777,
        )
        for path in root.rglob("*")
        if path.is_file()
    }


def test_explicit_source_deterministic_workspace(source: Path, tmp_path: Path) -> None:
    release = exporter()
    one, two = tmp_path / "one", tmp_path / "two"
    first = release.export(source, one)
    second = release.export(source, two)
    assert first == second
    assert digest(one) == digest(two)
    assert (one / "nmemory/__init__.py").read_text() == 'ORIGIN = "explicit source"\n'
    assert (one / "LICENSE").read_bytes() == (source / "LICENSE").read_bytes()
    assert (one / ".python-version").read_text() == "3.14.7\n"
    assert "../../" not in (one / "README.md").read_text()
    assert "docs/ARCHITECTURE.md#memory" in (one / "README.md").read_text()
    assert (one / "CHANGELOG.md").read_text() == "# Public changes\n"
    workspace = tomllib.loads((one / "pyproject.toml").read_text())
    assert set(workspace["tool"]["uv"]["workspace"]["members"]) == {
        "packages/database",
        "packages/netdoor",
        "packages/system1",
        "evals",
        "packages/nspawn",
        "packages/socket",
    }
    metadata = tomllib.loads((one / "pyproject.toml").read_text())
    assert "nspawn" in metadata["project"]["dependencies"]
    assert metadata["project"]["license-files"] == ["LICENSE"]
    assert (one / "LICENSE").read_bytes() == (source / "LICENSE").read_bytes()
    assert metadata["tool"]["uv"]["sources"]["nspawn"] == {"workspace": True}
    assert (one / "evals/nmemory_evals/world.py").is_file()
    assert (one / "evals/tests/test_determinism.py").is_file()
    assert not list(one.rglob("stream-*.jsonl"))
    assert not (one / "evals/nmemory_evals/capture_gold_set.json").exists()
    assert not (one / "evals/tests/test_capture_gap.py").exists()
    assert not (one / "plugin/evals").exists()
    for hook in HOOKS:
        assert (one / "nmemory/hooks" / hook).read_bytes() == (
            source / "plugin/hooks" / hook
        ).read_bytes()
    assert not list(one.rglob(".git"))
    result = subprocess.run(
        ["uv", "lock", "--check", "--offline"],
        cwd=one,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    manifest = json.loads((one / "source-manifest.json").read_text())
    assert (
        manifest["files"]["LICENSE"]["sha256"]
        == hashlib.sha256((one / "LICENSE").read_bytes()).hexdigest()
    )
    assert str(source) not in (one / "source-manifest.json").read_text()


@pytest.mark.parametrize(
    "missing",
    [
        "LICENSE",
        ".python-version",
        "libs/database/pyproject.toml",
        "plugin/hooks/system1.py",
        "docs/design/public/architecture.md",
    ],
)
def test_incomplete_source_refused(source: Path, tmp_path: Path, missing: str) -> None:
    release = exporter()
    (source / missing).unlink()
    output = tmp_path / "output"
    with pytest.raises(release.ExportError, match="missing"):
        release.export(source, output)
    assert not output.exists()


def test_nonempty_destination_untouched(source: Path, tmp_path: Path) -> None:
    release = exporter()
    output = tmp_path / "existing"
    put(output, "keep.txt", "Keep this file.\n")
    before = digest(output)
    with pytest.raises(release.ExportError, match="exists"):
        release.export(source, output)
    assert digest(output) == before


@pytest.mark.parametrize(
    "linked",
    [
        "mcps/nmemory/nmemory/outside.py",
        "mcps/nmemory/tests/outside.py",
        "docs/design/public/architecture.md",
    ],
)
def test_source_symlink_escape_refused(
    source: Path, tmp_path: Path, linked: str
) -> None:
    release = exporter()
    secret = tmp_path / "outside.txt"
    secret.write_text("Do not export this file.\n")
    path = source / linked
    path.unlink(missing_ok=True)
    path.symlink_to(secret)
    output = tmp_path / "output"
    with pytest.raises(release.ExportError, match="link|escape"):
        release.export(source, output)
    assert not output.exists()


def test_missing_local_link_refused(source: Path, tmp_path: Path) -> None:
    release = exporter()
    put(source, "mcps/nmemory/README.md", "[Missing](../../private.md)\n")
    with pytest.raises(release.ExportError, match="local link"):
        release.export(source, tmp_path / "output")


def test_output_inside_package_refused(source: Path) -> None:
    release = exporter()
    with pytest.raises(release.ExportError, match="output"):
        release.export(source, source / "mcps/nmemory/output")


def test_lock_failure_retains_preparation(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = exporter()

    run = subprocess.run

    def failed(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[:2] == ["uv", "lock"]:
            return subprocess.CompletedProcess(args, 1, "", "test failure")
        return run(args, **kwargs)

    monkeypatch.setattr(release.subprocess, "run", failed)
    with pytest.raises(release.ExportError, match="lock"):
        release.export(source, tmp_path / "output")
    assert (tmp_path / "output/lock-diagnostic.txt").read_text() == "test failure"
    assert (tmp_path / "output/pyproject.toml").is_file()
    assert not (tmp_path / "output/source-manifest.json").exists()


@pytest.mark.parametrize(
    "link",
    [
        "[Link][design]\n\n[design]: ../../missing.md\n",
        '<img src="../../missing.svg">\n',
        "[Image](../../missing.png)\n",
    ],
)
def test_missing_reference_and_image_refused(
    source: Path, tmp_path: Path, link: str
) -> None:
    release = exporter()
    put(source, "mcps/nmemory/README.md", link)
    with pytest.raises(release.ExportError, match="local link"):
        release.export(source, tmp_path / "output")


def test_document_mapping_and_reference_links(source: Path, tmp_path: Path) -> None:
    release = exporter()
    path = source / "docs/design/public/export-map.json"
    document = json.loads(path.read_text())
    document["targets"]["menot-you/n-memory"]["docs/design/public/architecture.md"] = (
        "docs/ARCHITECTURE.md"
    )
    path.write_text(json.dumps(document))
    put(
        source,
        "mcps/nmemory/README.md",
        '[Architecture][design]\n\n[design]: ../../docs/design/public/architecture.md#memory\n<img src="/docs/design/public/architecture.md">\n',
    )
    output = tmp_path / "output"
    release.export(source, output)
    assert "docs/ARCHITECTURE.md#memory" in (output / "README.md").read_text()
    assert '<img src="docs/ARCHITECTURE.md">' in (output / "README.md").read_text()
    assert not (output / "docs/design/public/architecture.md").exists()


def test_corrupt_map_cannot_escape_destination(source: Path, tmp_path: Path) -> None:
    release = exporter()
    path = source / "docs/design/public/export-map.json"
    document = json.loads(path.read_text())
    document["targets"]["menot-you/n-memory"]["LICENSE"] = "../outside-license"
    path.write_text(json.dumps(document))
    with pytest.raises(release.ExportError, match="unsafe"):
        release.export(source, tmp_path / "output")
    assert not (tmp_path / "outside-license").exists()


def test_hook_closure_incomplete_refused(source: Path, tmp_path: Path) -> None:
    release = exporter()
    (source / "plugin/hooks/system1-suggest.py").unlink()
    with pytest.raises(release.ExportError, match="missing"):
        release.export(source, tmp_path / "output")


@pytest.mark.parametrize("metadata", ["pyproject.toml", "mcps/nmemory/pyproject.toml"])
def test_invalid_metadata_refused(source: Path, tmp_path: Path, metadata: str) -> None:
    release = exporter()
    put(source, metadata, "[invalid\n")
    with pytest.raises(release.ExportError, match="metadata"):
        release.export(source, tmp_path / "output")
    assert not (tmp_path / "output").exists()


def test_nspawn_version_dependency_never_resolves_from_pypi(
    source: Path, tmp_path: Path
) -> None:
    release = exporter()
    path = source / "mcps/nmemory/pyproject.toml"
    path.write_text(path.read_text().replace("nspawn = { workspace = true }", ""))
    with pytest.raises(release.ExportError, match="nspawn"):
        release.export(source, tmp_path / "output")
    assert not (tmp_path / "output").exists()


def test_memory_projection_removes_private_commentary(
    source: Path, tmp_path: Path
) -> None:
    put(
        source,
        "mcps/nmemory/nmemory/__init__.py",
        '"""Public API.\n\nPrivate source ('
        + "cap-"
        + '12345).\n"""\n'
        + "# Private source "
        + "cap-"
        + "12345\nVALUE = 42\n",
    )
    output = tmp_path / "output"
    exporter().export(source, output)
    text = (output / "nmemory/__init__.py").read_text()
    assert "cap-" + "12345" not in text
    assert "Public API." in text and "VALUE = 42" in text
    done = subprocess.run(
        [sys.executable, "-m", "ruff", "format", "--check", str(output)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert done.returncode == 0, done.stdout + done.stderr


def test_memory_projection_refuses_private_executable_values(
    source: Path, tmp_path: Path
) -> None:
    put(
        source,
        "mcps/nmemory/nmemory/__init__.py",
        'COMMAND = "' + "/" + 'guara/apps/private"\n',
    )
    output = tmp_path / "output"
    with pytest.raises(exporter().ExportError, match="private-host-path"):
        exporter().export(source, output)
    assert not output.exists()


def test_memory_uses_the_policy_from_the_selected_source(
    source: Path, tmp_path: Path
) -> None:
    path = source / "mcps/nmemory/nmemory/commands/projection.py"
    path.write_text('raise RuntimeError("Selected source policy.")\n')
    with pytest.raises(RuntimeError, match="Selected source policy"):
        exporter().export(source, tmp_path / "output")


def test_memory_export_can_project_its_own_document_map(
    source: Path, tmp_path: Path
) -> None:
    path = source / "docs/design/public/export-map.json"
    document = json.loads(path.read_text())
    document["targets"]["menot-you/n-memory"]["templates/public/ci.yml"] = (
        ".github/workflows/ci.yml"
    )
    document["targets"]["menot-you/n-memory"]["docs/design/public/export-map.json"] = (
        "docs/design/public/export-map.json"
    )
    path.write_text(json.dumps(document))
    put(source, "templates/public/ci.yml", "name: CI\n")
    first, second = tmp_path / "first", tmp_path / "second"
    exporter().export(source, first)
    exporter().export(first, second)
    assert (second / ".github/workflows/ci.yml").read_text() == "name: CI\n"


def test_memory_contribution_guide_uses_an_exported_check(
    source: Path, tmp_path: Path
) -> None:
    path = source / "docs/design/public/export-map.json"
    document = json.loads(path.read_text())
    document["targets"]["menot-you/n-memory"]["CONTRIBUTING.md"] = "CONTRIBUTING.md"
    path.write_text(json.dumps(document))
    put(
        source,
        "CONTRIBUTING.md",
        "Documentation changes need local link checks, figure inspection, and the documentation build.\n"
        "The source workspace builds its portal with `bash tools/document.sh build`.\n"
        "That private portal also requires its configured memory export and build assets.\n"
        "A public export must not include private portal content.\n",
    )
    output = tmp_path / "output"
    exporter().export(source, output)
    guide = (output / "CONTRIBUTING.md").read_text()
    assert "tools/document.sh" not in guide
    assert "pytest tests -q" in guide


def test_memory_checks_the_generated_lock_for_credentials(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = exporter()
    run = subprocess.run

    def lock_result(
        args: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if args[:2] == ["uv", "lock"]:
            (Path(args[-1]) / "uv.lock").write_text(
                'token = "' + "ghp_" + "a" * 36 + '"\n'
            )
            return subprocess.CompletedProcess(args, 0, "", "")
        return run(args, **kwargs)

    monkeypatch.setattr(release.subprocess, "run", lock_result)
    output = tmp_path / "output"
    with pytest.raises(release.ExportError, match="credential"):
        release.export(source, output)
    assert not output.exists()


def test_public_document_links_resolve(tmp_path: Path) -> None:
    from workspace_root import WORKSPACE_ROOT

    release = exporter()
    stage = tmp_path / "documents"
    stage.mkdir()
    release._members(WORKSPACE_ROOT, stage)
    release._docs(WORKSPACE_ROOT, stage)
    release._links(stage)


def test_memory_projection_removes_producer_aliases(
    source: Path, tmp_path: Path
) -> None:
    put(
        source,
        "mcps/nmemory/nmemory/project.py",
        'PROJECT_ALIASES: dict[str, str] = {"producer": "private-project"}\n',
    )
    output = tmp_path / "output"
    exporter().export(source, output)
    text = (output / "nmemory/project.py").read_text()
    assert text == "PROJECT_ALIASES: dict[str, str] = {}\n"


@pytest.mark.parametrize("component", ["nmemory", "nspawn", "nbrowser", "napple"])
def test_component_root_and_metadata(
    source: Path, tmp_path: Path, component: str
) -> None:
    output = tmp_path / "public"
    result = exporter().export(source, output, component=component)
    assert (output / component / "__init__.py").is_file()
    assert (output / "tests/test_example.py").is_file()
    assert not (output / "mcps").exists()
    assert not (output / "plugin").exists()
    assert result["component"] == component
    assert result["primary_module"] == component
    metadata = tomllib.loads((output / "pyproject.toml").read_text())
    assert metadata["project"]["license"] == "AGPL-3.0-only"
    assert metadata["project"]["license-files"] == ["LICENSE"]
    if component != "nmemory":
        assert "nspawn" not in metadata["project"]["dependencies"]
    if component == "nbrowser":
        assert (output / "npm/package.json").is_file()
        assert (output / "python/README.md").is_file()


def test_required_document_refused(source: Path, tmp_path: Path) -> None:
    document = source / "mcps/nmemory/docs/RUNBOOK.md"
    document.unlink()
    with pytest.raises(exporter().ExportError, match="missing"):
        exporter().export(source, tmp_path / "public")


def test_generated_protocol_ids_and_private_records(tmp_path: Path) -> None:
    from nmemory.commands import projection

    path = tmp_path / "protocol.py"
    path.write_text('example = "cap-1"\n')
    assert projection.scan(tmp_path) == []
    path.write_text('example = "cap-" + str(31245)\n')
    assert projection.scan(tmp_path) == []
    path.write_text('example = "' + "cap-" + '31245"\n')
    assert projection.scan(tmp_path) == [
        {"path": "protocol.py", "line": 1, "rule": "private-record"}
    ]


def test_public_fixtures_preserve_the_privacy_boundary(tmp_path: Path) -> None:
    import shutil

    from nmemory.commands import projection

    source = Path(__file__).parent
    shutil.copytree(
        source, tmp_path / "tests", ignore=shutil.ignore_patterns("__pycache__")
    )
    projection.commentary(tmp_path)
    findings = projection.scan(tmp_path)
    assert findings == [], json.dumps(findings, indent=2)


def test_portable_location_uses_the_emitted_layout(tmp_path: Path) -> None:
    import importlib.util

    helper = Path(__file__).with_name("workspace_root.py")
    target = tmp_path / "tests/workspace_root.py"
    target.parent.mkdir()
    target.write_text(helper.read_text())
    put(tmp_path, "uv.lock", "version = 1\n")
    put(tmp_path, "source-manifest.json", '{"component": "nmemory"}')
    put(
        tmp_path,
        "ops/mirror/projects.json",
        json.dumps(
            {
                "components": {
                    "nmemory": {
                        "primary": "mcps/nmemory",
                        "packages": {"libs/database": "packages/database"},
                    }
                }
            }
        ),
    )
    spec = importlib.util.spec_from_file_location("public_location", target)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.workspace_path("mcps/nmemory") == tmp_path
    assert (
        module.workspace_path("libs/database/tests")
        == tmp_path / "packages/database/tests"
    )


@pytest.mark.parametrize("component", ["nmemory", "nspawn"])
def test_export_projects_only_staged_wire_recordings(
    source: Path, tmp_path: Path, component: str
) -> None:
    import shutil

    from workspace_root import workspace_path

    from nmemory.commands import projection

    fixtures = source / "mcps/nspawn/tests/fixtures"
    fixtures.mkdir()
    originals = {}
    for name in projection.WIRE_FIXTURES:
        original = workspace_path("mcps/nspawn/tests/fixtures") / name
        if not original.is_file():
            original = original.with_name(name.replace(".ndjson", ".public.ndjson"))
            shutil.copy2(original.with_suffix(".projection.json"), fixtures)
        shutil.copy2(original, fixtures)
        originals[original.name] = original.read_bytes()
    output = tmp_path / "public-wire"
    exporter().export(source, output, component)
    public_fixtures = (
        output
        / ("packages/nspawn" if component == "nmemory" else ".")
        / "tests/fixtures"
    )
    assert all(
        (fixtures / name).read_bytes() == value for name, value in originals.items()
    )
    assert all(
        not (public_fixtures / name).exists() for name in projection.WIRE_FIXTURES
    )
    assert all(
        (public_fixtures / name.replace(".ndjson", ".public.ndjson")).is_file()
        for name in projection.WIRE_FIXTURES
    )
    assert projection.scan(public_fixtures) == []


def test_installed_projection_and_bundled_hooks_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import io

    import nmemory
    from nmemory import github_client
    from nmemory.commands import projection
    from nmemory.github_plan import CloseReason, DesiredState, IssuePatch

    stage = tmp_path / "stage"
    stage.mkdir()
    cid = "cap-" + "12345"
    priv = "/" + "guara/secret"
    py_file = stage / "sample.py"
    py_file.write_text(
        f'"""Module header.\n\nOwner decision {cid}. Public detail.\n"""\n'
        f"# Private {priv}\n"
        "def run():\n"
        f'    """Source {priv}."""\n'
        "    return 1\n"
    )
    toml_file = stage / "sample.toml"
    toml_file.write_text(
        f'# Private {priv}\n# Public comment\nkey = "value # not comment"\n'
    )
    projection.commentary(stage)
    assert "Public detail." in py_file.read_text()
    assert priv not in py_file.read_text()
    assert "# Public comment" in toml_file.read_text()
    assert priv not in toml_file.read_text()

    put(
        stage,
        "mcps/nmemory/nmemory/project.py",
        'PROJECT_ALIASES: dict[str, str] = {"x": "y"}\n',
    )
    put(
        stage,
        "mcps/nmemory/pyproject.toml",
        '[tool.vulture]\npaths = [\n"nmemory",\n"../../harness/nott_harness",\n]\n',
    )
    put(
        stage,
        "mcps/nmemory/nmemory/tools/validation.py",
        f'MSG = "see {cid}"\n',
    )
    put(
        stage,
        "mcps/nspawn/nspawn/owner_roster.py",
        f'MSG = "rule ({cid})"\n',
    )
    put(
        stage,
        "CONTRIBUTING.md",
        "Documentation changes need local link checks, figure inspection, and the documentation build.\n"
        "The source workspace builds its portal with `bash tools/document.sh build`.\n"
        "That private portal also requires its configured memory export and build assets.\n"
        "A public export must not include private portal content.\n",
    )
    projection.public_runtime(stage)
    projection.public_contributing(stage, "pytest tests -q")
    assert (
        "cap-<number>"
        in (stage / "mcps/nmemory/nmemory/tools/validation.py").read_text()
    )
    assert cid not in (stage / "mcps/nspawn/nspawn/owner_roster.py").read_text()
    assert "pytest tests -q" in (stage / "CONTRIBUTING.md").read_text()

    hooks_dir = Path(nmemory.__file__).resolve().parent / "hooks"
    if not hooks_dir.is_dir():
        from workspace_root import workspace_path

        hooks_dir = workspace_path("plugin/hooks")
    for filename, mod_name in (
        ("renamed.py", "cov_renamed"),
        ("system1-suggest.py", "cov_suggest"),
        ("nmemory-recall.py", "cov_recall"),
    ):
        hook_path = hooks_dir / filename
        assert hook_path.is_file()
        spec = importlib.util.spec_from_file_location(mod_name, hook_path)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if filename == "renamed.py":
            assert mod.advisory("UNSET_VAR_NAME") is None
            monkeypatch.setenv("NOTT_NMEMORY_PROJECT", "sample")
            assert mod.advisory("NOTT_NMEMORY_PROJECT") == "sample"
        elif filename == "system1-suggest.py":
            monkeypatch.setenv("HOME", str(tmp_path))
            monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
            monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
            monkeypatch.delenv("NOTT_NSPAWN_RUN_ID", raising=False)
            assert not mod.owner_prompt({"agent_id": "sub"})
            assert mod.owner_prompt({"prompt": "hello world"})
            assert not mod.carries_credential("plain text", "my-key")
            assert mod.carries_credential("my-key in text", "my-key")
            assert mod.catalog()[0] is not None
            assert mod.audit_path() is not None
            assert mod.record_outcome({"event": "suggest"}) is True
            assert mod.suggest(json.dumps({"agent_id": "sub", "prompt": "hi"}), {}) == (
                "skipped",
                "",
            )
            assert mod.suggest("{}", {}) == ("bad-payload", "")
            monkeypatch.setattr(
                mod,
                "catalog",
                lambda: (
                    {"nott:execute": "execute"},
                    {"general-purpose": "general-purpose"},
                ),
            )
            orig_load = mod._load

            class _FakeClient:
                @staticmethod
                def route() -> tuple[str, str, str]:
                    return ("http://127.0.0.1:18031", "m", "my-key")

                @staticmethod
                def ask(*a: object, **kw: object) -> dict[str, object]:
                    return {
                        "skill": {
                            "type": "choice",
                            "value": "nott:execute",
                            "confidence": 0.9,
                        },
                        "agent": {
                            "type": "choice",
                            "value": "general-purpose",
                            "confidence": 0.9,
                        },
                    }

            monkeypatch.setattr(
                mod,
                "_load",
                lambda name, fn, _orig=orig_load: (
                    _FakeClient if fn == "system1.py" else _orig(name, fn)
                ),
            )
            monkeypatch.setattr(
                sys,
                "stdin",
                io.StringIO(json.dumps({"prompt": "run test", "session_id": "s1"})),
            )
            assert mod.main() == 0
        elif filename == "nmemory-recall.py":
            assert mod.door("http://127.0.0.1:16699/mcp") == (
                "127.0.0.1",
                16699,
                "/mcp",
            )
            assert mod.counts("0100007F") is True
            assert mod.headlines(
                {
                    "results": [
                        {
                            "id": "cap-1",
                            "authority_class": "user-stated",
                            "headline": "rule one",
                            "instruction_taint": False,
                        },
                        {
                            "id": "cap-" + "2",
                            "authority_class": "user-stated",
                            "headline": "tainted",
                            "instruction_taint": True,
                        },
                    ]
                }
            ) == [
                "cap-1 [user-stated]: rule one",
                "cap-" + "2 [user-stated]: [tainted content withheld]",
            ]
            fake_bin = tmp_path / "nmemory-bin"
            fake_bin.write_text("#!/bin/sh\n")
            fake_bin.chmod(0o700)
            monkeypatch.setenv("NOTT_NMEMORY_BIN", str(fake_bin))
            monkeypatch.setattr(
                mod.subprocess,
                "run",
                lambda *a, **kw: subprocess.CompletedProcess(
                    [],
                    0,
                    json.dumps(
                        {
                            "evidence": [
                                {
                                    "id": "cap-1",
                                    "authority_class": "user-stated",
                                    "headline": "cli rule",
                                    "instruction_taint": False,
                                }
                            ]
                        }
                    ),
                    "",
                ),
            )
            assert mod.cli_recall(["term"], "sample") == [
                "cap-1 [user-stated]: cli rule"
            ]

            class _FakeResp:
                status = 200

                def __init__(self, body: bytes) -> None:
                    self._body = body
                    self.headers = {"mcp-session-id": "sid"}

                def read(self, _limit: int) -> bytes:
                    return self._body

            class _FakeConn:
                def __init__(self, *a: object, **kw: object) -> None:
                    self.calls = 0

                def request(self, *a: object, **kw: object) -> None:
                    self.calls += 1

                def getresponse(self) -> _FakeResp:
                    if self.calls == 1:
                        return _FakeResp(b'{"result":{"protocolVersion":"2025-06-18"}}')
                    return _FakeResp(
                        json.dumps(
                            {
                                "result": {
                                    "isError": False,
                                    "content": [
                                        {
                                            "text": json.dumps(
                                                {
                                                    "results": [
                                                        {
                                                            "id": "cap-1",
                                                            "authority_class": "user-stated",
                                                            "headline": "http rule",
                                                            "instruction_taint": False,
                                                        }
                                                    ]
                                                }
                                            )
                                        }
                                    ],
                                }
                            }
                        ).encode()
                    )

                def close(self) -> None:
                    pass

            monkeypatch.setattr(mod, "OwnedConnection", _FakeConn)
            assert mod.recall(
                "http://127.0.0.1:16699/mcp",
                "key",
                ["term"],
                "sess-1",
                project="sample",
            ) == ["cap-1 [user-stated]: http rule"]
            monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
            assert mod.main() == ""

    seen_cmds: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen_cmds.append(cmd)
        joined = " ".join(cmd)
        if "--paginate" in cmd:
            if "sub_issues" in joined or "blocked_by" in joined:
                return subprocess.CompletedProcess(
                    cmd, 0, '[[{"number": 2, "id": 20}]]', ""
                )
            return subprocess.CompletedProcess(
                cmd,
                0,
                '[[{"number": 1, "id": 10, "title": "T", "body": "B", "state": "open", "state_reason": null, "labels": [{"name": "l"}], "type": {"name": "Task"}}]]',
                "",
            )
        if "search/issues" in joined:
            return subprocess.CompletedProcess(
                cmd, 0, '{"items": [{"number": 1, "id": 10}]}', ""
            )
        if "issue-types" in joined:
            return subprocess.CompletedProcess(
                cmd,
                0,
                '[{"name": "Task", "is_enabled": true}]',
                "",
            )
        return subprocess.CompletedProcess(
            cmd,
            0,
            '{"number": 1, "id": 10, "title": "T", "body": "B", "state": "open", "state_reason": null, "labels": [{"name": "l"}], "type": {"name": "Task"}}',
            "",
        )

    monkeypatch.setattr(github_client, "run_with_timeout", fake_run)
    gh = github_client.SubprocessGh()
    assert len(gh.list_issues("o/r", "l")) == 1
    assert gh.get_issue("o/r", 1).number == 1
    assert len(gh.search_marker("o/r", "cap-1")) == 1
    assert len(gh.list_sub_issues("o/r", 1)) == 1
    assert len(gh.list_blocked_by("o/r", 1)) == 1
    assert gh.list_issue_types("o/r") == {"Task"}
    gh.ensure_label("o/r", "l")
    assert gh.create_issue("o/r", "T", "B", ["l"], "Task").number == 1
    assert (
        gh.update_issue(
            "o/r",
            1,
            IssuePatch(
                title="T2",
                body="B2",
                issue_type="Task",
                state=DesiredState(is_open=False, close_reason=CloseReason.COMPLETED),
            ),
        ).number
        == 1
    )
    gh.add_labels("o/r", 1, ["a"])
    gh.remove_label("o/r", 1, "a")
    gh.add_sub_issue("o/r", 1, 20)
    gh.add_blocked_by("o/r", 1, 20)
    assert ["gh", "api", "repos/o/r/labels/l"] in seen_cmds
    assert [
        "gh",
        "api",
        "--method",
        "POST",
        "repos/o/r/issues/1/labels",
        "-f",
        "labels[]=a",
    ] in seen_cmds
    assert [
        "gh",
        "api",
        "--method",
        "DELETE",
        "repos/o/r/issues/1/labels/a",
    ] in seen_cmds
    assert [
        "gh",
        "api",
        "--method",
        "POST",
        "repos/o/r/issues/1/sub_issues",
        "-F",
        "sub_issue_id=20",
    ] in seen_cmds
    assert [
        "gh",
        "api",
        "--method",
        "POST",
        "repos/o/r/issues/1/dependencies/blocked_by",
        "-F",
        "issue_id=20",
    ] in seen_cmds
    assert [
        "gh",
        "api",
        "--method",
        "POST",
        "repos/o/r/issues",
        "-f",
        "title=T",
        "-f",
        "body=B",
        "-f",
        "type=Task",
        "-f",
        "labels[]=l",
    ] in seen_cmds
    assert [
        "gh",
        "api",
        "--method",
        "PATCH",
        "repos/o/r/issues/1",
        "-f",
        "title=T2",
        "-f",
        "body=B2",
        "-f",
        "type=Task",
        "-f",
        "state=closed",
        "-f",
        "state_reason=completed",
    ] in seen_cmds
    assert str(github_client.GhSpawnError("gh", OSError("missing")))
    assert str(github_client.GhCommandError("op", "exit 1", "err"))
    assert str(github_client.GhParseError("op", "bad json"))

    from nmemory.commands import layout

    doc_stage = tmp_path / "doc-stage"
    doc_stage.mkdir()
    (doc_stage / "README.md").write_text(
        "[Present](present.md)\n[Missing](absent.md)\n"
    )
    (doc_stage / "present.md").write_text("# Present\n")
    assert layout.local_links(doc_stage) == ["README.md"]
    assert (
        layout.rewrite_links(
            "See [guide](../../docs/guide.md#sec)",
            Path("mcps/nmemory/README.md"),
            Path("README.md"),
            {"docs/guide.md": "docs/guide.md", "mcps/nmemory": "."},
        )
        == "See [guide](docs/guide.md#sec)"
    )

    from nmemory.tools import validation

    assert "expected `a`" in validation._format_unknown_field_msg("x", ["a"])
    assert "expected one of" in validation._format_unknown_variant_msg(
        "x", ["a", "b", "c"]
    )
    for sample_val in (3.14, [1], {"a": 1}, None, object()):
        assert "invalid type" in validation._format_type_error(
            "f", sample_val, "a string"
        )
    for bad_extract in (
        {"bad": "v"},
        {"content": "a", "text": "b"},
        {},
        {"content": 123},
    ):
        with pytest.raises(validation.DeserializationError):
            validation.validate_tool_arguments("nmemory_turn_extract", bad_extract)
    assert validation.validate_tool_arguments(
        "nmemory_turn_extract", {"content": "ok"}
    ) == {"content": "ok"}
    with pytest.raises(validation.DeserializationError):
        validation.validate_tool_arguments(
            "nmemory_kind_classify",
            {"content": "ok", "capsule_id": "cap-1", "id": "cap-1"},
        )
    assert validation.validate_tool_arguments(
        "nmemory_capsule_retrieve", {"query": "term"}
    ) == {"terms": ["term"]}
    for bad_retrieve in (
        {"terms": [123]},
        {"terms": ["a"], "time_window": {"bad": "x"}},
        {"terms": ["a"], "time_window": {"from": 123}},
    ):
        with pytest.raises(validation.DeserializationError):
            validation.validate_tool_arguments("nmemory_capsule_retrieve", bad_retrieve)
    assert validation.validate_project_prefix(None) is None
    assert validation.validate_project_prefix("   ") is not None
    assert validation.validate_project_prefix("nott/") is not None
    assert validation.validate_project_prefix("nott") is None
    for tool_name, bad_args in (
        ("nmemory_capsule_show", "not-a-dict"),
        ("nmemory_capsule_show", {"id": 123}),
        ("nmemory_capsule_show", {"ids": 123}),
        ("nmemory_capsule_show", {"ids": [123]}),
        ("nmemory_capsule_show", {}),
        ("nmemory_capsule_ingest", {"items": 123}),
        (
            "nmemory_capsule_ingest",
            {"content": "c", "source": "s", "anchor": "a", "kind": "bad-kind"},
        ),
        (
            "nmemory_capsule_ingest",
            {
                "content": "c",
                "source": "s",
                "anchor": "a",
                "kind": "fact",
                "derived_from": "not-a-list",
            },
        ),
        (
            "nmemory_capsule_ingest",
            {
                "content": "c",
                "source": "s",
                "anchor": "a",
                "kind": "fact",
                "derived_from": [123],
            },
        ),
    ):
        with pytest.raises(validation.DeserializationError):
            validation.validate_tool_arguments(tool_name, bad_args)
    assert validation.validate_tool_arguments("unknown_tool", {"x": 1}) == {"x": 1}


def test_release_export_accepts_every_declared_component() -> None:
    from nmemory.commands.common import parse_command

    root = Path(__file__).resolve().parents[3]
    contract = json.loads((root / "ops/mirror/projects.json").read_text())
    assert contract["repositories"]["napple"] == "menot-you/n-apple"
    assert contract["components"]["napple"]["primary"] == "mcps/napple"
    for component in contract["repositories"]:
        if component == "plugin":
            continue
        args = parse_command(
            "release",
            "export",
            ["--component", component, "--source-root", ".", "--output", "out"],
        )
        assert args.component == component
