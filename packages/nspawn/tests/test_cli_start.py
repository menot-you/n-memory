# `nspawn run start` and `nspawn review start`: a start through the installed door.
#
# A Claude Code session keeps the tool list of the nSPAWN release that was
# installed when its door started, and a subagent keeps the tool list of its own
# spawn. When that list has no `notice` field, the client sends
# `"notice": "false"` as a string and the door refuses it. These commands read
# the tool's arguments as one JSON object on stdin and call the tool through a
# new door of this runtime, so a side-unit owner can start a run with
# `notice: false` whatever list it holds.

import io
import json
import sys
from pathlib import Path

import pytest
from registry_reset import sql
from supervisor_support import _drain_worker_threads, _stop_every_reaper, registry

from nspawn import cli, driver
from nspawn.client import NspawnClientError, NspawnRefused

__all__ = ["_drain_worker_threads", "_stop_every_reaper", "registry"]

START_TOOLS = {"run": "nspawn_run_start", "review": "nspawn_review_start"}


def _stdin(monkeypatch, text):
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))


def _fake_client(monkeypatch, answer=None, error=None):
    """Replace the door client; return the list that records each door and call."""
    seen = []

    class FakeClient:
        def __init__(self, server, cwd, deadline_s=None, **options):
            seen.append(
                {
                    "server": server,
                    "cwd": Path(cwd),
                    "deadline_s": deadline_s,
                    "options": options,
                    "calls": [],
                }
            )

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def call(self, tool, arguments):
            seen[-1]["calls"].append((tool, arguments))
            if error is not None:
                raise error
            return answer

    monkeypatch.setattr(cli, "NspawnClient", FakeClient)
    return seen


def _no_client(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("an input refusal must come before the door starts")

    monkeypatch.setattr(cli, "NspawnClient", forbidden)


@pytest.mark.parametrize("noun", ["run", "review"])
def test_cs01_start_sends_the_stdin_arguments_to_the_installed_door(
    noun, tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    arguments = {
        "cwd": str(tmp_path),
        "prompt": "p",
        "provider": "gpt",
        "notice": False,
    }
    if noun == "run":
        arguments.update({"class": "review", "read_only": True})
    answer = {"run_id": "run-x", "deduplicated": False, "phase": "spawning"}
    seen = _fake_client(monkeypatch, answer=answer)
    _stdin(monkeypatch, json.dumps(arguments))
    capsys.readouterr()

    code = cli.main([noun, "start"])
    out, err = capsys.readouterr()

    assert code == 0, err
    assert json.loads(out) == answer, "stdout is not the door's answer as JSON"
    assert len(seen) == 1, "the start did not open exactly one door"
    door = seen[0]
    assert door["calls"] == [(START_TOOLS[noun], arguments)], (
        "the door did not get the stdin arguments unchanged"
    )
    notice = door["calls"][0][1]["notice"]
    assert notice is False, "notice reached the door as something other than false"
    server = door["server"]
    assert server.command == sys.executable, "the door is not this runtime"
    assert list(server.args[-4:]) == ["-m", "nspawn.cli", "mcp", "serve"], (
        "the door is not this runtime's `nspawn mcp serve`"
    )
    assert door["cwd"].resolve() == tmp_path.resolve(), (
        "the door does not run in the caller's directory, so its project differs"
    )
    deadline = door["deadline_s"]
    if deadline is None:
        deadline = door["options"].get("deadline_s")
    assert deadline is not None and deadline > driver.ACK_WAIT_S, (
        "the call deadline is shorter than the driver's acknowledgement wait"
    )


@pytest.mark.parametrize(
    "argv,text",
    [
        (["run", "start", "extra"], "{}"),
        (["run", "start"], "not json"),
        (["run", "start"], "[1]"),
        (["run", "start"], ""),
        (["review", "start"], "null"),
    ],
)
def test_cs02_a_start_refuses_bad_input_before_a_door_starts(
    argv, text, monkeypatch, capsys
):
    _no_client(monkeypatch)
    _stdin(monkeypatch, text)
    capsys.readouterr()

    code = cli.main(argv)
    out, err = capsys.readouterr()

    assert code == 2
    assert out == ""
    assert "unknown command" not in err, "nspawn has no start command"
    assert " ".join(argv[:2]) in err, "the refusal does not name its command"


def test_cs03_a_refused_start_names_the_refusal_and_its_launch(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    arguments = {"cwd": str(tmp_path), "prompt": "p", "class": "review"}
    refusal = NspawnRefused(
        "nspawn_run_start",
        "the driver did not acknowledge the launch",
        {"type": "AdmissionPending", "launch_uuid": "launch-4711"},
    )
    _fake_client(monkeypatch, error=refusal)
    _stdin(monkeypatch, json.dumps(arguments))
    capsys.readouterr()

    code = cli.main(["run", "start"])
    out, err = capsys.readouterr()

    assert code == 2
    assert out == ""
    assert "the driver did not acknowledge the launch" in err
    assert "launch-4711" in err, "the refusal hides the launch that run show resolves"

    _fake_client(monkeypatch, error=NspawnClientError("the peer closed its pipe"))
    _stdin(monkeypatch, json.dumps(arguments))

    code = cli.main(["run", "start"])
    out, err = capsys.readouterr()

    assert code == 1, "a door failure is not exit 1"
    assert out == ""
    assert "the peer closed its pipe" in err


@pytest.fixture
def cs_run_root(tmp_path, monkeypatch):
    """The run root of the door: this test's, never the live one."""
    root = tmp_path / "runs"
    root.mkdir(mode=0o700)
    monkeypatch.setenv("NOTT_NSPAWN_RUN_ROOT", str(root))
    return root


def test_cs04_the_installed_door_refuses_a_string_notice_and_starts_no_run(
    registry, cs_run_root, tmp_path, monkeypatch, capsys
):
    # The caller's directory holds an `nspawn` package. The door must not run it.
    caller = tmp_path / "caller"
    planted = caller / "nspawn"
    planted.mkdir(parents=True)
    (planted / "__init__.py").write_text("raise SystemExit(97)\n", encoding="utf-8")
    (planted / "cli.py").write_text("raise SystemExit(97)\n", encoding="utf-8")
    monkeypatch.chdir(caller)
    arguments = {
        "cwd": str(tmp_path),
        "prompt": "p",
        "class": "review",
        "read_only": True,
        "notice": "false",
    }
    _stdin(monkeypatch, json.dumps(arguments))
    capsys.readouterr()

    code = cli.main(["run", "start"])
    out, err = capsys.readouterr()

    assert code == 2, err
    assert out == ""
    assert "argument 'notice' must satisfy type: 'boolean'" in err, (
        "the installed door did not refuse the string"
    )
    runs = sql(registry, "SELECT count(*) AS n FROM runs").fetchone()
    assert runs["n"] == 0, "a refused start left a run"
