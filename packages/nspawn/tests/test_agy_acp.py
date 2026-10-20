"""The AGY ACP agent: a cancel that wins the race, a malformed line, a streamed turn."""

from __future__ import annotations

import io
import json
import socket
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from registry_reset import store
from test_agy import EXHAUSTED, LIVE

from nspawn import agy, agy_acp, native
from nspawn.errors import NSpawnError
from nspawn.limits import MAX_DURABLE_OBSERVATION_BYTES
from nspawn.owner_roster import NativeAccount
from nspawn.process_identity import group_member_pids
from nspawn.redact import REDACTION_MARKER, SecretNeedles
from nspawn.registry import Registry
from nspawn.rows import NativeSessionContext


def test_the_managed_agy_adapter_confines_its_selected_native_child_and_reports_its_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home, outside, checkout = (
        tmp_path / name for name in ("home", "outside", "checkout")
    )
    home.mkdir(mode=0o750)
    outside.mkdir()
    checkout.mkdir()
    (outside / "canary").write_text("original outside")
    (checkout / ".git").mkdir()
    (checkout / ".git" / "config").write_text("original metadata")
    peer = tmp_path / "agy-peer.py"
    peer.write_text(
        "import json,os,sys\nfrom pathlib import Path\n"
        "print(json.dumps({'event':'init','conversation_id':'native-origin'}),flush=True)\n"
        "json.loads(sys.stdin.readline())\n"
        "def writes(path):\n"
        " try: Path(path).write_text('changed'); return True\n"
        " except OSError: return False\n"
        f"result={{'outside':writes({str(outside / 'canary')!r}),'git':writes({str(checkout / '.git/config')!r}),'home':writes({str(home / 'ungranted')!r}),'project':writes({str(checkout / 'answer')!r})}}\n"
        "print(json.dumps({'event':'result','result':{'conversation_id':'native-origin','status':'SUCCESS','response':json.dumps(result),'usage':{'total_tokens':0},'duration_seconds':0}}),flush=True)\n"
    )
    launch = native.NativeLaunch(
        (str(Path("/usr/bin/python3").resolve()), str(peer)),
        {"HOME": str(home), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
        SecretNeedles.none(),
        NativeSessionContext(str(home), read_roots=(str(peer),)),
        NativeAccount(home=home),
    )
    monkeypatch.setattr(native, "prepare_native", lambda *args, **kwargs: launch)
    agent = bare_agent()
    agent.sessions["wrapper-session"] = agy_acp.Session(checkout, "m")
    answer = agent.prompt(
        {
            "sessionId": "wrapper-session",
            "prompt": [{"type": "text", "text": "measure boundary"}],
        }
    )
    transcript = "".join(said(capsys.readouterr().out.splitlines()))
    assert json.loads(transcript) == {
        "outside": False,
        "git": False,
        "home": False,
        "project": True,
    }
    assert answer["_meta"]["nott"]["nativeSessionId"] == "native-origin"
    assert agent.sessions["wrapper-session"].conversation == "native-origin"


def customized_home(tmp_path: Path) -> tuple[Path, Path, Path]:
    """An account HOME with the owner's AGY config, and a checkout."""
    home, checkout = tmp_path / "home", tmp_path / "checkout"
    home.mkdir(mode=0o750)
    checkout.mkdir()
    config = home / ".gemini/config"
    (config / "plugins/owner-plugin").mkdir(parents=True)
    (config / "plugins/owner-plugin/plugin.json").write_text('{"name": "owner"}')
    # A stale copy of the session plugin the session's own must cover.
    (config / f"plugins/{agy_acp.PLUGIN}/rules").mkdir(parents=True)
    (config / f"plugins/{agy_acp.PLUGIN}/rules/AGENTS.md").write_text("stale role")
    (config / "config.json").write_text('{"owner": true}')
    return home, checkout, config


BINDING = "fixture-binding-0123456789abcdef"


def new_session(agent: agy_acp.Agent, checkout: Path, **extra: Any) -> str:
    answer = agent.handle("session/new", {"cwd": str(checkout), **extra})
    return str(answer["sessionId"])


def test_a_session_carries_its_role_skills_and_bridge_into_the_caged_agy_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The ACP session's role, skill root and MCP servers reach the native child
    through AGY's documented plugin root, read-only, beside the owner's own config;
    the bridge's door is the one host socket the cage shows."""
    home, checkout, config = customized_home(tmp_path)
    before = {p: p.read_bytes() for p in config.rglob("*") if p.is_file()}
    skills = tmp_path / "role/skills"
    (skills / "node-create").mkdir(parents=True)
    (skills / "node-create/SKILL.md").write_text("skill beacon")
    door = tmp_path / "door/node.sock"
    door.parent.mkdir()
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(door))
    listener.listen(1)

    def serve() -> None:
        connection, _ = listener.accept()
        with connection:
            connection.sendall(connection.recv(64).upper())

    threading.Thread(target=serve, daemon=True).start()
    plugin = home / ".gemini/config/plugins" / agy_acp.PLUGIN
    peer = tmp_path / "agy-peer.py"
    peer.write_text(
        "import json,os,socket,sys\nfrom pathlib import Path\n"
        "print(json.dumps({'event':'init','conversation_id':'c'}),flush=True)\n"
        "sys.stdin.readline()\n"
        f"plugin=Path({str(plugin)!r})\n"
        "servers=json.loads((plugin/'mcp_config.json').read_text())['mcpServers']\n"
        "door=servers['nott-node']['env']['NOTT_NODE_DOOR']\n"
        "c=socket.socket(socket.AF_UNIX);c.connect(door);c.sendall(b'knock')\n"
        "def writes(path):\n"
        " try: Path(path).write_text('x'); return True\n"
        " except OSError: return False\n"
        "seen={'rules':(plugin/'rules/AGENTS.md').read_text(),"
        "'skill':(plugin/'skills/node-create/SKILL.md').read_text(),"
        "'servers':servers,'door':c.recv(64).decode(),"
        f"'owner':sorted(os.listdir({str(config / 'plugins')!r})),"
        f"'config':Path({str(config / 'config.json')!r}).read_text(),"
        "'writable':writes(plugin/'rules/AGENTS.md'),"
        "'staged':[str(p) for p in Path(os.environ['TMPDIR']).parent.rglob('*') if 'session' in p.name],"
        f"'parent':os.listdir({str(door.parent)!r})}}\n"
        "print(json.dumps({'event':'result','result':{'conversation_id':'c','status':'SUCCESS','response':json.dumps(seen),'usage':{},'duration_seconds':0}}),flush=True)\n"
    )
    launch = native.NativeLaunch(
        (str(Path("/usr/bin/python3").resolve()), str(peer)),
        {"HOME": str(home), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
        SecretNeedles.none(),
        NativeSessionContext(str(home), read_roots=(str(peer),)),
        NativeAccount(home=home),
    )
    monkeypatch.setattr(native, "prepare_native", lambda *args, **kwargs: launch)
    agent = bare_agent()
    bridge = {
        "name": "nott-node",
        "command": "/usr/bin/python3",
        "args": ["-m", "nott_harness.node_mcp"],
        "env": [
            {"name": "NOTT_NODE_DOOR", "value": str(door)},
            {"name": "NOTT_NODE_BINDING", "value": BINDING},
        ],
    }
    ident = new_session(
        agent,
        checkout,
        mcpServers=[bridge],
        _meta={"rules": "Guara role beacon", "skills": str(skills)},
    )
    agent.prompt({"sessionId": ident, "prompt": [{"type": "text", "text": "x"}]})
    seen = json.loads("".join(said(capsys.readouterr().out.splitlines())))
    listener.close()
    assert seen == {
        "rules": "Guara role beacon",
        "skill": "skill beacon",
        "servers": {
            "nott-node": {
                "command": "/usr/bin/python3",
                "args": ["-m", "nott_harness.node_mcp"],
                # The turn's words never carry the bridge's binding.
                "env": {
                    "NOTT_NODE_DOOR": str(door),
                    "NOTT_NODE_BINDING": REDACTION_MARKER,
                },
            }
        },
        "door": "KNOCK",
        "owner": sorted([agy_acp.PLUGIN, "owner-plugin"]),
        "config": '{"owner": true}',
        "writable": False,
        "staged": [],
        "parent": ["node.sock"],
    }
    assert {p: p.read_bytes() for p in config.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize(
    "extra",
    [
        {"_meta": {"rules": "x" * (agy_acp.RULES_BYTES + 1)}},
        {"_meta": {"rules": "role", "skills": "relative/skills"}},
        {"mcpServers": [{"type": "http", "name": "web", "url": "https://x"}]},
        {"mcpServers": [{"name": "a", "command": "c", "args": [], "env": [{}]}]},
        {"_meta": ["not", "an", "object"]},
        {"_meta": []},
    ],
    ids=[
        "oversized-role",
        "relative-skills",
        "http-server",
        "malformed-env",
        "meta",
        "empty-meta-list",
    ],
)
def test_a_session_input_agy_cannot_take_whole_is_refused_at_session_new(
    tmp_path: Path, extra: dict[str, Any]
) -> None:
    with pytest.raises(agy_acp.Refused):
        new_session(bare_agent(), tmp_path, **extra)


def test_a_door_that_is_not_this_accounts_socket_refuses_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, checkout, _ = customized_home(tmp_path)
    door = tmp_path / "node.sock"
    door.write_text("a regular file where the socket was")
    launch = native.NativeLaunch(
        (str(Path("/usr/bin/python3").resolve()), "-c", "pass"),
        {"HOME": str(home), "PATH": "/usr/bin:/bin"},
        SecretNeedles.none(),
        NativeSessionContext(str(home)),
        NativeAccount(home=home),
    )
    monkeypatch.setattr(native, "prepare_native", lambda *args, **kwargs: launch)
    agent = bare_agent()
    server = {
        "name": "nott-node",
        "command": "/usr/bin/python3",
        "args": [],
        "env": [{"name": "NOTT_NODE_DOOR", "value": str(door)}],
    }
    ident = new_session(agent, checkout, mcpServers=[server])
    with pytest.raises(NSpawnError, match="socket"):
        agent.prompt({"sessionId": ident, "prompt": [{"type": "text", "text": "x"}]})


@pytest.mark.parametrize("event", ["init", "result"])
@pytest.mark.parametrize("large", [False, True], ids=["short", "bounded"])
def test_pre_identity_refusal_keeps_redacted_event_and_stderr_after_turn_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    event: str,
    large: bool,
) -> None:
    home, checkout = tmp_path / "home", tmp_path / "checkout"
    home.mkdir(mode=0o750)
    checkout.mkdir()
    secret = "test-credential-0123456789"
    padding = "汉" * MAX_DURABLE_OBSERVATION_BYTES if large else ""
    diagnostic = f"vendor-frame-marker {secret} {padding}"
    frame = (
        {"event": "init", "conversation_id": None, "error": diagnostic}
        if event == "init"
        else {
            "event": "result",
            "result": {
                "conversation_id": None,
                "status": "ERROR",
                "error": diagnostic,
                "usage": {},
                "duration_seconds": 0,
            },
        }
    )
    frame["inventory"] = {"tools": ["inventory-must-not-leave"]}
    frame["environment"] = {"credential": secret, "cwd": str(checkout)}
    peer = tmp_path / "agy-peer.py"
    peer.write_text(
        "import json,sys\n"
        "sys.stdin.read()\n"
        f"sys.stderr.write({(padding + ' vendor-stderr-marker ' + secret + chr(10))!r})\n"
        "sys.stderr.flush()\n"
        f"print(json.dumps({frame!r}),flush=True)\n"
    )
    launch = native.NativeLaunch(
        (str(Path("/usr/bin/python3").resolve()), str(peer)),
        {"HOME": str(home), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
        SecretNeedles((secret,)),
        NativeSessionContext(str(home), read_roots=(str(peer),)),
        NativeAccount(home=home),
    )
    monkeypatch.setattr(native, "prepare_native", lambda *args, **kwargs: launch)
    original_start = agy.AgyTurn.start
    started: list[tuple[Path, agy.AgyTurn]] = []

    @contextmanager
    def record_start(*args: Any, **kwargs: Any) -> Iterator[agy.AgyTurn]:
        with original_start(*args, **kwargs) as turn:
            started.append((kwargs["run_dir"], turn))
            yield turn

    monkeypatch.setattr(agy.AgyTurn, "start", record_start)
    agent = bare_agent()
    agent.sessions["s"] = agy_acp.Session(checkout, "m")
    agent.answer({"id": 9, "method": "session/prompt", "params": PARAMS})
    response = json.loads(capsys.readouterr().out)
    ((run_dir, turn),) = started
    assert not run_dir.exists()
    assert turn.child._process.returncode is not None
    assert group_member_pids(turn.child.identity.pid) == ()
    assert agent.sessions["s"].conversation is None
    assert agent.sessions["s"].turn is None and not agent.sessions["s"].busy
    assert response["id"] == 9 and response["error"]["code"] == -32603
    message = response["error"]["message"]
    assert message.startswith("nERR-2003::")
    assert "AGY conversation identity is missing or invalid" in message
    assert "vendor-frame-marker" in message
    assert "vendor-stderr-marker" in message
    assert REDACTION_MARKER in message and secret not in message
    assert "inventory-must-not-leave" not in message and str(checkout) not in message
    assert len(message.encode("utf-8")) <= MAX_DURABLE_OBSERVATION_BYTES


@dataclass
class Provider:
    model: str = "m"
    models: tuple[str, ...] = ("m",)
    accounts: dict[str, object] = field(default_factory=dict)


def bare_agent() -> agy_acp.Agent:
    agent = agy_acp.Agent.__new__(agy_acp.Agent)
    agent.roster = None  # type: ignore[assignment]
    agent.provider = Provider()  # type: ignore[assignment]
    agent.recorded = None
    agent.sessions = {}
    agent.writing = threading.Lock()
    return agent


def test_a_cancel_before_the_turn_is_published_never_runs_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agent = bare_agent()
    agent.sessions["s"] = agy_acp.Session(tmp_path, "m")
    ran: list[str] = []

    class Turn:
        def run(self, text: str, conversation_id: str | None) -> Any:
            ran.append(text)
            raise AssertionError("a cancelled turn ran")

    @contextmanager
    def start(*_: Any, **__: Any) -> Iterator[Turn]:
        # The cancel lands after prompt() cleared the flag, before turn is set.
        agent.cancel({"sessionId": "s"})
        yield Turn()

    monkeypatch.setattr(
        native,
        "prepare_native",
        lambda *_, **__: SimpleNamespace(argv=["x"], env={}, needles=()),
    )
    monkeypatch.setattr(agy.AgyTurn, "start", start)
    monkeypatch.setattr(native, "agy_confined", lambda launch, **kwargs: launch)
    params = {"sessionId": "s", "prompt": [{"type": "text", "text": "hi"}]}
    assert agent.prompt(params) == {"stopReason": "cancelled"}
    assert ran == []


def said(lines: list[str]) -> list[str]:
    return [json.loads(x)["params"]["update"]["content"]["text"] for x in lines]


def run_turn(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    agent: agy_acp.Agent,
    streamed: list[str],
    final: str,
    usage: dict[str, int],
    status: str = "SUCCESS",
) -> tuple[list[str], list[str], dict[str, Any]]:
    """One turn whose AGY child streams `streamed`, then answers `final` and its
    conversation's cumulative `usage`: what left during the turn, after it, and
    the prompt's answer."""
    during: list[str] = []

    class Turn:
        def run(
            self,
            text: str,
            conversation_id: str | None,
            on_text: Callable[[str], None] | None = None,
        ) -> agy.AgyResult:
            assert on_text is not None
            for chunk in streamed:
                on_text(chunk)
            during.extend(said(capsys.readouterr().out.splitlines()))
            return agy.AgyResult(
                conversation_id="c",
                status=status,
                final_text=final,
                usage=usage,
                error=None,
                duration_seconds=0.1,
            )

    @contextmanager
    def start(*_: Any, **__: Any) -> Iterator[Turn]:
        yield Turn()

    monkeypatch.setattr(
        native,
        "prepare_native",
        lambda *_, **__: SimpleNamespace(argv=["x"], env={}, needles=()),
    )
    monkeypatch.setattr(agy.AgyTurn, "start", start)
    monkeypatch.setattr(native, "agy_confined", lambda launch, **kwargs: launch)
    answer = agent.prompt(
        {"sessionId": "s", "prompt": [{"type": "text", "text": "hi"}]}
    )
    return during, said(capsys.readouterr().out.splitlines()), answer


def test_a_turn_streams_its_lines_and_reports_its_usage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    agent = bare_agent()
    agent.sessions["s"] = agy_acp.Session(tmp_path, "m")
    counters = {
        "input_tokens": 5,
        "output_tokens": 3,
        "thinking_tokens": 2,
        "total_tokens": 8,
    }
    during, after, answer = run_turn(
        monkeypatch,
        capsys,
        agent,
        ["First line.\n"],
        "First line.\nLast line.",
        counters,
    )
    # The first line leaves while the turn runs; the end adds only what is left.
    assert during == ["First line.\n"]
    assert after == ["Last line."]
    assert answer == {
        "stopReason": "end_turn",
        "_meta": {"nott": {"nativeSessionId": "c"}},
        "usage": {
            "inputTokens": 5,
            "outputTokens": 3,
            "thoughtTokens": 2,
            "totalTokens": 8,
        },
    }
    # AGY counts its conversation; a resumed turn reports only its own share,
    # after a cancelled one that counted too.
    cancelled = {k: v * 2 for k, v in counters.items()}
    _, _, answer = run_turn(
        monkeypatch, capsys, agent, [], "", cancelled, status="CANCELED"
    )
    assert answer == {"stopReason": "cancelled"}
    later = {k: v * 3 for k, v in counters.items()}
    _, _, answer = run_turn(monkeypatch, capsys, agent, [], "Again.", later)
    assert answer["usage"] == {
        "inputTokens": 5,
        "outputTokens": 3,
        "thoughtTokens": 2,
        "totalTokens": 8,
    }


def test_a_turn_ended_without_usage_keeps_the_count(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    agent = bare_agent()
    agent.sessions["s"] = agy_acp.Session(tmp_path, "m")
    run_turn(monkeypatch, capsys, agent, [], "One.", {"total_tokens": 100})
    _, _, answer = run_turn(monkeypatch, capsys, agent, [], "", {}, status="CANCELED")
    assert answer == {"stopReason": "cancelled"}
    _, _, answer = run_turn(
        monkeypatch, capsys, agent, [], "Two.", {"total_tokens": 110}
    )
    assert answer["usage"] == {"totalTokens": 10}


def test_the_end_adds_what_the_stream_did_not_say(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    agent = bare_agent()
    agent.sessions["s"] = agy_acp.Session(tmp_path, "m")
    # The stream said a working note, then the answer's first line.
    _, after, _ = run_turn(
        monkeypatch,
        capsys,
        agent,
        ["Working...\n", "Answer first line.\n"],
        "Answer first line.\nAnswer last line.",
        {},
    )
    assert after == ["Answer last line."]
    # An answer the stream said whole, but for its trailing newline, is not said again.
    _, after, _ = run_turn(monkeypatch, capsys, agent, ["PONG\n"], "PONG", {})
    assert after == []


def test_a_long_stream_is_matched_to_its_answer_in_linear_time(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    agent = bare_agent()
    agent.sessions["s"] = agy_acp.Session(tmp_path, "m")
    began = time.monotonic()
    _, after, _ = run_turn(
        monkeypatch, capsys, agent, ["a" * 1_200_000 + "x"], "answer", {}
    )
    assert after == ["answer"]
    assert time.monotonic() - began < 2, time.monotonic() - began


def test_a_long_reply_leaves_in_frames_the_host_accepts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    agent = bare_agent()
    # One line that JSON escapes to six bytes a character: 1.2 MB in one frame.
    text = "\x01" * 200_000
    agent.say("s", text)
    lines = capsys.readouterr().out.splitlines()
    assert all(len(line.encode()) < 1 << 20 for line in lines), [len(x) for x in lines]
    assert "".join(said(lines)) == text


def test_a_malformed_line_is_skipped_and_the_next_request_answered(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    agent = bare_agent()
    lines = "{not json\n42\n" + json.dumps({"id": 1, "method": "initialize"}) + "\n"
    monkeypatch.setattr("sys.stdin", io.StringIO(lines))
    threads: list[threading.Thread] = []
    real = threading.Thread

    def tracked(*args: Any, **kwargs: Any) -> threading.Thread:
        thread = real(*args, **kwargs)
        threads.append(thread)
        return thread

    monkeypatch.setattr(threading, "Thread", tracked)
    agent.serve()
    for thread in threads:
        thread.join(5)
    frames = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert [f["id"] for f in frames] == [1]
    assert frames[0]["result"]["protocolVersion"] == 1


def test_every_turn_runs_on_the_account_the_node_host_recorded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Declared zeta first, able second: nSPAWN alone would pick by its own rule.
    roster = tmp_path / "spawn.toml"
    roster.write_text(
        f'[bins]\nagy = "{tmp_path / "agy"}"\n'
        '[[provider]]\nname = "agy"\nkind = "agy"\nmodel = "m"\n'
        f'[provider.accounts.zeta]\nhome = "{tmp_path / "zeta"}"\n'
        f'[provider.accounts.able]\nhome = "{tmp_path / "able"}"\n'
    )
    monkeypatch.setenv("NOTT_NSPAWN_ROSTER", str(roster))
    launched: list[object] = []

    def prepare(_provider: object, account: object, **_: Any) -> SimpleNamespace:
        launched.append(account)
        raise agy.AgyError("stop before a process")

    monkeypatch.setattr(native, "prepare_native", prepare)
    for recorded in ("zeta", "able"):
        monkeypatch.setenv(agy_acp.ACCOUNT_ENV, recorded)
        agent = agy_acp.Agent()
        agent.sessions["s"] = agy_acp.Session(tmp_path, "m")
        with pytest.raises(agy.AgyError):
            agent.prompt({"sessionId": "s", "prompt": [{"type": "text", "text": "hi"}]})
    assert launched == ["zeta", "able"]
    monkeypatch.setenv(agy_acp.ACCOUNT_ENV, "gone")
    assert agy_acp.main() == 2


PARAMS = {"sessionId": "s", "prompt": [{"type": "text", "text": "hi"}]}


@dataclass
class Launched:
    agent: agy_acp.Agent
    accounts: list[str | None] = field(default_factory=list)


def refused_turn(
    monkeypatch: pytest.MonkeyPatch,
    cwd: Path,
    accounts: dict[str, object],
    error: str = EXHAUSTED,
) -> Launched:
    """An agent on `accounts` whose turn AGY refuses for its quota with `error`."""
    agent = bare_agent()
    agent.provider = Provider(accounts=accounts)  # type: ignore[assignment]
    agent.sessions["s"] = agy_acp.Session(cwd, "m")
    launched = Launched(agent)
    result = agy.AgyResult("c-1", "ERROR", None, {}, error, 0.5)

    class Turn:
        def run(
            self,
            text: str,
            conversation_id: str | None,
            on_text: Callable[[str], None] | None = None,
        ) -> Any:
            return result

    @contextmanager
    def start(*_: Any, **__: Any) -> Iterator[Turn]:
        yield Turn()

    def prepare(_: object, account: str | None, **__: Any) -> SimpleNamespace:
        launched.accounts.append(account)
        return SimpleNamespace(argv=["x"], env={}, needles=())

    monkeypatch.setattr(native, "prepare_native", prepare)
    monkeypatch.setattr(agy.AgyTurn, "start", start)
    monkeypatch.setattr(native, "agy_confined", lambda launch, **kwargs: launch)
    return launched


def test_a_quota_refusal_walls_the_account_and_still_reaches_the_caller(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The node host's AGY turns are the only ones that meet AGY's limit: its
    refusal walls the account until the reset it names, where nSPAWN's quota
    report and picker read it, and the owner still gets AGY's own words."""
    # Declared out of name order: the turn runs on, and walls, the same one.
    launched = refused_turn(monkeypatch, tmp_path, {"zeta": None, "agy1": None})
    store()
    before = int(time.time())
    with pytest.raises(agy_acp.Refused, match="Individual quota reached"):
        launched.agent.prompt(PARAMS)
    with Registry(store(), "t") as registry:
        (wall,) = registry.get_account_wall_rows(before)
    wait = 126 * 3600 + 54 * 60 + 6
    assert launched.accounts == ["agy1"]
    assert wall.account_alias == "agy1"
    assert before + wait <= wall.cooling_until <= int(time.time()) + wait
    assert (wall.source, wall.limit_type) == ("vendor_reset", "agy_quota")


def test_the_live_quota_refusal_walls_the_account_so_a_new_node_skips_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The live AGY CLI says its quota refusal without its code. The wall is the
    one `route show --move` and the picker read."""
    launched = refused_turn(monkeypatch, tmp_path, {"agy1": None}, LIVE)
    store()
    before = int(time.time())
    with pytest.raises(agy_acp.Refused, match="Individual quota reached"):
        launched.agent.prompt(PARAMS)
    with Registry(store(), "t") as registry:
        (wall,) = registry.get_account_wall_rows(before)
    wait = 85 * 3600 + 10 * 60 + 7
    assert wall.account_alias == "agy1"
    assert before + wait <= wall.cooling_until <= int(time.time()) + wait
    assert (wall.source, wall.limit_type) == ("vendor_reset", "agy_quota")


def test_a_wall_that_cannot_be_written_leaves_agys_refusal_to_the_caller(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    launched = refused_turn(monkeypatch, tmp_path, {"agy1": None})

    def unreadable() -> Path:
        raise PermissionError("store directory is not this account's")

    monkeypatch.setattr(agy_acp, "store_path", unreadable)
    with pytest.raises(agy_acp.Refused, match="Individual quota reached"):
        launched.agent.prompt(PARAMS)


def test_attached_files_reach_agy_as_words_and_an_image_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The node host hands on the app's attached files as ACP blocks; AGY takes text
    only. A link and an embedded file become words, as claude-agent-acp words them;
    an image, which AGY cannot see, is refused rather than dropped."""
    agent = bare_agent()
    # It reads an embedded file and no image: the node host links an image instead.
    capabilities = agent.handle("initialize", {})["agentCapabilities"]
    assert capabilities["promptCapabilities"] == {"embeddedContext": True}
    agent.sessions["s"] = agy_acp.Session(tmp_path, "m")
    ran: list[str] = []

    class Turn:
        def run(
            self,
            text: str,
            conversation_id: str | None,
            on_text: Callable[[str], None] | None = None,
        ) -> agy.AgyResult:
            ran.append(text)
            return agy.AgyResult("c", "SUCCESS", "ok", {}, None, 0.1)

    @contextmanager
    def start(*_: Any, **__: Any) -> Iterator[Turn]:
        yield Turn()

    monkeypatch.setattr(
        native,
        "prepare_native",
        lambda *_, **__: SimpleNamespace(argv=["x"], env={}, needles=()),
    )
    monkeypatch.setattr(agy.AgyTurn, "start", start)
    monkeypatch.setattr(native, "agy_confined", lambda launch, **kwargs: launch)
    link = {
        "type": "resource_link",
        "uri": "file:///Users/owner/report.pdf",
        "name": "report.pdf",
    }
    embedded = {
        "type": "resource",
        "resource": {"uri": "file:///Users/owner/notes.md", "text": "# Notes\n"},
    }
    words = {"type": "text", "text": "What do these say?"}
    agent.prompt({"sessionId": "s", "prompt": [link, embedded, words]})
    capsys.readouterr()
    assert ran == [
        (
            "[@report.pdf](file:///Users/owner/report.pdf)\n\n"
            '<context ref="file:///Users/owner/notes.md">\n# Notes\n\n</context>\n\n'
            "What do these say?"
        )
    ]
    image = {"type": "image", "data": "iVBORw0KGgo=", "mimeType": "image/png"}
    with pytest.raises(agy_acp.Refused, match="AGY takes no image"):
        agent.prompt({"sessionId": "s", "prompt": [image, words]})
    assert len(ran) == 1


def test_runtime_shows_only_its_project_sources_never_the_account_home(
    tmp_path: Path,
) -> None:
    import os
    import pwd

    project = tmp_path / "project"
    venv = project / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("")
    site = venv / "lib/python3.13/site-packages"
    site.mkdir(parents=True)
    source = project / "harness"
    source.mkdir()
    outside = tmp_path / "credentials"  # as ~/.ssh is beside a project in HOME
    outside.mkdir()
    home = pwd.getpwuid(os.getuid()).pw_dir
    (site / "x.pth").write_text(
        f"{source}\n{outside}\n{home}\n{Path(home).parent}\n/\nimport os\n"
    )
    assert agy_acp.runtime(str(venv / "bin/python")) == (
        venv.resolve(),
        source.resolve(),
    )
