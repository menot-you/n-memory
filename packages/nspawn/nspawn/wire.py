"""Tolerant shapes for the Codex app-server JSONL protocol.

the client identity, the ceilings the wire re-exports, the two policy enums the
durable registry stores, and the JSON-RPC frame shapes.

The installed protocol is broader than this slice. Unknown fields and methods are
RETAINED rather than rejected, so the broker does not turn brittle at a
forward-compatible boundary; that tolerance is the frame's contract, not laxity.
"""

import sys
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from . import limits
from .errors import NSpawnError

#: The PATH a sandboxed child is given, with Homebrew in front on macOS: there
#: `/usr/bin` holds only Apple's shims for git and python3.
SANDBOX_TOOL_PATH = (
    "/opt/homebrew/bin:/usr/bin:/bin" if sys.platform == "darwin" else "/usr/bin:/bin"
)

CLIENT_NAME = "nott-nspawn"
CLIENT_TITLE = "nSPAWN Codex control plane"
#: The oracle takes this from its own crate version at compile time; the contractned
#: `capabilities/nspawn/Cargo.toml` says 0.1.0.
CLIENT_VERSION = "0.1.0"

OPT_OUT_NOTIFICATIONS = ("item/agentMessage/delta", "item/reasoning/summaryTextDelta")

MAX_FILES_PER_ITEM = limits.MAX_FILES_PER_ITEM
MAX_IDENTIFIER_BYTES = limits.MAX_DURABLE_IDENTIFIER_BYTES
MAX_METHOD_BYTES = limits.MAX_METHOD_BYTES


#: The capability features the child MUST report disabled. The list is the
#: oracle's, in the oracle's order.
DISABLED_CAPABILITY_FEATURES = (
    "apps",
    "apps_mcp_path_override",
    "enable_mcp_apps",
    "hooks",
    "plugin_hooks",
    "plugin_sharing",
    "plugins",
    "remote_plugin",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "in_app_browser",
    "computer_use",
    "image_generation",
    "multi_agent",
    "multi_agent_mode",
    "multi_agent_v2",
    "enable_fanout",
    "memories",
    "external_agent_memory_import",
    "chronicle",
    "goals",
    "token_budget",
    "remote_control",
    "remote_models",
    "remote_compaction_v2",
    "deferred_executor",
    "auth_elicitation",
    "guardian_approval",
    "exec_permission_approvals",
    "request_permissions_tool",
    "tool_call_mcp_elicitation",
    "skill_mcp_dependency_install",
    "workspace_dependencies",
    "skill_search",
    "tool_search",
    "tool_suggest",
    "non_prefixed_mcp_tool_names",
    "code_mode_only",
    "artifact",
    "network_proxy",
    "respect_system_proxy",
    "search_tool",
    "standalone_web_search",
    "web_search_cached",
    "web_search_request",
    "realtime_conversation",
    "responses_websockets",
    "responses_websockets_v2",
    "executor_capability_discovery",
    "prevent_idle_sleep",
    "shell_snapshot",
    "secret_auth_storage",
    "use_agent_identity",
    "default_mode_request_user_input",
)


class ExecutorSandbox(Enum):
    """What the executor may write. `read-only` is the default."""

    READ_ONLY = "read-only"
    WORKSPACE_WRITE = "workspace-write"

    @classmethod
    def default(cls) -> ExecutorSandbox:
        return cls.READ_ONLY

    def as_str(self) -> str:
        return self.value

    @classmethod
    def parse(cls, value: str) -> ExecutorSandbox | None:
        """Read a sandbox back, answering None for anything outside the set."""
        try:
            return cls(value)
        except ValueError:
            return None


class ApprovalPolicy(Enum):
    """When the executor may ask. `never` is the default."""

    UNTRUSTED = "untrusted"
    ON_REQUEST = "on-request"
    NEVER = "never"

    @classmethod
    def default(cls) -> ApprovalPolicy:
        return cls.NEVER

    def as_str(self) -> str:
        return self.value

    @classmethod
    def parse(cls, value: str) -> ApprovalPolicy | None:
        try:
            return cls(value)
        except ValueError:
            return None


class WireError(NSpawnError):
    """A frame this port refuses to classify."""

    def __init__(self, message: str) -> None:
        self.message = message
        Exception.__init__(self, message)


@dataclass(frozen=True)
class RpcErrorObject:
    """The JSON-RPC error member."""

    code: int
    message: str
    data: Any | None = None

    def to_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            wire["data"] = self.data
        return wire

    @classmethod
    def from_wire(cls, raw: dict[str, Any]) -> RpcErrorObject:
        """Read an error member, REFUSING one that does not carry its two fields."""
        try:
            return cls(code=raw["code"], message=raw["message"], data=raw.get("data"))
        except (KeyError, TypeError) as error:
            raise WireError(f"JSON-RPC error member is malformed: {error}") from error


#: Sentinel for a frame member that is ABSENT, which is not the same as a member
#: present and null. `result: null` is a real answer; a missing `result` is not.
_ABSENT = object()


@dataclass
class RpcFrame:
    """One JSON-RPC frame, tolerant of everything this slice does not name.

    `result` distinguishes ABSENT from `null`: the oracle deserializes it through
    a helper that keeps a present null as `Some(Value::Null)`, because a turn that
    answered `null` did answer.
    """

    id: Any | None = None
    method: str | None = None
    params: Any | None = None
    error: RpcErrorObject | None = None
    jsonrpc: str | None = None
    _result: Any = field(default=_ABSENT, repr=False)

    @property
    def has_result(self) -> bool:
        """True when the frame CARRIES a result member, null included."""
        return self._result is not _ABSENT

    @property
    def result(self) -> Any:
        """The result member. Raises when the frame carries none."""
        if self._result is _ABSENT:
            raise AttributeError("this frame carries no result member")
        return self._result

    def with_result(self, value: Any) -> RpcFrame:
        """Return this frame carrying `value` as its result, null included."""
        self._result = value
        return self

    @classmethod
    def from_wire(cls, raw: Any) -> RpcFrame:
        """Read a frame, keeping a present-but-null result present.

        A frame that is syntactically valid JSON but is not an OBJECT — a list, a
        string, a number — is REFUSED here with a typed error. It reached this
        method as a decoded value, and letting it through would raise an
        AttributeError somewhere further in, where nothing is prepared to close
        the session.
        """
        if not isinstance(raw, dict):
            raise WireError(f"JSON-RPC frame is {type(raw).__name__}, not an object")
        error = raw.get("error")
        if error is not None and not isinstance(error, dict):
            raise WireError(
                f"JSON-RPC error member is {type(error).__name__}, not an object"
            )
        frame = cls(
            id=raw.get("id"),
            method=raw.get("method"),
            params=raw.get("params"),
            error=RpcErrorObject.from_wire(error) if error is not None else None,
            jsonrpc=raw.get("jsonrpc"),
        )
        if "result" in raw:
            frame.with_result(raw["result"])
        return frame

    def to_wire(self) -> dict[str, Any]:
        """Emit the frame, omitting every absent member the way the oracle does."""
        wire: dict[str, Any] = {}
        if self.id is not None:
            wire["id"] = self.id
        if self.method is not None:
            wire["method"] = self.method
        if self.params is not None:
            wire["params"] = self.params
        if self.has_result:
            wire["result"] = self._result
        if self.error is not None:
            wire["error"] = self.error.to_wire()
        if self.jsonrpc is not None:
            wire["jsonrpc"] = self.jsonrpc
        return wire


def request(id: int, method: str, params: Any) -> RpcFrame:
    """A request frame carrying params."""
    return RpcFrame(id=id, method=method, params=params)


def notification(method: str, params: Any) -> RpcFrame:
    """A notification: a method message with no id."""
    return RpcFrame(method=method, params=params)


def response(id: Any, result: Any) -> RpcFrame:
    """A response frame. A null `result` is still an answer."""
    return RpcFrame(id=id).with_result(result)


def error_response(id: Any, error: RpcErrorObject) -> RpcFrame:
    """An error response frame."""
    return RpcFrame(id=id, error=error)


def is_response(frame: RpcFrame) -> bool:
    """A frame answering a request: it has an id AND a result or an error."""
    return frame.id is not None and (frame.has_result or frame.error is not None)


def bounded_identifier(value: str | None) -> str | None:
    """An identifier the durable side can hold, or None.

    Empty is None and over-long is None — an identifier that cannot be stored is
    ABSENT rather than truncated, because a truncated id names a different thing.
    """
    if value is None or value == "":
        return None
    if len(value.encode("utf-8")) > MAX_IDENTIFIER_BYTES:
        return None
    return value


def _identifier_field(params: Any, key: str) -> str | None:
    if not isinstance(params, dict):
        return None
    value = params.get(key)
    return bounded_identifier(value) if isinstance(value, str) else None


def _raw_turn_id(params: Any) -> Any:
    """`turn.id` when PRESENT, else top-level `turnId`.

    Selection happens BEFORE string conversion, so a present-but-non-string
    `turn.id` stays a miss instead of silently falling back to `turnId`.
    """
    if not isinstance(params, dict):
        return None
    turn = params.get("turn")
    if isinstance(turn, dict) and "id" in turn:
        return turn["id"]
    return params.get("turnId")


def _turn_id(params: Any) -> str | None:
    raw = _raw_turn_id(params)
    return bounded_identifier(raw) if isinstance(raw, str) else None


class IncomingKind(Enum):
    """What an inbound frame turned out to be."""

    RESPONSE = "response"
    NOTIFICATION = "notification"
    REQUEST = "request"


@dataclass(frozen=True)
class IncomingFrame:
    """A frame read from the child, classified.

    A response carries either a result or an error, never both and never neither.
    """

    kind: IncomingKind
    id: Any = None
    method: str | None = None
    params: Any = None
    result: Any = None
    error: RpcErrorObject | None = None


def classify(frame: RpcFrame) -> IncomingFrame:
    """Classify an inbound frame, or REFUSE it.

    An unbounded method or an id that is not a bounded scalar is refused rather
    than carried: both cross into durable state further down.
    """
    if is_response(frame):
        if frame.id is None:
            raise WireError("response has no id")
        if frame.error is not None:
            return IncomingFrame(
                kind=IncomingKind.RESPONSE, id=frame.id, error=frame.error
            )
        return IncomingFrame(
            kind=IncomingKind.RESPONSE,
            id=frame.id,
            result=frame.result if frame.has_result else None,
        )
    if frame.method is not None:
        if len(frame.method.encode("utf-8")) > MAX_METHOD_BYTES:
            raise WireError(f"JSON-RPC method exceeds {MAX_METHOD_BYTES} bytes")
        params = frame.params if frame.params is not None else {}
        if frame.id is not None:
            valid_id = (
                isinstance(frame.id, int) and not isinstance(frame.id, bool)
            ) or (
                isinstance(frame.id, str)
                and len(frame.id.encode("utf-8")) <= MAX_IDENTIFIER_BYTES
            )
            if not valid_id:
                raise WireError("JSON-RPC request id is not a bounded scalar")
            return IncomingFrame(
                kind=IncomingKind.REQUEST,
                id=frame.id,
                method=frame.method,
                params=params,
            )
        return IncomingFrame(
            kind=IncomingKind.NOTIFICATION, method=frame.method, params=params
        )
    raise WireError("JSONL frame is neither response nor method message")


class TurnCompletionStatus:
    """How a turn ended. An unrecognised or ABSENT status is `unknown`.

    A missing status is `unknown("missing")` and never `completed`: a turn whose
    end nobody reported did not report success.
    """

    __slots__ = ("_value",)

    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    FAILED = "failed"

    def __init__(self, value: str) -> None:
        self._value = value

    @classmethod
    def parse(cls, value: str | None) -> TurnCompletionStatus:
        if value is None:
            return cls("missing")
        return cls(value)

    def as_str(self) -> str:
        return self._value

    def is_known(self) -> bool:
        return self._value in (self.COMPLETED, self.INTERRUPTED, self.FAILED)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, TurnCompletionStatus):
            return self._value == other._value
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._value)

    def __repr__(self) -> str:
        return f"TurnCompletionStatus({self._value!r})"


class NotificationKind(Enum):
    """The notifications this slice names. Everything else is `OTHER`."""

    THREAD_STARTED = "thread_started"
    TURN_STARTED = "turn_started"
    TURN_COMPLETED = "turn_completed"
    ITEM_STARTED = "item_started"
    ITEM_COMPLETED = "item_completed"
    OUTPUT_DELTA = "output_delta"
    FILE_CHANGE = "file_change"
    TOKEN_USAGE = "token_usage"
    ERROR = "error"
    WARNING = "warning"
    OTHER = "other"


@dataclass(frozen=True)
class Notification:
    """One parsed notification.

    A method this slice does not name is retained as `OTHER` with its params
    intact, never dropped: the installed protocol is broader than this port.
    """

    kind: NotificationKind
    method: str
    params: Any
    thread_id: str | None = None
    turn_id: str | None = None
    item_id: str | None = None
    item: Any = None
    delta: str = ""
    message: str = ""
    will_retry: bool = False
    status: TurnCompletionStatus | None = None


DEFAULT_ERROR_MESSAGE = "Codex app-server error"
DEFAULT_WARNING_MESSAGE = "Codex app-server warning"


def parse_notification(method: str, params: Any) -> Notification:
    """Parse a notification by method, retaining anything this slice cannot name."""
    common = {"method": method, "params": params}
    if method == "thread/started":
        thread = params.get("thread") if isinstance(params, dict) else None
        raw = thread.get("id") if isinstance(thread, dict) else None
        return Notification(
            kind=NotificationKind.THREAD_STARTED,
            thread_id=bounded_identifier(raw) if isinstance(raw, str) else None,
            **common,
        )
    if method == "turn/started":
        return Notification(
            kind=NotificationKind.TURN_STARTED,
            turn_id=_turn_id(params),
            thread_id=_identifier_field(params, "threadId"),
            **common,
        )
    if method == "turn/completed":
        turn = params.get("turn") if isinstance(params, dict) else None
        raw_status = turn.get("status") if isinstance(turn, dict) else None
        if raw_status is None and isinstance(params, dict):
            raw_status = params.get("status")
        return Notification(
            kind=NotificationKind.TURN_COMPLETED,
            turn_id=_turn_id(params),
            thread_id=_identifier_field(params, "threadId"),
            status=TurnCompletionStatus.parse(
                raw_status if isinstance(raw_status, str) else None
            ),
            **common,
        )
    if method in ("item/started", "item/completed"):
        item = params.get("item") if isinstance(params, dict) else None
        return Notification(
            kind=NotificationKind.ITEM_STARTED
            if method == "item/started"
            else NotificationKind.ITEM_COMPLETED,
            item=item if item is not None else params,
            thread_id=_identifier_field(params, "threadId"),
            turn_id=_identifier_field(params, "turnId"),
            **common,
        )
    if method in ("item/commandExecution/outputDelta", "item/fileChange/outputDelta"):
        delta = params.get("delta") if isinstance(params, dict) else None
        return Notification(
            kind=NotificationKind.OUTPUT_DELTA,
            item_id=_identifier_field(params, "itemId"),
            thread_id=_identifier_field(params, "threadId"),
            turn_id=_identifier_field(params, "turnId"),
            delta=delta if isinstance(delta, str) else "",
            **common,
        )
    if method == "item/fileChange/patchUpdated":
        return Notification(
            kind=NotificationKind.FILE_CHANGE,
            item_id=_identifier_field(params, "itemId"),
            thread_id=_identifier_field(params, "threadId"),
            turn_id=_identifier_field(params, "turnId"),
            item=params,
            **common,
        )
    if method == "thread/tokenUsage/updated":
        return Notification(
            kind=NotificationKind.TOKEN_USAGE,
            thread_id=_identifier_field(params, "threadId"),
            turn_id=_identifier_field(params, "turnId"),
            **common,
        )
    if method == "error":
        error = params.get("error") if isinstance(params, dict) else None
        raw = error.get("message") if isinstance(error, dict) else None
        if not isinstance(raw, str) and isinstance(params, dict):
            raw = params.get("message")
        will_retry = params.get("willRetry") if isinstance(params, dict) else None
        return Notification(
            kind=NotificationKind.ERROR,
            message=raw if isinstance(raw, str) else DEFAULT_ERROR_MESSAGE,
            thread_id=_identifier_field(params, "threadId"),
            turn_id=_identifier_field(params, "turnId"),
            will_retry=will_retry if isinstance(will_retry, bool) else False,
            **common,
        )
    if method == "warning":
        raw = params.get("message") if isinstance(params, dict) else None
        return Notification(
            kind=NotificationKind.WARNING,
            message=raw if isinstance(raw, str) else DEFAULT_WARNING_MESSAGE,
            thread_id=_identifier_field(params, "threadId"),
            **common,
        )
    return Notification(kind=NotificationKind.OTHER, **common)


# --- params the client sends -------------------------------------------------


def initialize_params() -> dict[str, Any]:
    """The handshake this client opens with, opting OUT of the delta firehose."""
    return {
        "clientInfo": {
            "name": CLIENT_NAME,
            "title": CLIENT_TITLE,
            "version": CLIENT_VERSION,
        },
        "capabilities": {
            "experimentalApi": True,
            "requestAttestation": False,
            "mcpServerOpenaiFormElicitation": False,
            "optOutNotificationMethods": list(OPT_OUT_NOTIFICATIONS),
        },
    }


def initialized_notification() -> RpcFrame:
    """The `initialized` notification that closes the handshake."""
    return notification("initialized", {})


def thread_start_params(
    model: str, cwd: str, approval_policy: ApprovalPolicy, sandbox: ExecutorSandbox
) -> dict[str, Any]:
    """The thread this client opens: no network, no web search, no ambient tools.

    A workspace-write sandbox names its ONE writable root explicitly and excludes
    the temp directories, so "writable" never means "writable anywhere".
    """
    params: dict[str, Any] = {
        "model": model,
        "cwd": cwd,
        "approvalPolicy": approval_policy.as_str(),
        "approvalsReviewer": "user",
        "sandbox": sandbox.as_str(),
        "allowProviderModelFallback": False,
        "experimentalRawEvents": False,
        "environments": [],
        "dynamicTools": [],
        "selectedCapabilityRoots": [],
        "config": {
            "sandbox_workspace_write.network_access": False,
            "tools.web_search": False,
            "web_search": "disabled",
            "notify": [],
            "analytics.enabled": False,
            "orchestrator.mcp.enabled": False,
            "orchestrator.skills.enabled": False,
            "shell_environment_policy.inherit": "none",
            "shell_environment_policy.set.PATH": SANDBOX_TOOL_PATH,
        },
    }
    if sandbox is ExecutorSandbox.WORKSPACE_WRITE:
        params["config"]["sandbox_workspace_write.writable_roots"] = [cwd]
        params["config"]["sandbox_workspace_write.exclude_tmpdir_env_var"] = True
        params["config"]["sandbox_workspace_write.exclude_slash_tmp"] = True
    return params


def turn_start_params(
    thread: str, text: str, cwd: str | None = None, model: str | None = None
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "threadId": thread,
        "input": [{"type": "text", "text": text}],
    }
    if cwd is not None:
        params["cwd"] = cwd
    if model is not None:
        params["model"] = model
    return params


def _nested_or_top_id(result: Any, container: str) -> str | None:
    if not isinstance(result, dict):
        return None
    nested = result.get(container)
    raw = nested.get("id") if isinstance(nested, dict) else None
    if not isinstance(raw, str):
        raw = result.get("id")
    return bounded_identifier(raw) if isinstance(raw, str) else None


def thread_id(result: Any) -> str | None:
    """`thread.id`, else top-level `id`, bounded."""
    return _nested_or_top_id(result, "thread")


def turn_id(result: Any) -> str | None:
    """`turn.id`, else top-level `id`, bounded."""
    return _nested_or_top_id(result, "turn")
