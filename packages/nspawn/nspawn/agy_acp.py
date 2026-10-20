"""The native AGY route as an ACP agent on stdio, for the NOTT node host.

`python -m nspawn.agy_acp` maps ACP `session/new`, `session/prompt`,
`session/cancel` and `session/set_model` onto one `AgyTurn` per prompt, launched
by `prepare_native` exactly as an nSPAWN run on the roster's agy account is: the
account's own OAuth home, never an API key. A turn resumes the conversation the
previous turn initialized; its reply streams as `agent_message_chunk`s, and the
prompt's answer carries the turn's usage.
"""

from __future__ import annotations

import json
import os
import pwd
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import agy, native
from .errors import NSpawnError
from .owner_roster import Provider, Roster
from .redact import SecretNeedles, overlap
from .registry import STORE_ERRORS, Registry, store_path

#: AGY's usage counters and the names ACP's prompt usage gives them.
USAGE = (
    ("input_tokens", "inputTokens"),
    ("output_tokens", "outputTokens"),
    ("thinking_tokens", "thoughtTokens"),
    ("cache_read_tokens", "cachedReadTokens"),
    ("total_tokens", "totalTokens"),
)
#: Characters per `agent_message_chunk`: JSON escapes one to 12 bytes at most,
#: so a chunk stays under the host's 1 MiB frame.
CHUNK = 65536
#: The plugin a session's role, skills and MCP servers reach AGY as.
PLUGIN = "nott-session"
#: AGY truncates a rule file past this (its rules.md); a role is never cut.
RULES_BYTES = 24_000
#: The node bridge's door: the one host socket a caged turn is shown.
DOOR = "NOTT_NODE_DOOR"
#: The bridge's caller binding, redacted from everything the turn says.
BINDING = "NOTT_NODE_BINDING"


@dataclass
class Session:
    cwd: Path
    model: str
    conversation: str | None = None
    #: The ACP session's role, skill root and MCP servers (AGY's own shape).
    rules: str | None = None
    skills: Path | None = None
    servers: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: AGY counts usage per conversation: its count after the last turn.
    counted: dict[str, int] = field(default_factory=dict)
    turn: agy.AgyTurn | None = None
    cancelled: bool = False
    busy: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)


class Refused(Exception):
    """A request this agent answers with a JSON-RPC error."""


#: The agy account the node host recorded for this node; every turn runs on it.
#: Unset, `prepare_native` picks the account as it does for an nSPAWN run.
ACCOUNT_ENV = "NOTT_NSPAWN_AGY_ACCOUNT"


def roster_provider() -> tuple[Roster, Provider]:
    path = os.environ.get("NOTT_NSPAWN_ROSTER") or str(
        Path.home() / ".nott" / "spawn.toml"
    )
    roster = Roster.read(Path(path))
    provider = next((p for p in roster.providers if p.kind == "agy"), None)
    if provider is None or provider.model is None:
        raise Refused("roster declares no agy provider with a model")
    return roster, provider


def words(block: Any) -> str:
    """One ACP prompt block as the words AGY reads: a link and an embedded file
    as claude-agent-acp words them, and anything AGY cannot read refused."""
    match block:
        case {"type": "text", "text": str(text)}:
            return text
        case {"type": "resource_link", "uri": str(uri), "name": str(name)}:
            return f"[@{name}]({uri})"
        case {"type": "resource", "resource": {"uri": str(uri), "text": str(text)}}:
            return f'<context ref="{uri}">\n{text}\n</context>'
        case {"type": str(kind)}:
            raise Refused(f"AGY takes no {kind} content")
        case _:
            raise Refused("prompt block is not an ACP content block")


def customization(params: dict[str, Any]) -> dict[str, Any]:
    """`session/new`'s role (`_meta.rules`), skill root (`_meta.skills`) and stdio
    `mcpServers` as `Session` fields; anything AGY cannot take whole is refused."""
    meta = params.get("_meta")
    if meta is None:
        meta = {}
    if not isinstance(meta, dict):
        raise Refused("session/new _meta is not an object")
    rules, skills = meta.get("rules"), meta.get("skills")
    if rules is not None and (
        not isinstance(rules, str) or len(rules.encode()) > RULES_BYTES
    ):
        raise Refused(f"a session role is text of at most {RULES_BYTES} bytes")
    if skills is not None and (
        not isinstance(skills, str)
        or not Path(skills).is_absolute()
        or not Path(skills).is_dir()
        or Path(skills).resolve() != Path(skills)
    ):
        raise Refused("a session skill root is an existing canonical directory")
    servers: dict[str, dict[str, Any]] = {}
    for server in params.get("mcpServers") or []:
        match server:
            case {
                "name": str(name),
                "command": str(command),
                "args": list(args),
                "env": list(env),
            } if server.get("type", "stdio") == "stdio" and name not in servers:
                if not all(isinstance(arg, str) for arg in args) or not all(
                    isinstance(item, dict)
                    and isinstance(item.get("name"), str)
                    and isinstance(item.get("value"), str)
                    for item in env
                ):
                    raise Refused(f"MCP server {name} is malformed")
                servers[name] = {
                    "command": command,
                    "args": args,
                    "env": {item["name"]: item["value"] for item in env},
                }
            case _:
                raise Refused("AGY takes uniquely named stdio MCP servers only")
    return {
        "rules": rules,
        "skills": None if skills is None else Path(skills),
        "servers": servers,
    }


def stage(found: Session, root: Path) -> Path | None:
    """The session's inputs as one AGY plugin in the private `root`, laid out as
    the `~/.gemini` it overlays; None when the session has none. `root`
    is never granted to the cage, which sees it only through the overlay."""
    if found.rules is None and found.skills is None and not found.servers:
        return None
    plugin = root / "config/plugins" / PLUGIN
    plugin.mkdir(parents=True)
    (plugin / "plugin.json").write_text(json.dumps({"name": PLUGIN}))
    if found.rules is not None:
        (plugin / "rules").mkdir()
        (plugin / "rules/AGENTS.md").write_text(found.rules)
    if found.skills is not None:
        (plugin / "skills").symlink_to(found.skills)
    if found.servers:
        config = plugin / "mcp_config.json"
        config.touch(mode=0o600)
        config.write_text(json.dumps({"mcpServers": found.servers}))
    return root


def runtime(command: str) -> tuple[Path, ...]:
    """What a stdio MCP `command` in a Python virtual environment runs from, for
    the cage to show read-only: the environment, its base interpreter's install
    and the editable source roots its `.pth` files name inside the project that
    owns it (read, never run). Any other command runs from what the cage already
    shows, the account HOME and /usr."""
    venv = Path(command).parent.parent
    if not Path(command).is_absolute() or not (venv / "pyvenv.cfg").is_file():
        return ()
    # Nothing holding the account's HOME, nor inside it beyond the project:
    # that is where ~/.ssh and every vendor's credentials live.
    home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    project = venv.resolve().parent
    roots = [venv.resolve()]
    for line in (venv / "pyvenv.cfg").read_text().splitlines():
        key, _, value = line.partition("=")
        base = Path(value.strip()).resolve().parent
        # Only an interpreter's install, never the directory a stray `home` names.
        if key.strip() == "home" and any(base.glob("lib/python3*")):
            roots.append(base)
    for declaration in sorted(venv.glob("lib/python3*/site-packages/*.pth")):
        for line in declaration.read_text().splitlines():
            source = Path(line).resolve() if line.startswith("/") else None
            if (
                source is not None
                and source.is_dir()
                and source.is_relative_to(project)
                and not home.is_relative_to(project)
            ):
                roots.append(source)
    return tuple(root for root in dict.fromkeys(roots) if not home.is_relative_to(root))


class Agent:
    def __init__(self) -> None:
        self.roster, self.provider = roster_provider()
        self.recorded = os.environ.get(ACCOUNT_ENV) or None
        if self.recorded is not None and self.recorded not in self.provider.accounts:
            raise Refused(f"agy account {self.recorded!r} is not in the roster")
        self.sessions: dict[str, Session] = {}
        self.writing = threading.Lock()

    @property
    def account(self) -> str | None:
        """The one account every turn runs on and a quota refusal walls: the one
        the node host recorded, else the first by name, as `prepare_native` picks
        one left unnamed."""
        return self.recorded or next(iter(sorted(self.provider.accounts)), None)

    def emit(self, frame: dict[str, Any]) -> None:
        with self.writing:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", **frame}) + "\n")
            sys.stdout.flush()

    def models(self, current: str) -> dict[str, Any]:
        ids = self.provider.models or [current]
        return {
            "currentModelId": current,
            "availableModels": [{"modelId": m, "name": m} for m in ids],
        }

    def session(self, params: dict[str, Any]) -> Session:
        found = self.sessions.get(params.get("sessionId"))  # type: ignore[arg-type]
        if found is None:
            raise Refused("unknown sessionId")
        return found

    def handle(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        match method:
            case "initialize":
                return {
                    "protocolVersion": 1,
                    "agentCapabilities": {
                        "loadSession": False,
                        "promptCapabilities": {"embeddedContext": True},
                    },
                    "authMethods": [],
                }
            case "session/new":
                cwd = Path(str(params.get("cwd")))
                if not cwd.is_absolute() or not cwd.is_dir():
                    raise Refused("session/new requires an existing absolute cwd")
                inputs = customization(params)
                ident = str(uuid4())
                assert self.provider.model is not None
                self.sessions[ident] = Session(cwd, self.provider.model, **inputs)
                return {"sessionId": ident, "models": self.models(self.provider.model)}
            case "session/set_model":
                found = self.session(params)
                model = params.get("modelId")
                if not isinstance(model, str) or (
                    self.provider.models and model not in self.provider.models
                ):
                    raise Refused("agy provider does not serve that model")
                found.model = model
                return {}
            case "session/prompt":
                return self.prompt(params)
            case _:
                raise Refused(f"method not supported: {method}")

    def prompt(self, params: dict[str, Any]) -> dict[str, Any]:
        found = self.session(params)
        with found.lock:
            if found.busy:
                raise Refused("a prompt is already running on this session")
            found.busy, found.cancelled = True, False
        try:
            return self.turn(found, params)
        finally:
            with found.lock:
                found.busy = False

    def turn(self, found: Session, params: dict[str, Any]) -> dict[str, Any]:
        text = "\n\n".join(words(block) for block in params.get("prompt", []))
        launch = native.prepare_native(
            replace(self.provider, model=found.model),
            self.account,
            roster=self.roster,
            cwd=found.cwd,
            read_only=False,
            resume_id=found.conversation,
        )
        shown: list[str] = []

        def stream(text: str) -> None:
            shown.append(text)
            self.say(params["sessionId"], text)

        bindings = SecretNeedles(
            tuple(
                server["env"][BINDING]
                for server in found.servers.values()
                if BINDING in server["env"]
            )
        )
        with (
            tempfile.TemporaryDirectory(prefix="agy-acp-", dir="/var/tmp") as run_dir,
            tempfile.TemporaryDirectory(prefix="agy-acp-in-", dir="/var/tmp") as staged,
        ):
            confined = native.agy_confined(
                launch,
                cwd=found.cwd,
                run_dir=Path(run_dir),
                writable=True,
                customization=stage(found, Path(staged)),
                read_paths=tuple(
                    dict.fromkeys(
                        (
                            *(() if found.skills is None else (found.skills,)),
                            *(
                                path
                                for server in found.servers.values()
                                for path in runtime(server["command"])
                            ),
                        )
                    )
                ),
                sockets=tuple(
                    Path(server["env"][DOOR])
                    for server in found.servers.values()
                    if DOOR in server["env"]
                ),
            )
            with agy.AgyTurn.start(
                confined.argv,
                cwd=found.cwd,
                env=confined.env,
                run_dir=Path(run_dir),
                needles=(
                    launch.needles
                    if bindings.is_empty()
                    else launch.needles.merged(bindings)
                ),
            ) as turn:
                with found.lock:
                    found.turn = turn
                    if found.cancelled:
                        return {"stopReason": "cancelled"}
                try:
                    result = turn.run(
                        text, conversation_id=found.conversation, on_text=stream
                    )
                except agy.AgyError:
                    if found.cancelled:
                        return {"stopReason": "cancelled"}
                    raise
                finally:
                    with found.lock:
                        found.turn = None
        found.conversation = result.conversation_id
        # Every ended turn advances the conversation's count, a cancelled one too.
        counted = {
            name: result.usage[name]
            for name, _ in USAGE
            if isinstance(result.usage.get(name), int)
        }
        usage = {
            acp: counted[name] - found.counted.get(name, 0)
            for name, acp in USAGE
            if name in counted
        }
        # A counter the result leaves out keeps its last count.
        found.counted = {**found.counted, **counted}
        if result.status == "CANCELED" or found.cancelled:
            return {"stopReason": "cancelled"}
        if result.status != "SUCCESS":
            self.wall(result.error)
            raise Refused(result.error or f"AGY turn ended {result.status}")
        final, said = result.final_text or "", "".join(shown)
        if final.rstrip() not in said:
            # What the stream has not said: the answer past the longest end of the
            # stream it begins with (the stream may open with a working note).
            rest = final[
                len(said) if final.startswith(said) else overlap(said, final) :
            ]
            self.say(params["sessionId"], rest)
        return {
            "stopReason": "end_turn",
            **({"usage": usage} if usage else {}),
            "_meta": {"nott": {"nativeSessionId": found.conversation}},
        }

    def say(self, session: str, text: str) -> None:
        for at in range(0, len(text), CHUNK):
            self.emit(
                {
                    "method": "session/update",
                    "params": {
                        "sessionId": session,
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": text[at : at + CHUNK]},
                        },
                    },
                }
            )

    def wall(self, error: str | None) -> None:
        """AGY's quota refusal walls the account these turns run on
        (`self.account`) until the reset it names: AGY says its limit nowhere
        else, and these turns are the ones that meet it. The refusal still
        reaches the caller when the wall cannot be written."""
        now = int(time.time())
        until = agy.quota_reset(error, now)
        alias = self.account
        if until is None or until <= now or alias is None:
            return
        try:
            with Registry(store_path(), "agy-acp", allow_live_store=True) as store:
                store.record_account_wall(
                    alias, until, "vendor_reset", "agy_quota", now
                )
        except (NSpawnError, OSError, *STORE_ERRORS) as failure:
            print(
                f"agy ACP agent could not record the wall: {failure}", file=sys.stderr
            )

    def cancel(self, params: dict[str, Any]) -> None:
        found = self.sessions.get(params.get("sessionId"))  # type: ignore[arg-type]
        if found is None:
            return
        with found.lock:
            found.cancelled = True
            turn = found.turn
        if turn is not None and turn.child.poll() is None:
            try:
                turn.child.stop_then_kill(turn.child.identity)
            except NSpawnError:
                pass  # the child already left; its turn reports the cancel

    def answer(self, request: dict[str, Any]) -> None:
        ident = request.get("id")
        try:
            result = self.handle(str(request.get("method")), request.get("params", {}))
        except (
            Refused,
            NSpawnError,
            OSError,
            ValueError,
            KeyError,
            TypeError,
        ) as error:
            self.emit({"id": ident, "error": {"code": -32603, "message": str(error)}})
        else:
            self.emit({"id": ident, "result": result})

    def serve(self) -> None:
        for line in sys.stdin:
            try:
                request = json.loads(line)
            except ValueError:
                continue
            if not isinstance(request, dict) or "method" not in request:
                continue  # responses to requests this agent never makes
            if request["method"] == "session/cancel":
                self.cancel(request.get("params", {}))
            elif "id" in request:
                # Prompts run beside the reader so a cancel can reach them.
                threading.Thread(
                    target=self.answer, args=(request,), daemon=True
                ).start()


def main() -> int:
    try:
        agent = Agent()
    except (Refused, NSpawnError, OSError, ValueError) as error:
        print(f"agy ACP agent refused: {error}", file=sys.stderr)
        return 2
    agent.serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
