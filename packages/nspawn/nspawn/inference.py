"""The closed, tool-free native structured-inference request."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from jsonschema.exceptions import (  # type: ignore[import-untyped]
    SchemaError,
    ValidationError,
)

from .limits import MAX_DURABLE_ROW_BYTES
from .pump import MAX_RUN_OUTPUT_BYTES
from .wire import DISABLED_CAPABILITY_FEATURES

# These are the installed native capability selections measured for the
# tool-free Default turn. The original model descriptor supplies every other
# field; no model alias, account or reasoning-strength rating is invented here.
_CODEX_MODEL_CLOSURE: dict[str, Any] = {
    "tool_mode": "direct",
    "multi_agent_version": None,
    "node_repl_disabled": True,
    "apply_patch_tool_type": None,
    "experimental_supported_tools": [],
}
_CODEX_CLOSED_FEATURES = (
    *DISABLED_CAPABILITY_FEATURES,
    "api_key_model_discovery",
    "default_mode_request_user_input",
    "shell_tool",
    "view_image",
    "sleep_tool",
    "code_mode",
    "code_mode_host",
    "unified_exec",
)


def _native_file(path: Path, home: Path) -> bytes:
    """Read complete bounded evidence through the selected owner's home only."""
    try:
        parts = path.relative_to(home).parts
        if not parts or any(part in {".", ".."} for part in parts):
            raise ValueError
        directory = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in parts[:-1]:
                metadata = os.fstat(directory)
                if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022:
                    raise ValueError
                child = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=directory,
                )
                os.close(directory)
                directory = child
            metadata = os.fstat(directory)
            if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022:
                raise ValueError
            descriptor = os.open(
                parts[-1],
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=directory,
            )
        finally:
            os.close(directory)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.geteuid()
                or before.st_mode & 0o022
                or before.st_nlink != 1
                or not 0 < before.st_size <= MAX_RUN_OUTPUT_BYTES
            ):
                raise ValueError
            data = stream.read(before.st_size + 1)
            fields = (
                "st_dev",
                "st_ino",
                "st_mode",
                "st_uid",
                "st_nlink",
                "st_size",
                "st_mtime_ns",
                "st_ctime_ns",
            )
            for after in (os.fstat(stream.fileno()), path.lstat()):
                if any(getattr(before, key) != getattr(after, key) for key in fields):
                    raise ValueError
            if len(data) != before.st_size:
                raise ValueError
            return data
    except OSError, ValueError:
        raise ValueError("native inference evidence is unavailable or unsafe") from None


@dataclass(frozen=True, slots=True)
class CodexInference:
    """One internal composition, bound to the original home and this run."""

    home: Path
    root: Path
    model: str
    catalog: str
    closed_servers: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            any(
                not path.is_absolute() or str(path) != os.path.normpath(path)
                for path in (self.home, self.root)
            )
            or not self.model
        ):
            raise ValueError(
                "native inference composition requires absolute homes and a model"
            )
        try:
            models = json.loads(self.catalog)["models"]
            json.dumps(models, allow_nan=False)
            if (
                len(models) != 1
                or models[0].get("slug") != self.model
                or any(
                    models[0].get(key) != value
                    for key, value in _CODEX_MODEL_CLOSURE.items()
                )
            ):
                raise ValueError
        except ValueError, TypeError, KeyError, AttributeError, RecursionError:
            raise ValueError(
                "native inference model capabilities are not closed"
            ) from None

    @classmethod
    def prepare(
        cls, home: Path, root: Path, model: str, servers: tuple[str, ...]
    ) -> CodexInference:
        try:
            cache = json.loads(_native_file(home / "models_cache.json", home))
            models = cache["models"]
            if not isinstance(models, list):
                raise TypeError
            selected = [
                item
                for item in models
                if isinstance(item, dict) and item.get("slug") == model
            ]
            if (
                len(selected) != 1
                or not set(_CODEX_MODEL_CLOSURE) <= selected[0].keys()
            ):
                raise ValueError
            descriptor = {**selected[0], **_CODEX_MODEL_CLOSURE}
            catalog = json.dumps({"models": [descriptor]}, allow_nan=False)
        except ValueError, TypeError, KeyError, RecursionError:
            raise ValueError(
                "native inference requires its original model descriptor"
            ) from None
        return cls(home, root, model, catalog, servers)

    @property
    def cwd(self) -> Path:
        return self.root / "neutral"

    def config(self) -> dict[str, Any]:
        return {
            **{f"features.{key}": False for key in _CODEX_CLOSED_FEATURES},
            "model_catalog_json": str(self.root / "catalog.json"),
            "web_search": "disabled",
            "notify": [],
            "plugins": {},
            "orchestrator.mcp.enabled": False,
            "orchestrator.skills.enabled": False,
            "shell_environment_policy.inherit": "none",
            "project_doc_max_bytes": 0,
            "project_doc_fallback_filenames": [],
            "developer_instructions": "",
            "mcp_servers": {name: {"enabled": False} for name in self.closed_servers},
        }

    def materialize(self) -> None:
        # The owning run directory already exists; these leaves never replace an
        # installed file or a native credential/history home.
        for directory in (self.root, self.cwd):
            metadata = directory.lstat()
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or metadata.st_mode & 0o077
            ):
                raise ValueError("native inference run directories are not private")
        with (self.root / "catalog.json").open("x") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(self.catalog)

    def config_refusal(self, result: object) -> str | None:
        """The original native config view, privately read before thread/start."""
        if not isinstance(result, dict) or not isinstance(result.get("layers"), list):
            return "native inference configuration lacks its layers"
        overrides = []
        for layer in result["layers"]:
            if not isinstance(layer, dict) or not isinstance(layer.get("name"), dict):
                return "native inference configuration layer is malformed"
            kind = layer["name"].get("type")
            if kind not in {"packagedDefaults", "system", "user", "sessionFlags"}:
                return "native inference loaded an inadmissible configuration layer"
            config = layer.get("config")
            if not isinstance(config, dict):
                return "native inference configuration layer has no object"
            if any(key.endswith("instructions_file") and config[key] for key in config):
                return "native inference loaded an ambient instructions file"
            if kind == "sessionFlags":
                overrides.append(config)
        if len(overrides) != 1:
            return "native inference configuration lacks its exact startup overrides"
        for key, expected in self.config().items():
            value: Any = overrides[0]
            for part in key.split("."):
                value = value.get(part) if isinstance(value, dict) else None
            if key == "mcp_servers":
                if not isinstance(value, dict) or set(value) != set(expected):
                    return "native inference startup MCP declaration differs"
                if any(
                    not isinstance(server, dict) or server.get("enabled") is not False
                    for server in value.values()
                ):
                    return "native inference leaves an inherited MCP enabled"
            elif value != expected or type(value) is not type(expected):
                return "native inference startup capability closure differs"
        effective = result.get("config")
        if (
            not isinstance(effective, dict)
            or effective.get("developer_instructions") not in (None, "")
            or effective.get("instructions") not in (None, "")
            or effective.get("web_search") != "disabled"
        ):
            return "native inference effective instructions or web capability differs"
        # Raw layers also state MCP names omitted by the typed Config projection.
        for layer in result["layers"]:
            servers = layer["config"].get("mcp_servers", {})
            if not isinstance(servers, dict) or set(servers) - set(self.closed_servers):
                return "native inference configuration has an undeclared MCP"
        return None

    def verify_history(
        self, path: object, thread: str, turn: str, effort: str | None, answer: str
    ) -> None:
        """A blocked native function call remains a refused result."""
        if not isinstance(path, str) or not Path(path).is_absolute():
            raise ValueError("native inference history path is missing")
        history = Path(path)
        if not history.is_relative_to(self.home / "sessions"):
            raise ValueError("native inference history is outside its original home")
        data = _native_file(history, self.home)
        if not data.endswith(b"\n"):
            raise ValueError("native inference history is incomplete")
        started = context = ended = False
        for index, line in enumerate(data.splitlines()):
            try:
                record = json.loads(line)
            except ValueError, RecursionError:
                raise ValueError("native inference history is malformed") from None
            if not isinstance(record, dict) or not isinstance(
                record.get("payload"), dict
            ):
                raise TypeError("native inference history is malformed")
            kind, payload = record.get("type"), record["payload"]
            if index == 0:
                if kind != "session_meta" or payload.get("id") != thread:
                    raise ValueError("native inference history thread differs")
                continue
            if kind == "response_item":
                if payload.get("type") not in {"message", "reasoning"} or (
                    payload.get("type") == "message"
                    and payload.get("role")
                    not in {"user", "developer", "assistant", "system"}
                ):
                    raise ValueError(
                        "native inference requested forbidden tool authority"
                    )
            elif kind == "event_msg":
                event = payload.get("type")
                if event == "task_started" and payload.get("turn_id") == turn:
                    if started or payload.get("collaboration_mode_kind") != "default":
                        raise ValueError(
                            "native inference history mode or turn differs"
                        )
                    started = True
                elif event == "task_complete" and payload.get("turn_id") == turn:
                    if (
                        not started
                        or not context
                        or ended
                        or payload.get("last_agent_message") != answer
                    ):
                        raise ValueError("native inference history completion differs")
                    ended = True
                elif event == "item_completed":
                    item = payload.get("item")
                    if not isinstance(item, dict) or item.get("type") not in {
                        "UserMessage",
                        "AgentMessage",
                        "Reasoning",
                    }:
                        raise ValueError(
                            "native inference requested forbidden tool authority"
                        )
                elif event not in {
                    "task_started",
                    "task_complete",
                    "token_count",
                    "agent_message",
                    "user_message",
                    "agent_reasoning",
                    "agent_reasoning_raw_content",
                }:
                    raise ValueError(
                        "native inference history contains unknown authority"
                    )
            elif kind == "turn_context":
                if payload.get("turn_id") == turn:
                    if (
                        not started
                        or context
                        or ended
                        or (
                            payload.get("cwd") != str(self.cwd)
                            or payload.get("model") != self.model
                            or payload.get("effort") != effort
                            or payload.get("collaboration_mode")
                            != {
                                "mode": "default",
                                "settings": {
                                    "model": self.model,
                                    "reasoning_effort": effort,
                                    "developer_instructions": None,
                                },
                            }
                        )
                    ):
                        raise ValueError("native inference history selection differs")
                    context = True
            elif kind not in {"world_state", "token_usage_record"}:
                raise ValueError("native inference history contains unknown authority")
        if not started or not context or not ended:
            raise ValueError("native inference history lacks its complete turn")


@dataclass(frozen=True, slots=True)
class StructuredInference:
    """Canonical immutable request bytes, never vendor arguments or environment."""

    encoded: str

    def __post_init__(self) -> None:
        if len(self.encoded.encode()) > MAX_DURABLE_ROW_BYTES:
            raise ValueError("inference_profile exceeds the durable request bound")
        try:
            profile = json.loads(self.encoded)
        except ValueError, RecursionError:
            raise ValueError("inference_profile must be JSON") from None
        if (
            not isinstance(profile, dict)
            or set(profile) != {"tools", "output_schema"}
            or profile["tools"] != "none"
            or not isinstance(profile["output_schema"], dict)
        ):
            raise ValueError("inference_profile requires tools=none and output_schema")
        try:
            Draft202012Validator.check_schema(profile["output_schema"])
        except SchemaError, RecursionError:
            raise ValueError("inference_profile.output_schema is invalid") from None

    @classmethod
    def from_request(cls, profile: dict[str, Any]) -> StructuredInference:
        try:
            encoded = json.dumps(
                profile, sort_keys=True, separators=(",", ":"), allow_nan=False
            )
        except ValueError, TypeError, RecursionError:
            raise ValueError("inference_profile must contain finite JSON") from None
        return cls(encoded)

    def data(self) -> dict[str, Any]:
        result: dict[str, Any] = json.loads(self.encoded)
        return result

    def schema(self) -> str:
        return json.dumps(
            self.data()["output_schema"], sort_keys=True, separators=(",", ":")
        )

    def parse_output(self, text: str) -> Any:
        """Validate the complete native answer, without persisting parser details."""
        if len(text.encode()) > MAX_RUN_OUTPUT_BYTES:
            raise ValueError("structured output exceeds the native result bound")
        try:
            value = json.loads(text)
            json.dumps(value, allow_nan=False)
            Draft202012Validator(self.data()["output_schema"]).validate(value)
        except ValueError, TypeError, RecursionError, ValidationError:
            raise ValueError(
                "structured output does not match its finite JSON schema"
            ) from None
        return value


@dataclass(frozen=True, slots=True)
class InferenceHome:
    """The original native history location, never a copied credential home."""

    home: str
    config_dir: str | None

    def __post_init__(self) -> None:
        if not Path(self.home).is_absolute() or (
            self.config_dir is not None and not Path(self.config_dir).is_absolute()
        ):
            raise ValueError("structured inference requires absolute native homes")

    @classmethod
    def from_record(cls, record: object) -> InferenceHome:
        if not isinstance(record, dict) or set(record) != {"HOME", "CLAUDE_CONFIG_DIR"}:
            raise ValueError("structured inference carries an invalid home record")
        home, config_dir = record["HOME"], record["CLAUDE_CONFIG_DIR"]
        if not isinstance(home, str) or (
            config_dir is not None and not isinstance(config_dir, str)
        ):
            raise ValueError("structured inference carries invalid native homes")
        return cls(home, config_dir)

    @classmethod
    def from_environment(cls, env: Mapping[str, str]) -> InferenceHome:
        return cls.from_record(
            {"HOME": env.get("HOME"), "CLAUDE_CONFIG_DIR": env.get("CLAUDE_CONFIG_DIR")}
        )

    def data(self) -> dict[str, str | None]:
        return {"HOME": self.home, "CLAUDE_CONFIG_DIR": self.config_dir}
