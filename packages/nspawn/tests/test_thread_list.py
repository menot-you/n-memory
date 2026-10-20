"""External sessions: `nspawn thread list|show` through the real verb, a fake
Codex app-server on real child pipes, and the installed Claude Agent SDK on a fake
Claude home. No case reads the owner's homes or stores."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from registry_reset import store

from nspawn import route
from nspawn.registry import Registry
from nspawn.rows import Backend, RunPhase, RunRow

FAKE_CODEX = r"""
import json, os, sys, time
from pathlib import Path
home = Path(os.environ['CODEX_HOME'])
if (home / 'hang').exists():
    (home / 'hang-pid').write_text(str(os.getpid()))
    time.sleep(300)
data = json.loads((home / 'threads.json').read_text())
def send(value):
    print(json.dumps(value), flush=True)
for line in sys.stdin:
    frame = json.loads(line)
    if 'id' not in frame:
        continue
    method, params = frame['method'], frame.get('params') or {}
    if method == 'initialize':
        send({'id': frame['id'], 'result': {'userAgent': 'fixture', 'codexHome': data.get('codexHome', str(home))}})
    elif method == 'thread/list':
        with (home / 'list-calls.jsonl').open('a') as calls:
            calls.write(json.dumps(params) + '\n')
        index = int(params.get('cursor') or 0)
        pages = data['pages']
        if 'rows' in data:  # pages of the size asked for
            pages = [data['rows'][i:i + params['limit']] for i in range(0, len(data['rows']), params['limit'])]
        send({'id': frame['id'], 'result': {'data': pages[index], 'nextCursor': str(index + 1) if index + 1 < len(pages) else None}})
    elif method == 'thread/read':
        found = [t for page in data['pages'] for t in page if t['id'] == params['threadId']]
        if found:
            send({'id': frame['id'], 'result': {'thread': found[0]}})
        else:
            send({'id': frame['id'], 'error': {'code': -32600, 'message': 'thread not found'}})
    else:
        send({'id': frame['id'], 'error': {'code': -32601, 'message': 'unsupported'}})
"""


class World:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.home = self.private(root / "home")
        self.claude = self.private(root / "claude")
        self.codex = self.private(root / "codex-main")
        (self.codex / "auth.json").write_text(
            json.dumps({"tokens": {"access_token": "synthetic-native-access-token"}})
        )
        (self.codex / "auth.json").chmod(0o600)
        self.binary = root / "codex"
        self.binary.write_text(f"#!{sys.executable}\n{FAKE_CODEX}")
        self.binary.chmod(0o700)
        self.roster = root / "spawn.toml"
        self.roster.write_text(
            f'[store]\nrun_root="{root / "runs"}"\n[bins]\ncodex="{self.binary}"\n'
            '[[provider]]\nname="codex"\nkind="codex"\nmodel="test-model"\n'
            f'[provider.accounts.codex-main]\nhome="{self.codex}"\n'
        )
        self.threads(self.codex, [[]])
        self.env = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.home),
            "CLAUDE_CONFIG_DIR": str(self.claude),
            "NOTT_NSPAWN_ROSTER": str(self.roster),
            "NOTT_NSPAWN_CONFIG": str(self.roster),
            "NOTT_DATABASE_DIR": os.environ["NOTT_DATABASE_DIR"],
            "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
            "TMPDIR": os.environ.get("TMPDIR", "/var/tmp"),
        }
        store()

    @staticmethod
    def private(path: Path) -> Path:
        path.mkdir(mode=0o700, parents=True)
        return path

    @staticmethod
    def threads(home: Path, pages: list[list[dict[str, Any]]], **extra: Any) -> None:
        (home / "threads.json").write_text(json.dumps({"pages": pages, **extra}))

    def verb(self, *args: str, timeout: float = 45) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(Path(sys.executable).with_name("nspawn")), "thread", *args],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def listed(self, *args: str) -> dict[str, Any]:
        done = self.verb("list", *args)
        assert done.returncode == 0, done.stderr
        shown: dict[str, Any] = json.loads(done.stdout)
        return shown

    def session(
        self,
        sid: str,
        entries: list[dict[str, Any]],
        *,
        cwd: str = "/work/demo",
        mtime: float | None = None,
        root: Path | None = None,
    ) -> Path:
        project = (root or self.claude) / "projects" / cwd.replace("/", "-")
        project.mkdir(parents=True, exist_ok=True)
        path = project / f"{sid}.jsonl"
        path.write_text("".join(json.dumps(e) + "\n" for e in entries))
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def recorded(self, thread_id: str, backend: Backend = Backend.CODEX) -> None:
        """A run nSPAWN started, on `thread_id`."""
        with Registry(str(store()), "thread-test") as registry:
            registry.open(now=1000)
            registry.insert_run(
                RunRow(
                    run_id=f"run-{thread_id}",
                    worktree="/fixture",
                    phase=RunPhase.COMPLETED,
                    sandbox="read-only",
                    model="m",
                    owner_instance=registry.instance_id,
                    created_at=1,
                    updated_at=1,
                    backend=backend,
                    thread_id=thread_id,
                    executed_provider="codex",
                    executed_account="codex-main",
                )
            )


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    yield World(tmp_path)


def user(
    uuid: str, parent: str | None, text: str, cwd: str = "/work/demo", **extra: Any
) -> dict[str, Any]:
    return {
        "type": "user",
        "uuid": uuid,
        "parentUuid": parent,
        "sessionId": extra.pop("sid", "s"),
        "cwd": cwd,
        "timestamp": "2026-10-12T10:00:00.000Z",
        "entrypoint": extra.pop("entrypoint", "cli"),
        "message": {"role": "user", "content": text},
        **extra,
    }


def assistant(
    uuid: str,
    parent: str,
    text: str,
    *,
    ident: str,
    model: str = "claude-fake",
    usage: dict[str, int] | None = None,
) -> dict[str, Any]:
    return {
        "type": "assistant",
        "uuid": uuid,
        "parentUuid": parent,
        "sessionId": "s",
        "cwd": "/work/demo",
        "timestamp": "2026-10-12T10:00:05.000Z",
        "message": {
            "id": ident,
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": text}],
            "usage": usage
            or {
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_read_input_tokens": 2,
                "cache_creation_input_tokens": 1,
            },
        },
    }


SDK_REQUIRED = pytest.mark.skipif(
    sys.platform == "darwin",
    reason="runtime_read refuses the Claude Agent SDK off Linux; Claude is unlisted there",
)
A = "11111111-2222-4333-8444-555555555551"
B = "11111111-2222-4333-8444-555555555552"
C = "11111111-2222-4333-8444-555555555553"
D = "11111111-2222-4333-8444-555555555554"
E = "11111111-2222-4333-8444-555555555555"


@SDK_REQUIRED
def test_r1_claude_rows_come_from_the_sdk(world: World) -> None:
    assert route.runtime_read({}) is not None, "the Claude Agent SDK is not installed"
    now = time.time()
    world.session(
        A,
        [
            user("a1", None, "first prompt A"),
            assistant("a2", "a1", "part", ident="msg_1", model="claude-old"),
            # The same message streamed again: its usage counts once.
            assistant(
                "a3",
                "a2",
                "rest",
                ident="msg_1",
                model="claude-new",
                usage={
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cache_read_input_tokens": 20,
                    "cache_creation_input_tokens": 10,
                },
            ),
            {"type": "custom-title", "customTitle": "Custom title A", "sessionId": A},
        ],
        mtime=now - 10,
    )
    world.session(
        B,
        [
            user("b1", None, "first prompt B"),
            {"type": "summary", "summary": "Summary B", "leafUuid": "b1"},
        ],
        mtime=now - 20,
    )
    world.session(C, [user("c1", None, "first prompt C")], mtime=now - 30)
    no_cwd = user("d1", None, "no cwd")
    del no_cwd["cwd"]
    world.session(D, [no_cwd], mtime=now - 5)
    world.session(
        E, [user("e1", None, "headless", entrypoint="sdk-cli")], mtime=now - 1
    )
    shown = world.listed()
    rows = {
        row["sessionId"]: row for row in shown["threads"] if row["provider"] == "claude"
    }
    assert list(rows) == [A, B, C]
    assert [rows[s]["title"] for s in (A, B, C)] == [
        "Custom title A",
        "Summary B",
        "first prompt C",
    ]
    assert rows[A]["cwd"] == "/work/demo"
    assert rows[A]["model"] == "claude-new"
    assert rows[A]["tokens"] == {
        "input": 100,
        "output": 50,
        "cacheRead": 20,
        "cacheWrite": 10,
        "total": 180,
    }
    assert rows[A]["route"] is None and rows[A]["continuable"] is True
    assert rows[A]["home"] == str(world.claude)
    one = world.verb("show", f"claude:{A}")
    assert one.returncode == 0, one.stderr
    assert json.loads(one.stdout)["thread"]["tokens"] is None
    missing = world.verb("show", "claude:11111111-2222-4333-8444-000000000000")
    assert missing.returncode == 3 and "not found" in missing.stderr


@SDK_REQUIRED
def test_r2_claude_tokens_stop_at_the_list_budget(world: World) -> None:
    now = time.time()
    world.session(
        A,
        [user("a1", None, "small"), assistant("a2", "a1", "x", ident="m1")],
        mtime=now - 1,
    )
    # Larger than the 64 MiB budget: its row keeps no tokens.
    big = [user("b0", None, "big")]
    for at in range(1, 70):
        big.append(user(f"b{at}", f"b{at - 1}", "y" * (1024 * 1024)))
    big.append(assistant("bz", "b69", "x", ident="m2"))
    world.session(B, big, mtime=now - 2)
    rows = {r["sessionId"]: r for r in world.listed()["threads"]}
    assert rows[A]["tokens"] is not None and rows[A]["model"] == "claude-fake"
    assert rows[B]["tokens"] is None and rows[B]["model"] is None


@SDK_REQUIRED
def test_r2b_a_large_newest_session_does_not_use_up_the_budget(world: World) -> None:
    """A live session is the newest and often the largest: the older sessions
    that fit the budget still get their tokens."""
    now = time.time()
    big = [user("b0", None, "big")]
    for at in range(1, 70):
        big.append(user(f"b{at}", f"b{at - 1}", "y" * (1024 * 1024)))
    big.append(assistant("bz", "b69", "x", ident="m2"))
    world.session(B, big, mtime=now - 1)
    world.session(
        A,
        [user("a1", None, "small"), assistant("a2", "a1", "x", ident="m1")],
        mtime=now - 2,
    )
    rows = {r["sessionId"]: r for r in world.listed()["threads"]}
    assert rows[B]["tokens"] is None
    assert rows[A]["tokens"] is not None and rows[A]["model"] == "claude-fake"


def codex_thread(ident: str, root: Path, **extra: Any) -> dict[str, Any]:
    return {
        "id": ident,
        "cwd": "/work/codex",
        "name": None,
        "preview": f"preview {ident}",
        "model": "gpt-test",
        "updatedAt": 1791000000,
        "path": str(root / f"{ident}.jsonl"),
        "status": {"type": "notLoaded"},
        **extra,
    }


def rollout(path: Path, totals: list[int]) -> None:
    lines = [{"type": "session_meta", "payload": {}}]
    for total in totals:
        usage = {
            "input_tokens": total,
            "cached_input_tokens": 1,
            "cache_write_input_tokens": 0,
            "output_tokens": 2,
            "reasoning_output_tokens": 0,
            "total_tokens": total + 2,
        }
        lines.append(
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": usage},
                },
            }
        )
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))


def codex_rows(shown: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {r["sessionId"]: r for r in shown["threads"] if r["provider"] == "codex"}


def test_r3_codex_rows_come_from_thread_list(world: World) -> None:
    rollout(world.root / "t1.jsonl", [5, 40])
    world.threads(world.codex, [[codex_thread("t1", world.root, name="Named")]])
    rows = codex_rows(world.listed("--limit", "7"))
    assert rows["t1"]["title"] == "Named"
    assert rows["t1"]["route"] == f"codex-main@{world.codex}"
    assert rows["t1"]["tokens"] == {
        "input": 40,
        "output": 2,
        "cacheRead": 1,
        "cacheWrite": 0,
        "total": 42,
    }
    assert rows["t1"]["continuable"] is True and rows["t1"]["model"] == "gpt-test"
    first = json.loads((world.codex / "list-calls.jsonl").read_text().splitlines()[0])
    assert first == {
        "limit": 200,
        "cursor": None,
        "sortKey": "updated_at",
        "useStateDbOnly": True,
        "archived": False,
        "sourceKinds": ["cli", "vscode", "exec", "appServer", "unknown"],
    }
    shown = world.verb("show", "codex:t1")
    assert shown.returncode == 0, shown.stderr
    assert json.loads(shown.stdout)["thread"]["tokens"]["total"] == 42


def test_r3_codex_home_mismatch_is_unlisted(world: World) -> None:
    world.threads(
        world.codex, [[codex_thread("t1", world.root)]], codexHome="/elsewhere"
    )
    shown = world.listed()
    assert "t1" not in codex_rows(shown)
    entry = next(u for u in shown["unlisted"] if u["home"] == str(world.codex))
    assert entry["provider"] == "codex" and "home" in entry["reason"]


def test_r3b_codex_pages_past_threads_nspawn_started(world: World) -> None:
    world.recorded("mine")
    world.threads(
        world.codex,
        [[codex_thread("mine", world.root)], [codex_thread("theirs", world.root)]],
    )
    assert list(codex_rows(world.listed("--limit", "1"))) == ["theirs"]


def test_r4_a_thread_nspawn_recorded_is_not_listed(world: World) -> None:
    world.recorded("mine")
    world.recorded(A, Backend.CLAUDE)
    world.threads(
        world.codex,
        [[codex_thread("mine", world.root), codex_thread("theirs", world.root)]],
    )
    world.session(A, [user("a1", None, "run by nSPAWN")])
    world.session(B, [user("b1", None, "a terminal session")])
    shown = world.listed()
    assert list(codex_rows(shown)) == ["theirs"]
    claude = [r["sessionId"] for r in shown["threads"] if r["provider"] == "claude"]
    assert claude == [B]
    for reference in ("codex:mine", f"claude:{A}"):
        started = world.verb("show", reference)
        assert started.returncode == 3 and "started by nSPAWN" in started.stderr


def test_codex_alias_with_at_sign_keeps_its_account_route(world: World) -> None:
    world.roster.write_text(
        world.roster.read_text().replace(
            "[provider.accounts.codex-main]", '[provider.accounts."owner@work"]'
        )
    )
    world.threads(world.codex, [[codex_thread("theirs", world.root)]])
    row = codex_rows(world.listed())["theirs"]
    assert row["route"] == route.account_route("owner@work", world.codex)


@SDK_REQUIRED
def test_claude_own_session_does_not_consume_the_limit(world: World) -> None:
    world.recorded(A, Backend.CLAUDE)
    now = time.time()
    world.session(A, [user("a1", None, "own session")], mtime=now)
    world.session(B, [user("b1", None, "terminal session")], mtime=now - 10)
    assert [row["sessionId"] for row in world.listed("--limit", "1")["threads"]] == [B]


def test_claude_scan_limit_does_not_grow_with_registry_history(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading
    from types import SimpleNamespace

    from nspawn.supervisor import Supervisor

    for key, value in world.env.items():
        monkeypatch.setenv(key, value)
    requested: list[int] = []

    def sessions(
        _operation: str, _home: Path, arguments: list[str]
    ) -> list[dict[str, Any]]:
        requested.append(int(arguments[1]))
        return [{"threads": []}]

    registry = SimpleNamespace(
        known_threads=lambda ids=None: (
            {str(i) for i in range(10_000)} if ids is None else set()
        )
    )
    owner = SimpleNamespace(
        _lock=threading.RLock(),
        _registry=registry,
        _claude_sessions=sessions,
        _thread_read=lambda call, *_: call("fixture"),
        _codex_threads=lambda *_args, **_kwargs: [],
    )
    Supervisor.threads(owner, 50)  # type: ignore[arg-type]
    assert requested == [200]


@pytest.mark.parametrize("retain,max_closed", [(0, 10), (10_000, 0)])
def test_pruning_keeps_the_thread_origin(
    world: World, retain: int, max_closed: int
) -> None:
    world.recorded("mine")
    with Registry(str(store()), "prune-test") as registry:
        registry.open(now=1000)
        row = registry.get_run("run-mine")
        registry.update_run(replace(row, phase=RunPhase.CLOSED))
        assert registry.prune_closed_runs(2000, retain, max_closed) == 1
        assert registry.known_threads(["mine", "theirs"]) == {"mine"}
    world.threads(world.codex, [[codex_thread("mine", world.root)]])
    assert codex_rows(world.listed()) == {}
    assert "started by nSPAWN" in world.verb("show", "codex:mine").stderr


@pytest.mark.parametrize("failure", [OSError, KeyError, TypeError])
def test_thread_read_failure_is_not_absence(
    world: World, monkeypatch: pytest.MonkeyPatch, failure: type[Exception]
) -> None:
    import threading
    from types import SimpleNamespace

    from nspawn.supervisor import Supervisor, ThreadUnread

    for key, value in world.env.items():
        monkeypatch.setenv(key, value)

    def read(_operation: str, **_: Any) -> None:
        raise failure("read failed")

    with Registry(str(store()), "read-test") as registry:
        registry.open(now=1000)
        owner = SimpleNamespace(
            _lock=threading.RLock(),
            _registry=registry,
            _thread_read=lambda call, *_: call("fixture"),
            _codex_threads=read,
        )
        expected = ThreadUnread if failure is OSError else failure
        with pytest.raises(expected) as caught:
            Supervisor.threads(owner, 1, ref=("codex", "missing"))  # type: ignore[arg-type]
        if failure is OSError:
            assert caught.value.entry.code == 1128
            assert "OSError" in caught.value.detail


def test_r4b_the_callers_sessions_leave_before_the_limit(world: World) -> None:
    """`--exclude -` names the caller's own sessions on stdin: they never take a
    place of the limit, in Codex pages or in the Claude list."""

    def excluded(*ids: str) -> dict[str, Any]:
        done = subprocess.run(
            [str(Path(sys.executable).with_name("nspawn")), "thread", "list"]
            + ["--limit", "1", "--exclude", "-"],
            input=json.dumps(list(ids)),
            env=world.env,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        assert done.returncode == 0, done.stderr
        shown: dict[str, Any] = json.loads(done.stdout)
        return shown

    world.threads(
        world.codex,
        [[codex_thread("held", world.root)], [codex_thread("theirs", world.root)]],
    )
    assert list(codex_rows(excluded("held"))) == ["theirs"]
    world.threads(world.codex, [[]])
    now = time.time()
    world.session(A, [user("a1", None, "a continued session")], mtime=now)
    world.session(B, [user("b1", None, "an older session")], mtime=now - 60)
    claude = [r["sessionId"] for r in excluded(A)["threads"]]
    assert claude == [B]
    # The page size is not the limit: four pages of held threads at limit 1 still
    # reach the older external thread.
    held = [codex_thread(f"held-{i}", world.root) for i in range(4)]
    world.threads(world.codex, [], rows=[*held, codex_thread("older", world.root)])
    shown = excluded(A, B, *(thread["id"] for thread in held))
    assert [r["sessionId"] for r in shown["threads"]] == ["older"]


@pytest.mark.skipif(
    sys.platform != "linux", reason="liveness reads /proc, which only Linux has"
)
def test_r5_liveness_reads_pid_records_and_writer_locks(world: World) -> None:
    from nspawn.threads import proc_start

    sessions = world.private(world.claude / "sessions")
    me = os.getpid()
    (sessions / f"{me}.json").write_text(
        json.dumps({"pid": me, "sessionId": A, "procStart": proc_start(me)})
    )
    (sessions / "1.json").write_text(
        json.dumps({"pid": me, "sessionId": B, "procStart": "1"})
    )
    now = time.time()
    world.session(A, [user("a1", None, "live")], mtime=now - 1)
    world.session(B, [user("b1", None, "stale record")], mtime=now - 2)
    locks = world.private(world.codex / "thread-writer-locks")
    world.threads(
        world.codex,
        [[codex_thread("held", world.root), codex_thread("free", world.root)]],
    )
    (locks / "free.lock").touch()
    with (locks / "held.lock").open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        shown = world.listed()
    claude = {
        r["sessionId"]: r["live"] for r in shown["threads"] if r["provider"] == "claude"
    }
    assert claude == {A: True, B: False}
    codex = {key: row["live"] for key, row in codex_rows(shown).items()}
    assert codex == {"held": True, "free": False}


def test_r6_default_codex_home_agy_and_a_missing_sdk(world: World) -> None:
    default = world.private(world.home / ".codex")
    world.threads(default, [[codex_thread("terminal", world.root)]])
    agy = world.private(world.root / "agy-main")
    world.roster.write_text(
        world.roster.read_text().replace("[bins]\n", f'[bins]\nagy="{world.binary}"\n')
        + f'[[provider]]\nname="agy"\nkind="agy"\nmodel="m"\n[provider.accounts.agy-main]\nhome="{agy}"\n'
    )
    # No claude-agent-acp on PATH or under HOME: runtime_read finds no SDK.
    world.env["PATH"] = "/usr/bin:/bin"
    shown = world.listed()
    row = codex_rows(shown)["terminal"]
    assert row["continuable"] is False and "~/.codex" in row["reason"]
    assert row["route"] is None and row["home"] == str(default)
    reasons = {u["provider"]: u["reason"] for u in shown["unlisted"]}
    assert "AGY" in reasons["agy"]
    assert "SDK" in reasons["claude"]


@pytest.mark.parametrize(
    "arguments",
    [
        ["list", "--limit", "0"],
        ["list", "--limit", "201"],
        ["show", "claude"],
        ["show", "nobody:x"],
        ["show", "codex:t1", "--messages"],
        ["show", f"claude:{A}", "--claude-home", "/var/tmp"],
    ],
)
def test_r7_malformed_requests_exit_2(world: World, arguments: list[str]) -> None:
    done = world.verb(*arguments)
    assert done.returncode == 2 and "unknown command" not in done.stderr


def test_r7_claude_home_must_be_private(world: World) -> None:
    open_home = world.root / "open"
    open_home.mkdir(mode=0o755)
    open_home.chmod(0o755)
    done = world.verb(
        "show", f"claude:{A}", "--messages", "--claude-home", str(open_home)
    )
    assert done.returncode == 2 and "unknown command" not in done.stderr


def test_r8_a_hung_home_is_killed_and_unlisted(world: World) -> None:
    (world.codex / "hang").touch()
    started = time.monotonic()
    shown = world.listed()
    assert time.monotonic() - started < 25
    entry = next(u for u in shown["unlisted"] if u["home"] == str(world.codex))
    assert "time" in entry["reason"]
    pid = int((world.codex / "hang-pid").read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    pytest.fail("the hung app-server outlived the verb")


@SDK_REQUIRED
def test_r9_messages_follow_the_active_chain(world: World) -> None:
    copy = world.private(world.root / "node-copy")
    world.session(
        A,
        [
            user("a1", None, "kept prompt"),
            assistant("a2", "a1", "kept answer", ident="m1"),
            user("a3", "a2", "abandoned prompt"),
            assistant("a4", "a3", "abandoned answer", ident="m2"),
            user("a5", "a2", "after rewind"),
            assistant("a6", "a5", "final answer", ident="m3"),
        ],
        root=copy,
    )
    done = world.verb("show", f"claude:{A}", "--messages", "--claude-home", str(copy))
    assert done.returncode == 0, done.stderr
    shown = json.loads(done.stdout)
    text = json.dumps(shown["messages"])
    assert "abandoned" not in text and "after rewind" in text and "kept prompt" in text
    assert shown["omitted"] == 0


@SDK_REQUIRED
def test_r9_messages_keep_the_tail_from_a_prompt(world: World) -> None:
    copy = world.private(world.root / "node-copy")
    entries: list[dict[str, Any]] = []
    parent: str | None = None
    for at in range(2101):
        uuid = f"u{at}"
        entries.append(
            user(uuid, parent, f"prompt {at}")
            if at % 2 == 0
            else assistant(uuid, parent or "", f"answer {at}", ident=f"m{at}")
        )
        parent = uuid
    world.session(A, entries, root=copy)
    done = world.verb("show", f"claude:{A}", "--messages", "--claude-home", str(copy))
    assert done.returncode == 0, done.stderr
    shown = json.loads(done.stdout)
    assert shown["omitted"] == 102
    assert len(shown["messages"]) == 1999
    assert shown["messages"][0]["type"] == "user"
    assert shown["messages"][0]["message"]["content"] == "prompt 102"


@pytest.mark.skipif(sys.platform != "linux", reason="the liveness read needs /proc")
def test_r5_a_fifo_record_never_blocks_the_list(tmp_path: Path) -> None:
    """Reads outside the verb's deadline skip a FIFO: no read can block."""
    from nspawn.threads import claude_live, rollout_tokens

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    os.mkfifo(sessions / "123.json")
    os.mkfifo(tmp_path / "rollout.jsonl")
    started = time.monotonic()
    assert claude_live(tmp_path) == {}
    assert rollout_tokens(str(tmp_path / "rollout.jsonl")) is None
    assert time.monotonic() - started < 5


def test_r8_a_child_that_does_not_die_stays_in_custody() -> None:
    """A timed-out read whose kill fails keeps its child where shutdown() finds it."""
    import threading
    from types import SimpleNamespace

    from nspawn import child
    from nspawn.supervisor import Supervisor

    release = threading.Event()

    class Stuck:
        identity = object()

        def stop_then_kill(self, identity: object) -> None:
            raise child.ChildIdentityError("no signal thread")

        def poll(self) -> None:
            return None

    owner = SimpleNamespace(_lock=threading.Lock(), _children={})

    def call(operation: str) -> None:
        owner._children[operation] = Stuck()
        release.wait(10)

    with pytest.raises(child.ChildIdentityError):
        Supervisor._thread_read(owner, call, 0.2, "slow")  # type: ignore[arg-type]
    assert len(owner._children) == 1
    # The reader's own release leaves a child that did not exit, too.
    ((operation, stuck),) = owner._children.items()
    Supervisor._release(owner, operation, stuck)  # type: ignore[arg-type]
    assert owner._children == {operation: stuck}
    release.set()


@SDK_REQUIRED
def test_sdk_prerequisite_matches_the_installed_runtime() -> None:
    adapter = shutil.which("claude-agent-acp")
    assert adapter is not None, "Run tests/sdk_setup.py before SDK tests."
    prefix = Path(adapter).parent.parent.parent
    done = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("sdk_setup.py")),
            str(prefix),
            "--check",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert Path(done.stdout.strip()) == Path(adapter).parent


def test_sdk_setup_refuses_an_install_outside_disposable_state() -> None:
    done = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("sdk_setup.py")), "/"],
        env={"PATH": ""},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert done.returncode == 2
    assert "Select a prefix under .nott/disposable." in done.stderr


def test_sdk_setup_refuses_a_different_package_version(tmp_path: Path) -> None:
    name = "@agentclientprotocol/claude-agent-acp"
    package = tmp_path / "node_modules" / name / "package.json"
    package.parent.mkdir(parents=True)
    package.write_text(json.dumps({"name": name, "version": "0.0.0"}))
    done = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("sdk_setup.py")),
            str(tmp_path),
            "--check",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert done.returncode == 2
    assert "The installed package must be" in done.stderr
    assert json.loads(package.read_text())["version"] == "0.0.0"
