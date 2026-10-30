import json
import shutil
import socket
import subprocess
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from supervisor_support import _fake_installation, registry, roster_home

from nspawn import child, claude_child, picker, supervisor
from nspawn.mcp import McpServer
from nspawn.owner_roster import Roster

__all__ = ["registry", "roster_home"]

READER_TOOLS = ["Glob", "Grep", "Read"]


def _forbidden_effect(*args, **kwargs):
    pytest.fail("the hermetic reader test reached a real process or network port")


def _flag_values(argv, flag):
    values = []
    for index, argument in enumerate(argv):
        if argument == flag and index + 1 < len(argv):
            values.append(argv[index + 1])
        elif argument.startswith(flag + "="):
            values.append(argument.partition("=")[2])
    return values


def _granted(argv, flag):
    """The sorted tool names that one launch option grants, without patterns."""
    names = []
    for value in _flag_values(argv, flag):
        for name in value.replace(",", " ").split():
            names.append(name.partition("(")[0])
    return sorted(names)


def _settings(argv):
    """Each settings document that a launch passes to the client."""
    documents = []
    for value in _flag_values(argv, "--settings"):
        text = value if value.lstrip().startswith("{") else Path(value).read_text()
        documents.append(json.loads(text))
    return documents


@pytest.fixture
def reader_launch(registry, tmp_path, roster_home, monkeypatch):
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    account_home = tmp_path / "account-home"
    account_home.mkdir(mode=0o700)
    binary = tmp_path / "native-client"
    binary.write_text("not an executable test model\n")
    binary.chmod(0o700)

    plugin = tmp_path / "reader-plugin"
    (plugin / "agents").mkdir(parents=True)
    (plugin / "agents" / "worker.md").write_text(
        "---\nname: worker\ndescription: Test worker.\n---\n\nDo the brief.\n"
    )
    (plugin / "agents" / "reader.md").write_text(
        "---\nname: reader\ndescription: Test reader.\ntools: Read, Grep, Glob\n"
        "---\n\nRead the brief.\n"
    )
    (plugin / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"nmemory": {"command": "nmemory"}}})
    )

    secrets = tmp_path / "secrets"
    secrets.mkdir(mode=0o700)
    token = secrets / "writer-a.token"
    token.write_text("sk" + "-ant-oat01-reader-test-0123456789\n")
    token.chmod(0o600)
    roster_path = roster_home / ".nott" / "spawn.toml"

    monkeypatch.setenv("HOME", str(roster_home))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.delenv(child.MEMORY_POLICY_ENV, raising=False)
    monkeypatch.delenv(supervisor.CLAUDE_BIN_ENV, raising=False)
    monkeypatch.setattr(subprocess, "Popen", _forbidden_effect)
    monkeypatch.setattr(socket.socket, "connect", _forbidden_effect)
    monkeypatch.setattr(socket.socket, "connect_ex", _forbidden_effect)
    monkeypatch.setattr(claude_child, "inspect_claude_installation", _fake_installation)
    monkeypatch.setattr(Roster, "refuse_unservable_spawn", lambda *args: None)

    original_mkdtemp = tempfile.mkdtemp
    original_which = shutil.which

    def local_mkdtemp(suffix=None, prefix=None, dir=None):
        return original_mkdtemp(suffix=suffix, prefix=prefix, dir=tmp_path)

    monkeypatch.setattr(tempfile, "mkdtemp", local_mkdtemp)

    class PaperScope:
        """A slice with no cgroup behind it: the call site is judged, not systemd."""

        def __init__(self, *, unit, **kwargs):
            self.unit = unit
            self.path = tmp_path / "paper-scopes" / unit
            self.path.mkdir(parents=True, exist_ok=True)
            (self.path / "cgroup.events").write_text("populated 0\n")

        @classmethod
        def recorded(cls, unit):
            return cls(unit=unit)

        def command(self, argv):
            return [f"--slice={self.unit}", *argv]

        def environment(self, env):
            return dict(env)

        def drain(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(supervisor, "AggregateScope", PaperScope)

    starts = []
    workers = []

    def pick(roster, now, **kwargs):
        provider = next(p for p in roster.providers if p.name == "writer")
        return picker.PickerResult(
            kwargs.get("requested_provider"),
            provider,
            "writer-a",
            "fixture-model",
            None,
            None,
        )

    def which(command, mode=1, path=None):
        if command == "bwrap":
            return str(binary)
        return original_which(command, mode=mode, path=path)

    def spawn(argv, cwd, env, run_dir, needles, **kwargs):
        scope = kwargs.get("scope")
        starts.append(
            SimpleNamespace(
                argv=list(argv),
                command=list(argv) if scope is None else scope.command(list(argv)),
                cwd=Path(cwd),
                env=dict(env),
                settings=_settings(argv),
                recorded=registry.writer_scope(Path(run_dir).name),
            )
        )
        raise child.ChildSpawnError(str(argv[0]), "captured test launch")

    monkeypatch.setattr(picker, "pick_execution", pick)
    monkeypatch.setattr(shutil, "which", which)
    monkeypatch.setattr(child, "spawn_child", spawn)
    monkeypatch.setattr(
        supervisor, "start_worker", lambda call, worker: workers.append(worker)
    )

    def launch(tool, endpoint=None):
        proxy = (
            ""
            if endpoint is None
            else f'[provider.env]\nANTHROPIC_BASE_URL = "{endpoint}"\n'
        )
        roster_path.write_text(
            f"[defaults]\nplugins = [{json.dumps(str(plugin))}]\n"
            f"[bins]\nclaude = {json.dumps(str(binary))}\n"
            '[[provider]]\nname = "writer"\nkind = "claude"\n'
            'model = "fixture-model"\nmodels = ["fixture-model"]\n'
            f"secrets = {json.dumps(str(secrets))}\n{proxy}"
            "[provider.accounts.writer-a]\n"
            f"home = {json.dumps(str(account_home))}\n"
            'auth_env = "CLAUDE_CODE_OAUTH_TOKEN"\n'
        )
        sv = supervisor.Supervisor(registry, tmp_path / "runs")
        server = McpServer(supervisor=sv)
        start = sv.review if tool == "nspawn_review_start" else sv.execute

        def dispatch(name, arguments):
            assert name == tool, f"the server dispatched {name!r}, not {tool!r}"
            options = {
                key: value
                for key, value in arguments.items()
                if key not in ("idempotency_key", "group")
            }
            return {"run_id": start(**options).run_id}

        monkeypatch.setattr(server, "_dispatched", dispatch)
        try:
            if tool == "nspawn_review_start":
                answer = server.nspawn_review_start(
                    branch="lane",
                    cwd=str(checkout),
                    provider="writer-a",
                    prompt="review it",
                    agent="reader",
                )
            else:
                answer = server.nspawn_run_start(
                    prompt="read it",
                    cwd=str(checkout),
                    provider="writer-a",
                    read_only=True,
                    agent="reader",
                    timeout_ms=10000,
                )
            run_id = answer["run_id"]
            while workers:
                workers.pop(0).run()
            status = sv.status(run_id)
        finally:
            sv.shutdown()
        if len(starts) != 1:
            pytest.fail(
                f"the run made {len(starts)} native launches, not one: "
                f"{status.failure_reason!r} {status.warning!r}"
            )
        return SimpleNamespace(run_id=run_id, launched=starts[0])

    return launch


def test_a_read_only_run_gets_the_tools_of_its_agent_file_only(reader_launch):
    launched = reader_launch("nspawn_run_start").launched

    assert _granted(launched.argv, "--tools") == READER_TOOLS
    assert set(_granted(launched.argv, "--allowedTools")) <= set(READER_TOOLS)


def test_a_review_gets_the_tools_of_its_agent_file_and_no_bash(reader_launch):
    launched = reader_launch("nspawn_review_start").launched

    assert _granted(launched.argv, "--tools") == READER_TOOLS
    assert "Bash" not in _granted(launched.argv, "--allowedTools")


def test_a_review_starts_with_no_bwrap_settings(reader_launch):
    launched = reader_launch("nspawn_review_start").launched

    sandboxed = [document for document in launched.settings if "sandbox" in document]
    assert sandboxed == [], "the review launch carries settings for the bwrap sandbox"


def test_a_review_on_a_public_route_starts_in_its_own_run_slice(reader_launch):
    seen = reader_launch("nspawn_review_start", endpoint="https://proxy.invalid")
    launched = seen.launched
    unit = f"nottcontroller-{uuid.UUID(seen.run_id).hex}.slice"

    slices = [part for part in launched.command if part.startswith("--slice=")]
    assert slices == [f"--slice={unit}"], "the review is in no run slice of its own"
    assert launched.recorded is not None
    assert launched.recorded[0] == unit
