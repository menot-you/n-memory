"""Configure one project with a pinned runtime and reversible client settings.

Also scan repository facts into staged memory proposals.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import tomllib
import urllib.parse
from pathlib import Path
from typing import Any

from ..config import ConfigError, _file_identity, _validate_document
from ..config import _read_for_write as _read
from ..curator import CuratorClient, CuratorError
from ..database import create_store, store_path
from ..ingest import IngestDefaults
from ..project import canonical_project
from ..project_scan import scan_project
from ..renamed import env
from ..tools.common import HandlerState
from .common import open_store_from_args, parse_command


def _signing_refused() -> bytes:
    raise CuratorError("repository scans cannot sign owner approvals")


def cmd_project_scan(argv: list[str]) -> None:
    args = parse_command("project", "scan", argv)
    store = open_store_from_args(args)
    anchor_root = env("NOTT_NMEMORY_ANCHOR_ROOT") or os.getcwd()
    state = HandlerState(
        store=store,
        anchor_root=anchor_root,
        embeddings=None,
        read_only=False,
        home_dir=None,
        project_dir=anchor_root,
        effective_defaults=lambda _: IngestDefaults(args.project, source="caller"),
        resolve_hmac_key=_signing_refused,
    )
    proposals = scan_project(state, Path(args.root), args.project, CuratorClient())
    for proposal in proposals:
        print(proposal)
    print("Review proposals with nmemory_proposal_review. Choose ratified or rejected.")


def _write(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    staged = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staged, path)
    finally:
        staged.unlink(missing_ok=True)


def _json(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw) if raw else {}
    except ValueError:
        raise ConfigError("project bootstrap: invalid client JSON") from None
    if not isinstance(value, dict):
        raise ConfigError("project bootstrap: client configuration must be an object")
    return value


def _table(document: dict[str, Any], key: str) -> dict[str, Any]:
    value = document.setdefault(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"project bootstrap: {key} must be an object")
    return value


def _encoded(document: dict[str, Any]) -> bytes:
    return (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode()


def mcp_restore(
    record: dict[str, Any], report: dict[str, Any], current: bytes
) -> bytes:
    """Check the recorded MCP binding. Remove only that binding from a copy."""
    try:
        before = base64.b64decode(record["before"], validate=True)
    except ValueError, KeyError, TypeError:
        raise ConfigError("project bootstrap: restoration bytes are invalid") from None
    original = _json(before)
    field_present = "mcpServers" in original
    servers = _table(original, "mcpServers")
    if "nmemory" in servers:
        raise ConfigError("project bootstrap: restoration receipt is invalid")
    binding = {
        "command": report.get("python"),
        "args": [
            "-I",
            report.get("launcher"),
            "mcp",
            "serve",
            "--project",
            report.get("project_id"),
        ],
    }
    servers["nmemory"] = binding
    if hashlib.sha256(_encoded(original)).hexdigest() != record.get("after"):
        raise ConfigError("project bootstrap: restoration receipt is invalid")
    document = _json(current)
    servers = _table(document, "mcpServers")
    if servers.get("nmemory") != binding:
        raise ConfigError("project bootstrap: configuration changed after setup")
    del servers["nmemory"]
    if not servers and not field_present:
        del document["mcpServers"]
    return _encoded(document)


def codex_restore(
    record: dict[str, Any], report: dict[str, Any], current: bytes
) -> bytes:
    """Check the recorded Codex MCP table. Remove only that table from a copy."""
    try:
        before = base64.b64decode(record["before"], validate=True).decode()
        text = current.decode()
    except ValueError, KeyError, TypeError:
        raise ConfigError("project bootstrap: restoration bytes are invalid") from None
    try:
        original = tomllib.loads(before)
    except ValueError:
        raise ConfigError("project bootstrap: restoration receipt is invalid") from None
    if "nmemory" in original.get("mcp_servers", {}):
        raise ConfigError("project bootstrap: restoration receipt is invalid")
    args = [
        "-I",
        report.get("launcher"),
        "mcp",
        "serve",
        "--project",
        report.get("project_id"),
    ]
    snippet = (
        "\n[mcp_servers.nmemory]\ncommand = "
        + json.dumps(report.get("python"))
        + "\nargs = "
        + json.dumps(args)
        + "\n"
    )
    if hashlib.sha256((before + snippet).encode()).hexdigest() != record.get("after"):
        raise ConfigError("project bootstrap: restoration receipt is invalid")
    try:
        document = tomllib.loads(text)
    except ValueError:
        raise ConfigError("project bootstrap: invalid Codex TOML") from None
    if document.get("mcp_servers", {}).get("nmemory") != {
        "command": report.get("python"),
        "args": args,
    }:
        raise ConfigError("project bootstrap: configuration changed after setup")
    table_body = snippet[1:]
    if "\n\n" + table_body in text:
        restored = text.replace("\n\n" + table_body, "\n", 1)
    elif text.startswith("\n" + table_body):
        restored = text[len(table_body) + 1 :]
    elif table_body in text:
        restored = text.replace(table_body, "", 1)
    else:
        matches = list(
            re.finditer(r"(?m)^\[[^\r\n]+\][ \t]*(?:#[^\n]*)?(?:\n|$)", text)
        )
        for index, match in enumerate(matches):
            if match.group().strip() == "[mcp_servers.nmemory]":
                start = match.start()
                end = (
                    matches[index + 1].start()
                    if index + 1 < len(matches)
                    else len(text)
                )
                span = text[start:end]
                body = re.sub(
                    r"(?:\r?\n[ \t]*(?:#[^\r\n]*)?)+\Z",
                    "\n" if span.endswith("\n") else "",
                    span,
                )
                cut_start = (
                    start - 1
                    if start >= 2 and text[start - 2 : start] == "\n\n"
                    else start
                )
                restored = text[:cut_start] + text[start + len(body) :]
                break
        else:
            raise ConfigError("project bootstrap: configuration changed after setup")
    try:
        tomllib.loads(restored)
    except ValueError:
        raise ConfigError("project bootstrap: invalid Codex TOML") from None
    return restored.encode()


def _runtime(project: str, config: Path, mode: str, hooks: Path) -> bytes:
    # Keep the venv path. Resolving its symlink would select the base interpreter.
    python = os.path.abspath(sys.executable)
    allowed = [
        "HOME",
        "PATH",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "XDG_STATE_HOME",
        "XDG_RUNTIME_DIR",
        "NOTT_DATABASE_DIR",
        "CLAUDE_CODE_ENTRYPOINT",
        "NOTT_NSPAWN_RUN_ID",
        "NOTT_NSPAWN_SESSION_ID",
    ]
    if mode == "native":
        from nott_system1 import SCOPES, route_variables

        allowed.extend(
            name for scope in (None, *sorted(SCOPES)) for name in route_variables(scope)
        )
    return (
        f"#!{python} -I\n"
        + """import os
import sys
"""
        + f"environment = {{name: os.environ[name] for name in {allowed!r} if name in os.environ}}\n"
        + f"environment.update({{'NOTT_NMEMORY_PROJECT': {project!r}, 'NOTT_NMEMORY_CONFIG': {str(config)!r}, 'NOTT_NMEMORY_CURATOR': '0', 'NOTT_NMEMORY_SYSTEM1': {'0' if mode == 'lexical' else '1'!r}, 'NOTT_NMEMORY_BIN': __file__, 'NOTT_NMEMORY_PYTHON': {python!r}}})\n"
        + f"python = {python!r}\nhooks = {str(hooks)!r}\n"
        + """arguments = sys.argv[1:]
if arguments[:1] == ["--hook"]:
    import subprocess
    name = arguments[1]
    if name not in ("nmemory-session.sh", "nmemory-recall.py", "nmemory-capture-gate.py"):
        raise SystemExit("unsupported memory hook")
    command = (["bash"] if name.endswith(".sh") else [python, "-I"])
    raise SystemExit(subprocess.call(command + [os.path.join(hooks, name), *arguments[2:]], env=environment))
if arguments == ["--system1-probe"]:
    os.environ.clear()
    os.environ.update(environment)
    from nmemory.system1 import taint_probe
    result = taint_probe(["The test statement contains plain data."])
    if result is None or result[1] is not None or result[0] == [None]:
        raise SystemExit("System1 probe failed")
    print('{"ready":true}')
else:
    os.execve(python, [python, "-I", "-m", "nmemory", *arguments], environment)
"""
    ).encode()


def _probe(launcher: Path, project: str, mode: str) -> dict[str, bool]:
    """Check the configured stdio consumer and the selected System1 route."""
    import anyio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def check() -> None:
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-I", str(launcher), "mcp", "serve", "--project", project],
            env=dict(os.environ),
        )
        with anyio.fail_after(20):
            async with (
                stdio_client(parameters, errlog=sink) as (reader, writer),
                ClientSession(reader, writer) as session,
            ):
                await session.initialize()
                catalog = await session.list_tools()
                if "nmemory_capsule_ingest" not in {
                    tool.name for tool in catalog.tools
                }:
                    raise ConfigError("project bootstrap: MCP capture tool is absent")
                result = await session.call_tool(
                    "nmemory_context_show",
                    {"query": "project setup readiness", "project_id": project},
                )
                if result.is_error:
                    raise ConfigError("project bootstrap: MCP context probe failed")

    try:
        with open(os.devnull, "w") as sink:
            anyio.run(check)
    except (ExceptionGroup, TimeoutError, OSError) as error:
        raise ConfigError(
            f"project bootstrap: MCP probe failed ({type(error).__name__})"
        ) from None
    if mode != "lexical":
        result = subprocess.run(
            [sys.executable, "-I", str(launcher), "--system1-probe"],
            capture_output=True,
            timeout=25,
            check=False,
        )
        if result.returncode or result.stdout.strip() != b'{"ready":true}':
            raise ConfigError("project bootstrap: System1 probe failed")
    return {"mcp": True, "system1": mode != "lexical"}


def _hooks_directory(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit.resolve(strict=True)
    package = Path(__file__).resolve().parents[1] / "hooks"
    if package.is_dir():
        return package
    # Source execution is also supported. Installed wheels use package resources.
    return Path(__file__).resolve().parents[4] / "plugin/hooks"


def _restore(receipt: Path, root: Path, *, failed: bool = False) -> dict[str, Any]:
    raw, _ = _read(receipt)
    document = _json(raw)
    records = document.get("files")
    if (
        document.get("schema") != 1
        or document.get("root") != str(root)
        or not isinstance(records, list)
    ):
        raise ConfigError("project bootstrap: restoration receipt is invalid")
    allowed = {
        root / name
        for name in (
            ".mcp.json",
            ".claude/settings.json",
            ".codex/config.toml",
            ".nott/nmemory/.gitignore",
            ".nott/nmemory/runtime.py",
            ".nott/nmemory/memory.toml",
        )
    }
    restored: dict[Path, bytes] = {}
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            raise ConfigError("project bootstrap: restoration record is invalid")
        path = Path(record["path"])
        if path not in allowed or path.resolve() != path:
            raise ConfigError(
                "project bootstrap: restoration path is outside the project"
            )
        if (
            not isinstance(record.get("existed"), bool)
            or type(record.get("mode")) is not int
            or not 0 <= record["mode"] <= 0o7777
        ):
            raise ConfigError("project bootstrap: restoration metadata is invalid")
        current, metadata = _read(path)
        expected = record.get("after")
        if metadata is None:
            raise ConfigError("project bootstrap: configuration changed after setup")
        try:
            restored[path] = base64.b64decode(record["before"], validate=True)
        except ValueError, KeyError, TypeError:
            raise ConfigError(
                "project bootstrap: restoration bytes are invalid"
            ) from None
        if hashlib.sha256(current).hexdigest() != expected:
            report = document.get("report")
            if not isinstance(report, dict) or path not in {
                root / ".mcp.json",
                root / ".codex/config.toml",
            }:
                raise ConfigError(
                    "project bootstrap: configuration changed after setup"
                )
            restored[path] = (
                mcp_restore(record, report, current)
                if path == root / ".mcp.json"
                else codex_restore(record, report, current)
            )
    for record in reversed(records):
        path = Path(record["path"])
        if (
            record["existed"]
            or (path == root / ".mcp.json" and _json(restored[path]))
            or (path == root / ".codex/config.toml" and restored[path].strip())
        ):
            _write(path, restored[path], record["mode"])
        else:
            path.unlink()
    receipt.unlink()
    return {
        "schema": 1,
        "status": "restored",
        "root": str(root),
        "database_preserved": True,
        "failed_setup": failed,
    }


def cmd_project_bootstrap(argv: list[str]) -> None:
    args = parse_command("project", "bootstrap", argv)
    root = args.root.resolve(strict=True)
    if (
        not root.is_dir()
        or root.stat().st_uid != os.geteuid()
        or root.stat().st_mode & 0o022
    ):
        raise ConfigError(
            "project bootstrap: root must be an owner-controlled directory"
        )
    owned = root / ".nott/nmemory"
    for path in (
        root / ".nott",
        owned,
        root / ".claude",
        root / ".codex",
        root / ".gemini",
    ):
        if path.is_symlink() or (
            path.exists()
            and (
                not path.is_dir()
                or path.stat().st_uid != os.geteuid()
                or path.stat().st_mode & 0o022
            )
        ):
            raise ConfigError(
                "project bootstrap: configuration directory must be owner-controlled"
            )
    owned.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = owned / "bootstrap.lock"
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "rb+") as stream:
        metadata = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o077
            or metadata.st_nlink != 1
        ):
            raise ConfigError("project bootstrap: setup lock must be private")
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        _bootstrap(args, root, owned)


def _bootstrap(args: Any, root: Path, owned: Path) -> None:
    receipt = owned / "bootstrap.json"
    if args.restore:
        print(json.dumps(_restore(receipt, root)))
        return
    if not args.project or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._/-]*", args.project
    ):
        raise ConfigError("project bootstrap: --project requires a project identifier")
    if args.system1 == "lexical" and (
        args.endpoint is not None or args.model is not None
    ):
        raise ConfigError("project bootstrap: lexical mode takes no endpoint or model")
    project = canonical_project(args.project)
    hooks = _hooks_directory(args.hooks_dir)
    required = (
        "nmemory-session.sh",
        "nmemory-recall.py",
        "nmemory-capture-gate.py",
        "renamed.py",
        "renamed.sh",
        "system1-suggest.py",
        "system1.py",
    )
    if args.client == "claude" and not all(
        (hooks / name).is_file() for name in required
    ):
        raise ConfigError("project bootstrap: packaged memory hooks are missing")
    launcher = owned / "runtime.py"
    config = owned / "memory.toml"
    runtime = _runtime(project, config, args.system1, hooks)
    prior, _ = _read(receipt)
    if prior:
        document = _json(prior)
        previous_report = document.get("report")
        if not isinstance(previous_report, dict) or document.get("request") != [
            project,
            args.client,
            args.system1,
            args.endpoint,
            args.model,
            str(hooks),
            os.path.abspath(sys.executable),
        ]:
            raise ConfigError(
                "project bootstrap: restore the previous setup before changing its options"
            )
        for record in document["files"]:
            path = Path(record["path"])
            current, _ = _read(path)
            if hashlib.sha256(current).hexdigest() != record["after"]:
                if path == root / ".mcp.json":
                    mcp_restore(record, previous_report, current)
                elif path == root / ".codex/config.toml":
                    codex_restore(record, previous_report, current)
                else:
                    raise ConfigError(
                        "project bootstrap: configuration changed after setup"
                    )
        proof = _probe(launcher, project, args.system1)
        print(json.dumps({**previous_report, "status": "unchanged", "probe": proof}))
        return
    config_raw, _ = _read(config)
    document = _validate_document(config, config_raw)
    text = config_raw.decode()
    if args.system1 != "lexical":
        if not args.endpoint or not args.model:
            raise ConfigError(
                "project bootstrap: System1 requires --endpoint and --model"
            )
        try:
            endpoint = urllib.parse.urlsplit(args.endpoint)
            port = endpoint.port
        except ValueError:
            raise ConfigError("project bootstrap: invalid System1 endpoint") from None
        if (
            endpoint.scheme not in ("http", "https")
            or port == 0
            or not endpoint.hostname
            or endpoint.username is not None
            or endpoint.password is not None
            or endpoint.query
            or endpoint.fragment
            or not args.model.strip()
        ):
            raise ConfigError(
                "project bootstrap: System1 requires an endpoint without credentials and a model"
            )
        selected = {
            "endpoint": args.endpoint,
            "model": args.model,
            "protocol": "openai-compatible" if args.system1 == "local" else "native",
        }
        if "system1" in document and any(
            document["system1"].get(key, "native" if key == "protocol" else None)
            != value
            for key, value in selected.items()
        ):
            raise ConfigError(
                "project bootstrap: existing System1 settings differ from the selected route"
            )
        if "system1" not in document:
            text += "\n[system1]\n" + "".join(
                f"{key} = {json.dumps(value)}\n" for key, value in selected.items()
            )
    _validate_document(config, text.encode())
    ignore = owned / ".gitignore"
    ignore_raw, _ = _read(ignore)
    ignore_rules = (
        ignore_raw
        + (b"\n" if ignore_raw and not ignore_raw.endswith(b"\n") else b"")
        + b"/bootstrap.json\n/bootstrap.lock\n/runtime.py\n/memory.toml\n"
    )
    files: dict[Path, tuple[bytes, int]] = {
        ignore: (ignore_rules, 0o600),
        launcher: (runtime, 0o700),
        config: (text.encode(), 0o600),
    }
    binding = {
        "command": os.path.abspath(sys.executable),
        "args": ["-I", str(launcher), "mcp", "serve", "--project", project],
    }
    if args.client == "codex":
        target = root / ".codex/config.toml"
        raw, _ = _read(target)
        try:
            data = tomllib.loads(raw.decode())
        except UnicodeDecodeError, ValueError:
            raise ConfigError("project bootstrap: invalid Codex TOML") from None
        if "nmemory" in data.get("mcp_servers", {}):
            raise ConfigError("project bootstrap: nmemory MCP binding already exists")
        edited = (
            raw.decode()
            + "\n[mcp_servers.nmemory]\ncommand = "
            + json.dumps(binding["command"])
            + "\nargs = "
            + json.dumps(binding["args"])
            + "\n"
        )
        tomllib.loads(edited)
        files[target] = (edited.encode(), 0o600)
    else:
        target = root / ".mcp.json"
        raw, _ = _read(target)
        data = _json(raw)
        servers = _table(data, "mcpServers")
        if "nmemory" in servers:
            raise ConfigError("project bootstrap: nmemory MCP binding already exists")
        servers["nmemory"] = binding
        files[target] = (_encoded(data), 0o600)
    events: dict[str, list[tuple[str, list[str]]]] = {
        "SessionStart": [("nmemory-session.sh", []), ("nmemory-session.sh", ["pins"])],
        "UserPromptSubmit": [("nmemory-recall.py", [])],
        "Stop": [("nmemory-capture-gate.py", [])],
    }
    if args.client == "claude":
        settings = root / ".claude/settings.json"
        raw, _ = _read(settings)
        data = _json(raw)
        registered = _table(data, "hooks")
        for event, entries in events.items():
            group = registered.setdefault(event, [])
            if not isinstance(group, list):
                raise ConfigError("project bootstrap: hook event must be a list")
            for name, extra in entries:
                command = shlex.join(
                    [
                        os.path.abspath(sys.executable),
                        str(launcher),
                        "--hook",
                        name,
                        *extra,
                    ]
                )
                group.append(
                    {"hooks": [{"type": "command", "command": command, "timeout": 10}]}
                )
        files[settings] = (_encoded(data), 0o600)
    records: list[dict[str, Any]] = []
    for path, (after, mode) in files.items():
        before, metadata = _read(path)
        records.append(
            {
                "path": str(path),
                "before": base64.b64encode(before).decode(),
                "existed": metadata is not None,
                "mode": stat.S_IMODE(metadata.st_mode) if metadata else mode,
                "after": hashlib.sha256(after).hexdigest(),
                "identity": _file_identity(metadata),
            }
        )
    database = store_path()
    created = not database.exists()
    if created:
        create_store(database, allow_live_store=True)
    report: dict[str, Any] = {
        "schema": 1,
        "status": "configured",
        "project_id": project,
        "root": str(root),
        "client": args.client,
        "python": os.path.abspath(sys.executable),
        "launcher": str(launcher),
        "config_paths": [str(path) for path in files if path != launcher],
        "receipt_path": str(receipt),
        "database": {"path": str(database), "created": created},
        "system1": {"mode": args.system1, "ready": False},
        "hooks": {
            "enabled": args.client == "claude",
            "events": list(events) if args.client == "claude" else [],
        },
    }
    transaction = {
        "schema": 1,
        "root": str(root),
        "request": [
            project,
            args.client,
            args.system1,
            args.endpoint,
            args.model,
            str(hooks),
            os.path.abspath(sys.executable),
        ],
        "files": records,
        "report": report,
    }
    written: list[dict[str, Any]] = []
    try:
        # Record restoration data before the first replacement.
        _write(receipt, _encoded(transaction))
        for record in records:
            path = Path(record["path"])
            before, metadata = _read(path)
            if (
                before != base64.b64decode(record["before"])
                or _file_identity(metadata) != record["identity"]
            ):
                raise ConfigError(
                    "project bootstrap: configuration changed before replacement"
                )
            _write(path, *files[path])
            written.append(record)
        proof = _probe(launcher, project, args.system1)
        report["probe"] = proof
        report["system1"]["ready"] = proof["system1"]
        _write(receipt, _encoded(transaction))
    except BaseException:
        # Retain a receipt if a concurrent writer prevents safe restoration.
        transaction["files"] = written
        _write(receipt, _encoded(transaction))
        _restore(receipt, root, failed=True)
        raise
    print(json.dumps(report))
