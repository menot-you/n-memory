"""Read the public layout and prepare its package metadata and documents."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tomllib
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

LICENSE = "AGPL-3.0-only"
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
LINK = re.compile(
    r"(?<=\]\()(?P<inline><[^>\n]+>|[^\s)]+)"
    r"|(?m:^\s*\[[^]\n]+\]:\s*)(?P<reference><[^>\n]+>|[^\s]+)"
    r"|\b(?:href|src|xlink:href)=[\"'](?P<html>[^\"']+)[\"']"
)


def relative(value: str) -> Path:
    path = PurePosixPath(value)
    if not path.parts or path.is_absolute() or ".." in path.parts or "\\" in value:
        raise ValueError("public export: unsafe map path")
    return Path(*path.parts)


def read(root: Path) -> dict[str, Any]:
    value: dict[str, Any] = json.loads((root / "ops/mirror/projects.json").read_text())
    if value.get("schema") != 1:
        raise ValueError("public export: unsupported layout map")
    for component, entry in value["components"].items():
        relative(entry["primary"])
        destinations = set()
        for origin, destination in entry["packages"].items():
            relative(origin)
            relative(destination)
            if destination in destinations or not destination.startswith(
                ("packages/", "evals")
            ):
                raise ValueError(
                    "public export: duplicate or unsafe member destination"
                )
            destinations.add(destination)
        if component != "plugin" and not entry.get("module"):
            raise ValueError("public export: missing primary module")
    for member, resources in value.get("test_resources", {}).items():
        relative(member)
        destinations = set()
        for origin, target in resources.items():
            relative(origin)
            relative(target)
            if not target.startswith("tests/resources/") or target in destinations:
                raise ValueError("public export: unsafe test resource destination")
            destinations.add(target)
    return value


def paths(contract: dict[str, Any], component: str) -> dict[str, str]:
    entry = contract["components"][component]
    return {entry["primary"]: ".", **entry["packages"]}


def destination(name: str, mapping: dict[str, str]) -> str:
    for origin in sorted(mapping, key=len, reverse=True):
        if name == origin or name.startswith(origin + "/"):
            return (Path(mapping[origin]) / Path(name).relative_to(origin)).as_posix()
    return name


def resources(
    root: Path,
    stage: Path,
    contract: dict[str, Any],
    mapping: dict[str, str],
    copy: Any,
) -> None:
    """Copy declared test files from their source home into selected tests."""
    public = root / "source-manifest.json"
    source_mapping = (
        paths(contract, json.loads(public.read_text())["component"])
        if public.is_file()
        else {}
    )
    for member, entries in contract.get("test_resources", {}).items():
        if member not in mapping:
            continue
        for origin, target in entries.items():
            source = (
                Path(source_mapping[member]) / relative(target)
                if public.is_file()
                else relative(origin)
            )
            if not (root / source).is_file():
                raise ValueError(f"public export: missing test resource: {source}")
            copy(root, source, stage, Path(mapping[member]) / relative(target))


def hooks(root: Path, stage: Path, origin: Path, target: Path, copy: Any) -> None:
    """Copy the required memory hooks into the selected package."""
    for name in HOOKS:
        copy(root, origin / name, stage, target / name)


def rewrite_links(
    text: str, source: Path, target: Path, mapping: dict[str, str]
) -> str:
    def rewrite(match: re.Match[str]) -> str:
        assert match.lastgroup is not None
        value = match.group(match.lastgroup)
        url = urlsplit(value.strip("<>"))
        if url.scheme or url.netloc or not url.path:
            return match.group()
        origin = os.path.normpath(
            unquote(url.path).lstrip("/")
            if url.path.startswith("/")
            else str(source.parent / unquote(url.path))
        )
        selected = destination(origin, mapping)
        link = os.path.relpath(selected, target.parent)
        result = urlunsplit(("", "", link.replace(" ", "%20"), url.query, url.fragment))
        replacement = f"<{result}>" if value.startswith("<") else result
        start, end = match.span(match.lastgroup)
        return (
            match.group()[: start - match.start()]
            + replacement
            + match.group()[end - match.start() :]
        )

    return LINK.sub(rewrite, text)


def local_links(root: Path) -> list[str]:
    missing = []
    for path in sorted(root.rglob("*")):
        if path.suffix not in {".md", ".html", ".svg"} or not path.is_file():
            continue
        for match in LINK.finditer(path.read_text()):
            assert match.lastgroup is not None
            url = urlsplit(match.group(match.lastgroup).strip("<>"))
            if url.scheme or url.netloc or not url.path:
                continue
            target = (
                root / unquote(url.path).lstrip("/")
                if url.path.startswith("/")
                else path.parent / unquote(url.path)
            ).resolve()
            if not target.is_relative_to(root.resolve()) or not target.exists():
                missing.append(path.relative_to(root).as_posix())
    return missing


def metadata(root: Path, stage: Path, mapping: dict[str, str]) -> dict[str, Any]:
    public = (root / "source-manifest.json").is_file()
    packages = {}
    for origin, target in mapping.items():
        if origin == "plugin":
            continue
        path = stage / target / "pyproject.toml"
        parsed = tomllib.loads(path.read_text())
        project = parsed["project"]
        packages[project["name"]] = {
            "member": target,
            "module": project["name"].replace("-", "_"),
            "version": project["version"],
            "license": project.get("license", LICENSE),
        }
    for origin, target in mapping.items():
        if origin == "plugin":
            continue
        path = stage / target / "pyproject.toml"
        text = path.read_text()
        parsed = tomllib.loads(text)
        sources = parsed.get("tool", {}).get("uv", {}).get("sources", {})
        project = parsed["project"]
        dependencies = [
            *project.get("dependencies", []),
            *(
                item
                for group in project.get("optional-dependencies", {}).values()
                for item in group
            ),
        ]
        for name, source in sources.items():
            if source.get("workspace") and name not in packages:
                raise ValueError(
                    f"public export: workspace dependency is outside the selected closure: {name}"
                )
            if "path" in source:
                raise ValueError(
                    "public export: package path sources require a workspace source"
                )
        for requirement in dependencies:
            name = (
                re.split(r"[\s\[<>=!~@;]", requirement, maxsplit=1)[0]
                .lower()
                .replace("_", "-")
            )
            if (name in packages or name == "nspawn") and sources.get(name) != {
                "workspace": True
            }:
                raise ValueError(
                    f"public export: {name} must use its selected workspace source"
                )
        if "license" not in project:
            text = text.replace("[project]\n", f'[project]\nlicense = "{LICENSE}"\n', 1)
        if "license-files" not in project:
            text = text.replace(
                "[project]\n", '[project]\nlicense-files = ["LICENSE"]\n', 1
            )
        shutil.copyfile(root / "LICENSE", stage / target / "LICENSE")
        for config, field in (("vulture", "paths"), ("mypy", "mypy_path")):
            values = parsed.get("tool", {}).get(config, {}).get(field, [])
            for value in values:
                if not value.startswith("../"):
                    continue
                source_path = os.path.normpath(
                    str(Path(target if public else origin) / value)
                )
                selected = source_path if public else destination(source_path, mapping)
                if (not public and selected == source_path) or (
                    public and not (stage / relative(selected)).exists()
                ):
                    raise ValueError(
                        "public export: package metadata references an unselected source"
                    )
                text = text.replace(
                    json.dumps(value), json.dumps(os.path.relpath(selected, target))
                )
        path.write_text(text)
    browser = mapping.get("mcps/nbrowser")
    if browser is not None and (stage / browser / "npm/package.json").is_file():
        path = stage / browser / "npm/package.json"
        data = json.loads(path.read_text())
        data.setdefault("license", LICENSE)
        path.write_text(json.dumps(data, indent=2) + "\n")
    return packages


def documents(
    root: Path, stage: Path, contract: dict[str, Any], component: str, copy: Any
) -> None:
    public = (root / "source-manifest.json").is_file()
    document_path = root / "docs/design/public/export-map.json"
    if public and (root / "docs/export-map.json").is_file():
        document_path = root / "docs/export-map.json"
    document = json.loads(document_path.read_text())
    if document.get("schema_version") != 1:
        raise ValueError("public export: unsupported documentation map")
    target = contract["repositories"][component]
    try:
        mapping = document["targets"][target]
        readme = document["root_readmes"][target]
        changelog = document["root_changelog"]
    except KeyError, TypeError:
        raise ValueError("public export: incomplete documentation map") from None
    if not isinstance(mapping, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in mapping.items()
    ):
        raise ValueError("public export: unsupported documentation map")
    if len(set(mapping.values())) != len(mapping):
        raise ValueError("public export: duplicate document destination")
    if readme not in mapping or changelog not in mapping:
        raise ValueError("public export: incomplete documentation map")
    for origin, target_name in mapping.items():
        relative(origin)
        relative(target_name)
    required = [*contract["documents"]["root"], *contract["documents"]["docs"]]
    if not set(required) <= {*mapping.values(), "README.md", "CHANGELOG.md"}:
        raise ValueError("public export: missing required document mapping")
    links = {
        **paths(contract, component),
        **mapping,
        readme: "README.md",
        changelog: "CHANGELOG.md",
    }
    if not public:
        code = paths(contract, component)
        for path in sorted(stage.rglob("*")):
            if (
                path.suffix not in {".md", ".svg", ".html"}
                or not path.is_file()
                or path.is_symlink()
            ):
                continue
            emitted = path.relative_to(stage)
            for origin, destination_name in sorted(
                code.items(), key=lambda item: len(item[1]), reverse=True
            ):
                destination_path = Path(destination_name)
                if emitted.is_relative_to(destination_path):
                    source = Path(origin) / emitted.relative_to(destination_path)
                    path.write_text(
                        rewrite_links(path.read_text(), source, emitted, links)
                    )
                    break
    for origin, target_name in mapping.items():
        source = relative(target_name if public else origin)
        target_path = relative(target_name)
        if not (root / source).is_file():
            raise ValueError(f"public export: missing document source: {source}")
        copy(root, source, stage, target_path)
        if source.suffix in {".md", ".html", ".svg"}:
            (stage / target_path).write_text(
                rewrite_links(
                    (root / source).read_text(),
                    source,
                    target_path,
                    {} if public else links,
                )
            )
    for origin, target_name in ((readme, "README.md"), (changelog, "CHANGELOG.md")):
        source = relative(mapping[origin] if public else origin)
        copy(root, source, stage, Path(target_name))
        (stage / target_name).write_text(
            rewrite_links(
                (root / source).read_text(),
                source,
                Path(target_name),
                {} if public else links,
            )
        )
    for name in required:
        if not (stage / name).is_file():
            raise ValueError(f"public export: missing required document: {name}")


def inventory(stage: Path) -> dict[str, Any]:
    return {
        path.relative_to(stage).as_posix(): {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "mode": path.stat().st_mode & 0o777,
            "executable": bool(path.stat().st_mode & 0o111),
            **({"link": os.readlink(path)} if path.is_symlink() else {}),
        }
        for path in sorted(stage.rglob("*"))
        if path.is_file()
    }


def diagnostic(text: str) -> str:
    """Remove URL credentials and token values from a resolver diagnostic."""
    text = re.sub(r"(https?://)[^/\s@]+@", r"\1<redacted>@", text)
    return re.sub(
        r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|sk-(?:live-|proj-)?[A-Za-z0-9_-]{24,})\b",
        "<redacted>",
        text,
    )
