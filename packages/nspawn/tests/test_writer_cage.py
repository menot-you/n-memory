import copy
import io
import json
import os
import runpy
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from supervisor_support import _fake_installation, registry, roster_home

from nspawn import agy, app_server, child, claude_child, native, picker, supervisor
from nspawn.errors import NSpawnError
from nspawn.mcp import McpServer
from nspawn.owner_roster import NativeAccount, Roster
from nspawn.process_identity import ProcessIdentity
from nspawn.redact import SecretNeedles
from nspawn.rows import NativeSessionContext

__all__ = ["registry", "roster_home"]


def _flag_values(argv, flag):
    values = []
    for index, argument in enumerate(argv):
        if argument == flag:
            assert index + 1 < len(argv), (
                f"the writer launch option {flag} must have a value: {argv!r}"
            )
            values.append(argv[index + 1])
        elif argument.startswith(flag + "="):
            values.append(argument.partition("=")[2])
    return values


def _plugin_dirs(argv):
    return [Path(value) for value in _flag_values(argv, "--plugin-dir")]


def _forbidden_effect(*args, **kwargs):
    pytest.fail("the hermetic writer test reached a real process or network port")


def _guard_result(script, payload):
    stdout = io.StringIO()
    stderr = io.StringIO()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        patch.setattr(sys, "stdout", stdout)
        patch.setattr(sys, "stderr", stderr)
        patch.setattr(subprocess, "run", _forbidden_effect)
        namespace = runpy.run_path(str(script))
        exit_code = namespace["main"]()
    output = stdout.getvalue().strip()
    decision = (
        json.loads(output).get("hookSpecificOutput", {}).get("permissionDecision")
        if output
        else None
    )
    return exit_code, decision


def _bash_refusals(plugins, payload):
    """The fake native client dispatches only the fixture's registered hook."""
    results = []
    for plugin in plugins:
        manifest = plugin / "hooks" / "hooks.json"
        if not manifest.is_file():
            continue
        document = json.loads(manifest.read_text())
        for group in document.get("hooks", {}).get("PreToolUse", []):
            if group.get("matcher") != "Bash":
                continue
            for hook in group.get("hooks", []):
                command = hook.get("command", "")
                if hook.get("type") == "command" and command.endswith(
                    '/hooks/release-guard.py"'
                ):
                    script = Path(
                        command.strip('"').replace("${CLAUDE_PLUGIN_ROOT}", str(plugin))
                    )
                    results.append(_guard_result(script, payload))
    return results


@pytest.fixture
def writer_launch(registry, tmp_path, roster_home, monkeypatch):
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    account_home = tmp_path / "account-home"
    account_home.mkdir(mode=0o700)
    binary = tmp_path / "native-client"
    binary.write_text("not an executable test model\n")
    binary.chmod(0o700)

    plugin = tmp_path / "writer-plugin"
    (plugin / "agents").mkdir(parents=True)
    (plugin / "agents" / "worker.md").write_text(
        "---\nname: worker\ndescription: Test worker.\n---\n\nDo the brief.\n"
    )
    hooks = plugin / "hooks"
    hooks.mkdir()
    source_hooks = Path(__file__).resolve().parents[3] / "plugin" / "hooks"
    source_manifest = json.loads((source_hooks / "hooks.json").read_text())
    command = '"${CLAUDE_PLUGIN_ROOT}/hooks/release-guard.py"'
    hook_groups = []
    for group in source_manifest["hooks"]["PreToolUse"]:
        selected = [
            hook
            for hook in group["hooks"]
            if hook.get("type") == "command" and hook.get("command") == command
        ]
        if selected:
            hook_groups.append({**group, "hooks": selected})
    (hooks / "hooks.json").write_text(
        json.dumps({"hooks": {"PreToolUse": hook_groups}})
    )
    (hooks / "release-guard.py").write_bytes(
        (source_hooks / "release-guard.py").read_bytes()
    )
    (hooks / "release-guard.py").chmod(0o700)
    (plugin / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "nmemory": {"command": "must-not-start-nmemory"},
                    "nspawn": {"command": "must-not-start-nspawn"},
                }
            }
        )
    )

    secrets = tmp_path / "secrets"
    secrets.mkdir(mode=0o700)
    token = secrets / "writer-a.token"
    token.write_text("sk" + "-ant-oat01-writer-test-0123456789\n")
    token.chmod(0o600)
    roster_path = roster_home / ".nott" / "spawn.toml"

    monkeypatch.setenv("HOME", str(roster_home))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("WRITER_CANARY", "inherited-not-secret")
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
        def __init__(self, *, unit, **kwargs):
            self.unit = unit
            self.path = tmp_path / "paper-scopes" / unit
            self.path.mkdir(parents=True, exist_ok=True)
            (self.path / "cgroup.events").write_text("populated 0\n")

        @classmethod
        def recorded(cls, unit):
            return cls(unit=unit)

        def command(self, argv):
            return list(argv)

        def environment(self, env):
            return dict(env)

        def drain(self):
            pass

        def close(self):
            pass

    class ExitedChild:
        def __init__(self):
            self.identity = ProcessIdentity(
                pid=424242, boot_id="writer-cage-fixture", start_ticks=1
            )
            self.stdout_pump = SimpleNamespace(finished=True)
            self.stderr_pump = SimpleNamespace(finished=True)

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

        def write_stdin(self, payload, timeout=None):
            pass

        def close_stdin(self):
            pass

        def stop_then_kill(self, expected):
            assert expected == self.identity, (
                "native writer cleanup must target the captured child identity"
            )

    monkeypatch.setattr(supervisor, "AggregateScope", PaperScope)

    def launch(kind="claude"):
        state = SimpleNamespace(
            kind=kind,
            checkout=checkout,
            plugin=plugin,
            starts=[],
            agy_starts=[],
            thread_starts=[],
            error=None,
            run_id=None,
        )
        roster_path.write_text(
            f"[defaults]\nplugins = [{json.dumps(str(plugin))}]\n"
            f"[bins]\n{kind} = {json.dumps(str(binary))}\n"
            '[[provider]]\nname = "writer"\n'
            f"kind = {json.dumps(kind)}\n"
            'model = "fixture-model"\nmodels = ["fixture-model"]\n'
            f"secrets = {json.dumps(str(secrets))}\n"
            "[provider.accounts.writer-a]\n"
            f"home = {json.dumps(str(account_home))}\n"
            'auth_env = "CLAUDE_CODE_OAUTH_TOKEN"\n'
            "[native_write]\n"
            f"roots = [{json.dumps(str(tmp_path))}]\n"
        )
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
                return None if kind == "agy" else str(binary)
            return original_which(command, mode=mode, path=path)

        def prepare(provider, alias, **kwargs):
            return native.NativeLaunch(
                argv=(str(binary), "app-server", "--listen", "stdio://"),
                env={
                    "PATH": str(tmp_path),
                    "HOME": str(account_home),
                    "CODEX_HOME": str(account_home),
                },
                needles=SecretNeedles.none(),
                context=NativeSessionContext(
                    str(account_home), read_roots=(str(checkout),)
                ),
                account=NativeAccount(account_home),
            )

        def spawn(argv, cwd, env, run_dir, needles, **kwargs):
            settings = []
            for value in _flag_values(argv, "--settings"):
                settings.append(
                    json.loads(value)
                    if value.lstrip().startswith("{")
                    else json.loads(Path(value).read_text())
                )
            state.starts.append(
                SimpleNamespace(
                    argv=list(argv),
                    cwd=Path(cwd),
                    env=dict(env),
                    settings=settings,
                )
            )
            if kind == "claude":
                raise child.ChildSpawnError(str(argv[0]), "captured test launch")
            return ExitedChild()

        def rpc(turn, method, params, **kwargs):
            if method == "initialize":
                return {}
            if method in ("thread/start", "thread/resume"):
                state.thread_starts.append(copy.deepcopy(params))
                raise app_server.AppServerError("captured test thread start")
            pytest.fail(f"unexpected fake app-server RPC: {method}")

        def agy_start(argv, **kwargs):
            state.agy_starts.append(
                SimpleNamespace(
                    argv=list(argv),
                    cwd=Path(kwargs["cwd"]),
                    env=dict(kwargs["env"]),
                )
            )
            raise agy.AgyError("captured test AGY start")

        monkeypatch.setattr(picker, "pick_execution", pick)
        monkeypatch.setattr(shutil, "which", which)
        monkeypatch.setattr(native, "prepare_native", prepare)
        monkeypatch.setattr(child, "spawn_child", spawn)
        monkeypatch.setattr(
            supervisor, "start_worker", lambda call, worker: workers.append(worker)
        )
        monkeypatch.setattr(
            supervisor.Supervisor,
            "_native_readonly_mcp",
            lambda *args, **kwargs: ({}, {}),
        )
        monkeypatch.setattr(app_server.AppServerTurn, "_call", rpc)
        monkeypatch.setattr(
            app_server.AppServerTurn, "_send", lambda *args, **kwargs: None
        )
        monkeypatch.setattr(agy.AgyTurn, "start", staticmethod(agy_start))

        sv = supervisor.Supervisor(registry, tmp_path / "runs")
        server = McpServer(supervisor=sv)

        def dispatch(tool, arguments):
            assert tool == "nspawn_run_start", (
                f"writer admission must dispatch nspawn_run_start, not {tool!r}"
            )
            # These two keys belong to driver transport, not Supervisor.execute.
            execute_arguments = {
                key: value
                for key, value in arguments.items()
                if key not in ("idempotency_key", "group")
            }
            accepted = sv.execute(**execute_arguments)
            return {"run_id": accepted.run_id}

        monkeypatch.setattr(server, "_dispatched", dispatch)
        try:
            try:
                answer = server.nspawn_run_start(
                    prompt="inspect the writer launch",
                    cwd=str(checkout),
                    provider="writer-a",
                    execution_class="verification",
                    native_write=True,
                    timeout_ms=10000,
                )
            except NSpawnError as error:
                state.error = str(error)
            else:
                state.run_id = answer["run_id"]
                # Retain admission's real worker choice without starting threads.
                while workers:
                    workers.pop(0).run()
        finally:
            sv.shutdown()
        return state

    return launch


def test_claude_writer_starts_uncaged_in_its_clone(writer_launch):
    seen = writer_launch()
    assert len(seen.starts) == 1, (
        f"a Claude writer must reach one native launch: {seen.error}"
    )
    launched = seen.starts[0]
    assert (
        launched.cwd,
        "--restricted" in launched.argv,
        _flag_values(launched.argv, "--settings"),
        launched.settings,
        launched.env.get("WRITER_CANARY"),
    ) == (
        seen.checkout,
        False,
        [],
        [],
        "inherited-not-secret",
    ), "a Claude writer must use its clone, ordinary settings and inherited environment"


@pytest.mark.parametrize(
    ("ambient", "expected"),
    [
        (None, "/var/tmp"),
        ("/tmp", "/var/tmp"),
        ("/var/tmp/../../tmp", "/var/tmp"),
        ("/var/tmp/kept-by-the-caller", "/var/tmp/kept-by-the-caller"),
    ],
)
def test_claude_writer_gets_a_tmpdir_under_var_tmp(
    writer_launch, monkeypatch, ambient, expected
):
    """On SERVER, /tmp is a tmpfs with a fixed inode limit. The writer loads no
    project settings, so the driver sets its TMPDIR: a TMPDIR that is not under
    /var/tmp is replaced, and one that is stays."""
    if ambient is None:
        monkeypatch.delenv("TMPDIR")
    else:
        monkeypatch.setenv("TMPDIR", ambient)
    seen = writer_launch()
    assert len(seen.starts) == 1, (
        f"a Claude writer must reach one native launch: {seen.error}"
    )
    assert seen.starts[0].env.get("TMPDIR") == expected


def test_writer_tmpdir_is_absolute_when_the_ambient_value_is_relative(
    writer_launch, monkeypatch, tmp_path
):
    """The writer runs in its clone, not in the driver's directory. A relative
    TMPDIR that the driver checked against its own directory would reach a
    different place there, and `../../tmp` could reach /tmp."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TMPDIR", "kept-under-var-tmp")
    seen = writer_launch()
    assert len(seen.starts) == 1, (
        f"a Claude writer must reach one native launch: {seen.error}"
    )
    assert seen.starts[0].env.get("TMPDIR") == str(tmp_path / "kept-under-var-tmp")


def test_writer_tmpdir_replaces_a_relative_value_that_leaves_var_tmp(
    monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    assert child.writer_tmpdir(os.path.relpath("/tmp", tmp_path)) == "/var/tmp"


def test_writer_loads_plugin_hooks_without_nspawn_mcp(writer_launch):
    seen = writer_launch()
    assert len(seen.starts) == 1, (
        f"a Claude writer must reach one native launch to load hooks: {seen.error}"
    )
    plugins = _plugin_dirs(seen.starts[0].argv)
    assert plugins, "the writer launch loaded no plugin hooks"

    # The projected hooks run from the source directory, so a hook finds its repository.
    source_hooks = (seen.plugin / "hooks").resolve()
    expected_manifest = json.loads(
        (source_hooks / "hooks.json")
        .read_text()
        .replace("${CLAUDE_PLUGIN_ROOT}/hooks", str(source_hooks))
    )
    expected_guard = (source_hooks / "release-guard.py").read_bytes()
    loaded_hooks = []
    servers = set()
    for plugin in plugins:
        manifest = plugin / "hooks" / "hooks.json"
        if manifest.is_file() and json.loads(manifest.read_text()) == expected_manifest:
            guard = plugin / "hooks" / "release-guard.py"
            loaded_hooks.append(
                guard.is_file() and guard.read_bytes() == expected_guard
            )
        config = plugin / ".mcp.json"
        if config.is_file():
            servers.update(json.loads(config.read_text()).get("mcpServers", {}))

    assert any(loaded_hooks), "the loaded plugins lost the registered Bash guard"
    assert "nspawn" not in servers, "a writer plugin still declares nspawn MCP"


def test_writer_denies_the_nspawn_mcp_tools_of_every_scope(writer_launch):
    """A plugin `.mcp.json` without `nspawn` does not stop an nSPAWN server that
    the account home registers at user scope: a plugin dir lifts
    `--strict-mcp-config`. The writer launch denies the tools of both."""
    seen = writer_launch()
    assert len(seen.starts) == 1, (
        f"a Claude writer must reach one native launch: {seen.error}"
    )
    denied = {
        name
        for value in _flag_values(seen.starts[0].argv, "--disallowedTools")
        for name in value.split(",")
    }
    assert {"mcp__plugin_nott_nspawn__*", "mcp__nspawn__*"} <= denied


@pytest.mark.parametrize("flag", ["--disallowedTools", "--disallowed-tools=Read"])
def test_roster_cannot_replace_the_writer_nspawn_deny(flag):
    with pytest.raises(child.ChildEnvironmentError):
        child.compose_child_argv(
            Path("/opt/native-client"), declared_args=[flag, "Read"]
        )


def test_writer_keeps_a_real_plugin_guard_refusal(writer_launch):
    seen = writer_launch()
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": 'gh pr merge "$PR"'},
        "cwd": str(seen.checkout),
    }
    baseline = _bash_refusals([seen.plugin], payload)
    assert baseline == [(2, "deny")], "the real fixture guard must refuse the command"
    assert len(seen.starts) == 1, (
        f"a Claude writer must reach one native launch to retain guard refusals: {seen.error}"
    )

    # Substitute only native hook dispatch; execute the same real guard code.
    writer_results = _bash_refusals(_plugin_dirs(seen.starts[0].argv), payload)
    assert (2, "deny") in writer_results, (
        "the writer lost a refusal made by its registered plugin guard"
    )


@pytest.mark.parametrize("kind", ["codex", "agy"])
def test_native_writer_starts_without_an_adapter_writer_profile(writer_launch, kind):
    seen = writer_launch(kind)
    if kind == "agy":
        assert len(seen.agy_starts) == 1, (
            "an AGY writer must reach native start without bubblewrap",
            seen.error,
        )
        assert not seen.thread_starts, "AGY must not enter Codex profile composition"
        launched = seen.agy_starts[0]
        assert launched.cwd == seen.checkout, "an AGY writer must start in its checkout"
        assert not any(Path(arg).name == "bwrap" for arg in launched.argv), (
            "an AGY writer must launch without a bubblewrap cage"
        )
    else:
        assert len(seen.thread_starts) == 1, (
            f"a Codex writer must reach thread/start composition: {seen.error}"
        )
        params = seen.thread_starts[0]
        config = params.get("config", {})
        assert (
            params.get("permissions"),
            config.get("permissions"),
            config.get("default_permissions"),
        ) == (None, None, None), (
            "native_write still installs the app-server adapter's writer profile"
        )
