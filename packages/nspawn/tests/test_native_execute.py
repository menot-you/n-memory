"""Native execution and resume through real MCP stdio, PostgreSQL and child pipes."""

import json
import os
import re
import selectors
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import nott_database
import pytest
from class_dispatch import DECLARED_CLASS
from host_process import live_processes_running
from registry_reset import store


def _store_rows(statement: str, params: tuple[object, ...] = ()) -> list[Any]:
    """Rows of this test's registry file, read on a connection of their own."""
    conn = nott_database.connect(store(), readonly=True)
    try:
        return nott_database.query(conn, statement, params)
    finally:
        conn.close()


def _store_write(statement: str, params: tuple[object, ...] = ()) -> None:
    conn = nott_database.connect(store())
    try:
        with nott_database.write(conn):
            conn.execute(statement, params).close()
    finally:
        conn.close()


def _usage(run_id: str) -> list[dict[str, Any]]:
    return [
        json.loads(row[0])
        for row in _store_rows(
            "SELECT payload FROM run_events WHERE run_id = ? AND kind = 'usage'",
            (run_id,),
        )
    ]


VENDOR = r"""
import json, os, subprocess, sys, time
from pathlib import Path
def send(value):
    print(json.dumps(value), flush=True)
def artifact(name):
    return Path(os.environ['TMPDIR']) / name if KIND == 'agy' else Path(name)
INVENTORY = sys.argv[-3:] == ['mcp', 'list', '--json']
with artifact('launches.log').open('a') as log:
    log.write(json.dumps(['start', 'inventory' if INVENTORY else 'app', time.monotonic()]) + '\n')
if INVENTORY:
    if Path('slow-inventory').exists():
        time.sleep(0.3)
    catalog = Path('mcp-catalog.json')
    print(catalog.read_text() if catalog.exists() else '[]', flush=True)
    print('synthetic-inventory-only-secret', file=sys.stderr, flush=True)
    with artifact('launches.log').open('a') as log:
        log.write(json.dumps(['end', 'inventory', time.monotonic()]) + '\n')
    raise SystemExit(0)
artifact('vendor-pid').write_text(str(os.getpid()))
def answer(prompt):
    if prompt == 'echo fixture MCP credential':
        print(MCP_SECRET, file=sys.stderr, flush=True)
        return MCP_SECRET
    if prompt == 'wait for close':
        descendant = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        artifact('vendor-tree.json').write_text(json.dumps({'parent':os.getpid(), 'child':descendant.pid}))
        time.sleep(30)
    if prompt == 'wait for deadline':
        time.sleep(30)
    if prompt == 'rotate fixture credential':
        if KIND == 'agy':
            artifact('rotation-ready').touch()
            while not artifact('rotation-empty').exists(): time.sleep(0.01)
            print('synthetic torn-window line', file=sys.stderr, flush=True)
            artifact('rotation-torn-emitted').touch()
            while not artifact('rotation-done').exists(): time.sleep(0.01)
            value = 'synthetic-native-access-token synthetic-rotated-access-token'
            print(json.dumps({'fixture': value}), file=sys.stderr, flush=True)
            return value
        home = Path(os.environ.get('CODEX_HOME', os.environ['HOME']))
        auth = home / ('auth.json' if KIND == 'codex' else '.gemini/antigravity-cli/antigravity-oauth-token')
        # A vendor CLI rewrites its token in place (truncate, then write), and nSPAWN
        # reads it while the file is empty: a line emitted inside that window makes
        # the torn read deterministic instead of a scheduling accident. The window
        # (200 ms) outlasts the pauses without their last one (150 ms) and fits
        # inside all of them (310 ms): a pause budget cut to 150 ms refuses.
        with auth.open('w') as torn:
            print('synthetic torn-window line', file=sys.stderr, flush=True)
            time.sleep(0.2)
            torn.write(json.dumps({'tokens' if KIND == 'codex' else 'token': {'access_token': 'synthetic-rotated-access-token'}}))
        value = 'synthetic-native-access-token synthetic-rotated-access-token'
        print(json.dumps({'fixture': value}), file=sys.stderr, flush=True)
        return value
    return 'PONG'
if KIND == 'codex':
    for line in sys.stdin:
        frame = json.loads(line)
        method = frame.get('method')
        params = frame.get('params', {})
        if method == 'initialize':
            send({'id': frame['id'], 'result': {'userAgent': 'native-fixture', 'codexHome': 'wrong-home' if Path('wrong-quota-home').exists() else os.environ['CODEX_HOME']}})
        elif method == 'account/rateLimits/read':
            assert params is None
            Path('quota-read-called').touch()
            account_id = 'unexpected-account' if Path('wrong-quota-account').exists() else None
            send({'id':frame['id'],'result':{'accountId':account_id,'rateLimits':{'limitId':'codex','primary':{'usedPercent':25,'windowDurationMins':10080,'resetsAt':4102444800},'secondary':None},'rateLimitsByLimitId':None}})
        elif method == 'plugin/installed':
            Path('metadata-pid').write_text(str(os.getpid()))
            plugins = [{'id': 'extra@runtime', 'name': 'extra', 'remotePluginId': 'plugins_fixture', 'enabled': True, 'installed': True}]
            if Path('skill-plugin.json').exists():
                plugins.append(json.loads(Path('skill-plugin.json').read_text())['summary'])
            send({'id': frame['id'], 'result': {'marketplaces': [{'name': 'runtime', 'path': None, 'plugins': plugins}], 'marketplaceLoadErrors': [], 'private': 'synthetic-inventory-only-secret'}})
        elif method == 'plugin/read':
            if params.get('pluginName') == 'skills' and Path('skill-plugin.json').exists():
                send({'id': frame['id'], 'result': {'plugin': json.loads(Path('skill-plugin.json').read_text())}})
            elif params.get('pluginName') != 'plugins_fixture':
                send({'id': frame['id'], 'error': {'code': -32600, 'message': 'remote plugin slug returns 404'}})
            else:
                send({'id': frame['id'], 'result': {'plugin': {'summary': {'id': 'extra@runtime'}, 'mcpServers': ['runtime_extra']}, 'private': 'synthetic-inventory-only-secret'}})
        elif method in ('thread/start', 'thread/resume'):
            servers = params.get('config', {}).get('mcp_servers', {})
            MCP_SECRET = servers.get('nmemory', {}).get('env', {}).get('OPENAI_API_KEY')
            recorded = json.dumps(params)
            if MCP_SECRET:
                assert MCP_SECRET == 'synthetic-mcp-only-credential'
                assert 'OPENAI_API_KEY' not in os.environ
                assert all('OPENAI_API_KEY' not in json.dumps(server) for name, server in servers.items() if name != 'nmemory')
                assert MCP_SECRET not in ' '.join(sys.argv)
                Path('mcp-env-verified').touch()
                recorded = recorded.replace(MCP_SECRET, '[fixture-private]')
            Path('thread-request.json').write_text(recorded)
            if params.get('ephemeral'):
                Path('metadata-pid').write_text(str(os.getpid()))
            thread = params.get('threadId', 'native-thread')
            profile = params.get('config', {}).get('permissions', {}).get(params.get('permissions'), {})
            # What codex reports for a native write profile, unless the test plants another.
            writer = {'type': 'workspaceWrite', 'writableRoots': [], 'networkAccess': False,
                      'excludeTmpdirEnvVar': True, 'excludeSlashTmp': True}
            if Path('writer-sandbox.json').exists():
                writer = json.loads(Path('writer-sandbox.json').read_text())
            send({'id': frame['id'], 'result': {'thread': {'id': thread, 'ephemeral': params.get('ephemeral', False)},
                  'model': params['model'], 'modelProvider': params.get('modelProvider', 'openai'),
                  'sandbox': writer if 'write' in profile.get('filesystem', {}).values()
                  else {'type': 'readOnly' if params.get('permissions') or params.get('sandbox') == 'read-only' else 'workspaceWrite'},
                  'activePermissionProfile': {'id': 'unexpected-profile' if Path('wrong-read-profile').exists() else params['permissions'], 'extends': ':workspace' if Path('read-profile-parent').exists() else None} if params.get('permissions') else None}})
            count_path = Path(os.environ['CODEX_HOME']) / 'fixture-turn-count'
            if method == 'thread/resume' and not RESET_USAGE and count_path.exists():
                count = int(count_path.read_text())
                total = dict(zip(('inputTokens', 'outputTokens', 'totalTokens', 'cachedInputTokens', 'reasoningOutputTokens'),
                                 (18 * count, 7 * count, 25 * count, 7 * count, 3 * count)))
                send({'method': 'thread/tokenUsage/updated', 'params': {'threadId': thread,
                      'turnId': 'previous-turn', 'tokenUsage': {'total': total, 'last': total}}})
        elif method == 'mcpServerStatus/list':
            settings = json.loads(Path('thread-request.json').read_text()).get('config', {})
            config = settings.get('mcp_servers', {})
            data = [{'name': name, 'tools': {tool: {} for tool in entry.get('enabled_tools', ['fixture_write'])},
                     'resources': [], 'resourceTemplates': []}
                    for name, entry in config.items() if entry.get('enabled', True)]
            if not Path('lazy-plugin').exists() and settings.get('plugins', {}).get('extra@runtime', {}).get('mcp_servers', {}).get('runtime_extra', {}).get('enabled') is not False:
                data.append({'name': 'runtime_extra', 'pluginId': 'extra@runtime', 'tools': {'fixture_write': {}}, 'resources': [], 'resourceTemplates': []})
            if settings.get('features.apps') is not False:
                data.append({'name': 'codex_apps', 'tools': {'fixture_write': {}}, 'resources': [], 'resourceTemplates': []})
            if Path('mcp-catalog-drift').exists():
                data.append({'name': 'unexpected', 'tools': {'fixture_write': {}}, 'resources': [], 'resourceTemplates': []})
            ephemeral = json.loads(Path('thread-request.json').read_text()).get('ephemeral')
            cursor = None
            if Path('moving-catalog').exists() and not Path('catalog-moved').exists():
                memory = next(row for row in data if row['name'] == 'nmemory')
                data = [memory]
                cursor = '1'
                if params.get('cursor') == '1':
                    Path('catalog-moved').touch()
                    cursor = None
            if not ephemeral and Path('missing-final-memory').exists():
                data = []
            Path('mcp-status-checked').touch()
            private = {'private': 'synthetic-inventory-only-secret'} if ephemeral else {}
            send({'id': frame['id'], 'result': {'data': data, 'nextCursor': cursor, **private}})
        elif method == 'turn/start':
            settings = json.loads(Path('thread-request.json').read_text()).get('config', {})
            if Path('lazy-plugin').exists() and settings.get('plugins', {}).get('extra@runtime', {}).get('mcp_servers', {}).get('runtime_extra', {}).get('enabled') is not False:
                send({'method': 'mcpServer/startupStatus/updated', 'params': {'name': 'runtime_extra', 'status': 'starting'}})
            metadata = Path('metadata-pid')
            if metadata.exists():
                try: os.kill(int(metadata.read_text()), 0)
                except ProcessLookupError: pass
                else: raise RuntimeError('metadata process is still alive at model dispatch')
            Path('model-turn-started').touch()
            prompt = params['input'][0]['text']
            if prompt in ('quota fixture', 'quota malformed fixture', 'quota followed by absence fixture'):
                send({'method':'account/rateLimits/updated','params':{'rateLimits':{'limitId':'codex','primary':{'usedPercent':None if prompt == 'quota malformed fixture' else 25,'windowDurationMins':10080,'resetsAt':4102444800},'secondary':None}}})
                if prompt == 'quota followed by absence fixture':
                    send({'method':'account/rateLimits/updated','params':{'rateLimits':{'limitId':'codex','primary':None,'secondary':None}}})
            if Path('exhausted-window').exists():
                percent = int(Path('window-percent').read_text()) if Path('window-percent').exists() else 100
                send({'method':'account/rateLimits/updated','params':{'rateLimits':{'limitId':'codex','primary':{'usedPercent':percent,'windowDurationMins':300,'resetsAt':int(Path('exhausted-window').read_text())},'secondary':None}}})
            text = answer(prompt)
            failure = ({'message': 'fixture 401 text is not authoritative',
                        'codexErrorInfo': json.loads(prompt.removeprefix('fixture error '))}
                       if prompt.startswith('fixture error ') else
                       # What codex relayed for a model the account may not use
                       # (codex 0.157.1, 2026-09-28): the API's error body as text.
                       {'message': json.dumps({'type': 'error', 'status': 400, 'error': {
                            'type': 'invalid_request_error',
                            'message': "The 'test-model' model is not supported when using Codex with a ChatGPT account."}}),
                        'codexErrorInfo': 'other', 'additionalDetails': None}
                       if prompt == 'fixture vendor 400' else None)
            send({'id': frame['id'], 'result': {'turn': {'id': 'native-turn'}}})
            count_path = Path(os.environ['CODEX_HOME']) / 'fixture-turn-count'
            count = int(count_path.read_text()) + 1 if count_path.exists() else 1
            count_path.write_text(str(count))
            if RESET_USAGE:
                count = 1
            keys = ('inputTokens', 'outputTokens', 'totalTokens', 'cachedInputTokens', 'reasoningOutputTokens')
            per_turn = (18, 7, 25, 7, 3)
            for increment, last in (((6, 3, 9, 2, 1), (6, 3, 9, 2, 1)),
                                    (per_turn, (12, 4, 16, 5, 2)),
                                    (per_turn, (12, 4, 16, 5, 2))):
                total = {key: (count - 1) * base + value for key, base, value in zip(keys, per_turn, increment)}
                send({'method': 'thread/tokenUsage/updated', 'params': {'threadId': thread,
                      'turnId': 'native-turn', 'tokenUsage': {'total': total, 'last': dict(zip(keys, last))}}})
            send({'method': 'item/completed', 'params': {'threadId': thread,
                  'turnId': 'native-turn', 'item': {'id': 'answer', 'type': 'agentMessage',
                  'phase': 'final_answer', 'text': text}}})
            if Path('pause-before-complete').exists():
                Path('turn-paused').touch()
                while not Path('resume-turn').exists():
                    time.sleep(0.02)
            send({'method': 'turn/completed', 'params': {'threadId': thread,
                  'turn': {'id': 'native-turn', 'status': 'failed' if failure else 'completed', 'error': failure}}})
else:
    conversation = next((arg.split('=', 1)[1] for arg in sys.argv if arg.startswith('--conversation=')), 'native-conversation')
    send({'event': 'init', 'conversation_id': conversation,
          'init': {'cwd': os.getcwd(), 'tools': [], 'permission_mode': 'request-review'}})
    for line in sys.stdin:
        message = json.loads(line)
        assert message['event'] == 'user'
        assert isinstance(message['message']['content'], str)
        text = answer(message['message']['content'])
        canceled = message['message']['content'] == 'cancel fixture turn'
        count = int(os.environ['NOTT_NSPAWN_FIXTURE_ATTEMPT'])
        send({'event': 'result', 'result': {'conversation_id': conversation,
              'status': 'CANCELED' if canceled else 'SUCCESS', 'response': None if canceled else text, 'error': None,
              'usage': {'input_tokens': 2 * count, 'output_tokens': count, 'total_tokens': 3 * count,
                        'thinking_tokens': count, 'cache_read_tokens': 4 * count},
              'duration_seconds': 0.01 * count, 'num_turns': count}})
"""


class Client:
    def __init__(self, process, *, kind="codex", cwd=None, run_root=None):
        self.process = process
        self.kind, self.cwd, self.run_root = kind, cwd, run_root
        self.last_run = None
        self.rotations = set()
        self.sequence = 0
        self.buffer = b""
        self.selector = selectors.DefaultSelector()
        self.selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        self.selector.register(process.stderr, selectors.EVENT_READ, "stderr")

    def request(self, method, params):
        self.sequence += 1
        frame = {
            "jsonrpc": "2.0",
            "id": self.sequence,
            "method": method,
            "params": params,
        }
        self.process.stdin.write((json.dumps(frame) + "\n").encode())
        self.process.stdin.flush()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            while b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                response = json.loads(line)
                if response.get("id") == self.sequence:
                    return response
            for key, _ in self.selector.select(0.1):
                data = os.read(key.fileobj.fileno(), 65536)
                if not data:
                    self.selector.unregister(key.fileobj)
                    continue
                if key.data == "stdout":
                    self.buffer += data
                    assert len(self.buffer) <= 2 * 1024 * 1024
        pytest.fail("native MCP request did not answer within its deadline")

    def call(self, name, arguments):
        response = self.request("tools/call", {"name": name, "arguments": arguments})
        assert "error" not in response, response
        result = response["result"]
        assert not result.get("isError"), result
        decoded = json.loads(result["content"][0]["text"])
        if name == "nspawn_run_start":
            self.last_run = decoded["run_id"]
            if (
                self.kind == "agy"
                and arguments.get("prompt") == "rotate fixture credential"
            ):
                self.rotations.add(self.last_run)
        return decoded

    def observed(self, name, run_id=None):
        return (
            self.run_root / (run_id or self.last_run) / "scratch" / name
            if self.kind == "agy"
            else self.cwd / name
        )

    def rotate_owned_fixture_auth(self, run_id):
        """The fixture owner rotates its credential outside the read-only child."""
        if run_id not in self.rotations:
            return
        ready = self.observed("rotation-ready", run_id)
        if not ready.exists():
            return
        auth = self.cwd / "first/.gemini/antigravity-cli/antigravity-oauth-token"
        with auth.open("w") as torn:
            self.observed("rotation-empty", run_id).touch()
            deadline = time.monotonic() + 3
            while not self.observed("rotation-torn-emitted", run_id).exists():
                assert time.monotonic() < deadline
                time.sleep(0.01)
            time.sleep(0.2)
            torn.write(
                json.dumps(
                    {"token": {"access_token": "synthetic-rotated-access-token"}}
                )
            )
        self.observed("rotation-done", run_id).touch()
        self.rotations.remove(run_id)

    def vendor_pids(self):
        """Read complete argv, including a binary after a long cage prefix."""
        running = []
        for process in Path("/proc").iterdir():
            if not process.name.isdigit():
                continue
            try:
                arguments = (process / "cmdline").read_bytes().split(b"\0")
                if os.fsencode(self.cwd / "vendor") in arguments:
                    running.append(int(process.name))
            except FileNotFoundError, ProcessLookupError, PermissionError:
                continue
        return running

    def owning_host_pids(self, namespaced):
        """Map only this fixture's live PID namespace to host process identities."""
        namespace = None
        observed = []
        for pid in self.vendor_pids():
            process = Path("/proc") / str(pid)
            try:
                fields = next(
                    line.split()[1:]
                    for line in (process / "status").read_text().splitlines()
                    if line.startswith("NSpid:")
                )
                observed.append((process.name, fields))
                if len(fields) > 1 and int(fields[-1]) == namespaced["parent"]:
                    namespace = os.readlink(process / "ns/pid")
                    break
            except FileNotFoundError, ProcessLookupError, PermissionError:
                continue
        assert namespace is not None, observed
        mapped = {}
        for process in Path("/proc").iterdir():
            if not process.name.isdigit():
                continue
            try:
                if os.readlink(process / "ns/pid") != namespace:
                    continue
                fields = next(
                    line.split()[1:]
                    for line in (process / "status").read_text().splitlines()
                    if line.startswith("NSpid:")
                )
                mapped[int(fields[-1])] = int(process.name)
            except FileNotFoundError, ProcessLookupError, PermissionError:
                continue
        return {name: mapped[pid] for name, pid in namespaced.items()}

    def terminal(self, run_id):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            status = self.call("nspawn_run_show", {"run_id": run_id, "timeout_ms": 200})
            self.rotate_owned_fixture_auth(run_id)
            if status["phase"] in ("completed", "failed", "closed"):
                return status
        pytest.fail("native run did not reach a terminal phase")


@pytest.fixture
def native_client(tmp_path, request):
    provider = request.param
    kind = "agy" if provider == "agy" else "codex"
    store()
    fixture_root = tmp_path
    if kind == "agy":
        tmp_path = fixture_root / "project"
        tmp_path.mkdir()
    binary = tmp_path / "vendor"
    binary.write_text(
        f"#!{'/usr/bin/python3' if kind == 'agy' else sys.executable}\nKIND={kind!r}\nRESET_USAGE={provider == 'codex_reset'}\n"
        + VENDOR
    )
    binary.chmod(0o700)
    config = tmp_path / "spawn.toml"
    lines = [
        "[store]",
        f"run_root={json.dumps(str(fixture_root / 'runs'))}",
        "[bins]",
        f"{kind}={json.dumps(str(binary))}",
        "[[provider]]",
        f"name={json.dumps(provider)}",
        f"kind={json.dumps(kind)}",
        'model="test-model"',
        'models=["test-model"]',
    ]
    if provider == "fugu":
        secrets = tmp_path / "secrets"
        secrets.mkdir(mode=0o700)
        lines += [f"secrets={json.dumps(str(secrets))}"]
    for alias in ("first", "second"):
        home = tmp_path / alias
        home.mkdir(mode=0o700)
        auth = home / (
            ".gemini/antigravity-cli/antigravity-oauth-token"
            if kind == "agy"
            else "auth.json"
        )
        auth.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        auth.write_text(
            json.dumps(
                {
                    "token" if kind == "agy" else "tokens": {
                        "access_token": "synthetic-native-access-token"
                    }
                }
            )
        )
        auth.chmod(0o600)
        lines += [f"[provider.accounts.{alias}]", f"home={json.dumps(str(home))}"]
        if provider == "fugu":
            token = secrets / f"{alias}.token"
            token.write_text("synthetic-fugu-api-credential")
            token.chmod(0o600)
            profile = home / "config.toml"
            profile.write_text(
                '[model_providers.sakana_test]\nname="fixture"\nwire_api="responses"\nenv_key="FUGU_TEST_KEY"\nbase_url="https://api.sakana.ai/v1"\n'
            )
            profile.chmod(0o600)
            lines += ['auth_env="FUGU_TEST_KEY"', 'model_provider="sakana_test"']
    if provider == "codex_preference":
        # Both accounts, in the owner's order, for a witness that fails over.
        lines += ["[preference]", 'verification = ["first", "second"]']
    config.write_text("\n".join(lines) + "\n")
    env = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "LANG", "NOTT_DATABASE_DIR")
        if key in os.environ
    }
    # The served process imports THIS tree's `nspawn`, not whichever tree the
    # venv's editable install points at: otherwise a test measures the wrong code.
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    if provider == "codex_frozen_clock":
        # Every native quota observation lands in ONE second, so a same-second
        # race is deterministic: the app-server reader's clock alone is pinned.
        frozen = tmp_path / "frozen-clock-site"
        frozen.mkdir()
        (tmp_path / "frozen-clock").write_text(str(int(time.time())))
        (frozen / "sitecustomize.py").write_text(
            "import time, types\nimport nspawn.app_server as served\n"
            f"pinned = float(open({str(tmp_path / 'frozen-clock')!r}).read())\n"
            "served.time = types.SimpleNamespace(**{name: getattr(time, name) "
            "for name in dir(time) if not name.startswith('__')})\n"
            "served.time.time = lambda: pinned\n"
        )
        env["PYTHONPATH"] = os.pathsep.join((str(frozen), env["PYTHONPATH"]))
    if kind == "agy":
        # The protocol peer's cumulative count is supplied at its native port
        # from attempts, independently of the token journal under test. The real
        # cage remains unchanged: no project, HOME or credential write is admitted.
        peer_port = tmp_path / "agy-peer-port"
        peer_port.mkdir()
        (peer_port / "sitecustomize.py").write_text(
            "import nott_database\nfrom nspawn.agy import AgyTurn\nfrom nspawn.registry import store_path\n"
            "original=AgyTurn.start.__func__\n"
            "def start(cls,argv,*,cwd,env,**kwargs):\n"
            " with nott_database.connect(store_path(),readonly=True) as connection:\n"
            "  count=nott_database.query(connection,'SELECT count(*) FROM native_contexts WHERE home=?',(env['HOME'],))[0][0]\n"
            " return original(cls,argv,cwd=cwd,env={**env,'NOTT_NSPAWN_FIXTURE_ATTEMPT':str(count)},**kwargs)\n"
            "AgyTurn.start=classmethod(start)\n"
        )
        env["PYTHONPATH"] = os.pathsep.join((str(peer_port), env["PYTHONPATH"]))
    env.update(
        # Direct launch: a systemd scope per driver is proven by the live tier;
        # here it cost 106 scopes and a doubled runtime per run.
        NOTT_NSPAWN_DRIVER_LAUNCH="direct",
        NOTT_NSPAWN_CONFIG=str(config),
        NOTT_NSPAWN_ROSTER=str(config),
    )
    # Item assignment: the public source scan refuses a NAME=value credential shape.
    env["FUGU_TEST_KEY"] = "synthetic-fugu-api-credential"
    env["OPENAI_API_KEY"] = "synthetic-mcp-only-credential"
    with subprocess.Popen(
        [str(Path(sys.executable).with_name("nspawn")), "mcp", "serve"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=tmp_path,
        env=env,
    ) as process:
        client = Client(
            process, kind=kind, cwd=tmp_path, run_root=fixture_root / "runs"
        )
        try:
            response = client.request(
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "native-test", "version": "1"},
                },
            )
            assert "result" in response
            process.stdin.write(
                b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
            )
            process.stdin.flush()
            yield client, tmp_path
        finally:
            client.selector.close()
            process.stdin.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


@pytest.mark.parametrize(
    "native_client", ["codex", "codex_reset", "agy", "fugu"], indirect=True
)
def test_native_usage_reaches_common_journal_without_resume_double_count(native_client):
    client, cwd = native_client
    args = {
        "provider": "first",
        "cwd": str(cwd),
        "prompt": "Reply PONG",
        "class": DECLARED_CLASS,
        "read_only": True,
        "timeout_ms": 5000,
    }
    for _ in range(2):
        accepted = client.call("nspawn_run_start", args)
        status = client.terminal(accepted["run_id"])
        assert status["phase"] == "completed", status
        expected = (6, 1, 4, 3) if status["backend"] == "agy" else (18, 7, 7, 25)
        assert status["tokens"] == expected[3]
        events = _usage(accepted["run_id"])
        assert len(events) == 1
        payload = events[0]
        assert isinstance(payload, dict)
        assert (
            payload["input_tokens"],
            payload["output_tokens"],
            payload["cache_read_input_tokens"],
        ) == expected[:3]
        assert payload["cost_usd"] is None
        assert status["cost_usd"] is None
        assert payload["duration_api_ms"] is None
        assert payload["permission_denials"] is None
        assert payload["duration_ms"] >= 0
        assert payload["num_turns"] == 1
        # Scratch instrumentation is outside the admitted read-only project.
        assert payload["witnessed_effect"] == (
            "none" if status["backend"] == "agy" else "files"
        )
        assert payload["failure"] is None
        args["session_id"] = status["thread_id"]


@pytest.mark.parametrize("native_client", ["agy"], indirect=True)
def test_native_agy_partial_baseline_stays_unknown_until_measured(native_client):
    client, cwd = native_client
    binary = cwd / "vendor"
    binary.write_text(
        binary.read_text().replace(
            "'input_tokens': 2 * count,",
            "**({'input_tokens': 2 * count} if count > 1 else {}),",
        )
    )
    args = {
        "provider": "first",
        "cwd": str(cwd),
        "prompt": "Reply PONG",
        "class": DECLARED_CLASS,
        "read_only": True,
    }
    for expected_input in (None, None, 6):
        accepted = client.call("nspawn_run_start", args)
        status = client.terminal(accepted["run_id"])
        assert status["phase"] == "completed", status
        assert status["tokens"] == 3
        (payload,) = _usage(accepted["run_id"])
        assert payload["input_tokens"] == expected_input
        assert payload["output_tokens"] == 1
        assert payload["cache_read_input_tokens"] == 4
        args["session_id"] = status["thread_id"]


@pytest.mark.parametrize(
    "native_client, info, cooled, api_status, reason",
    [
        ("codex", "unauthorized", True, None, "VendorTurnError (unauthorized)"),
        ("fugu", "unauthorized", True, None, "VendorTurnError (unauthorized)"),
        (
            "codex",
            "usageLimitExceeded",
            True,
            None,
            "VendorTurnError (usageLimitExceeded)",
        ),
        (
            "codex",
            {"httpConnectionFailed": {"httpStatusCode": 403}},
            True,
            403,
            "VendorApiError (HTTP 403, httpConnectionFailed)",
        ),
        (
            "codex",
            {"httpConnectionFailed": {"httpStatusCode": 500}},
            False,
            500,
            "VendorApiError (HTTP 500, httpConnectionFailed)",
        ),
        (
            "codex",
            "rateLimitExceeded",
            False,
            None,
            "VendorTurnError (rateLimitExceeded)",
        ),
        # An unknown variant proves no status: its name is all the reason says.
        (
            "codex",
            {"futureError": {"httpStatusCode": 401}},
            False,
            None,
            "VendorTurnError (futureError)",
        ),
    ],
    indirect=["native_client"],
)
def test_native_account_failure_controls_next_unpinned_spawn(
    native_client, info, cooled, api_status, reason
):
    client, cwd = native_client
    provider = "fugu" if (cwd / "first/config.toml").exists() else "codex"
    args = {
        "provider": provider,
        "cwd": str(cwd),
        "read_only": True,
        "prompt": "fixture error " + json.dumps(info),
        "class": DECLARED_CLASS,
    }
    first = client.call("nspawn_run_start", {**args, "provider": "first"})
    failed = client.terminal(first["run_id"])
    assert failed["phase"] == "failed", failed
    assert failed["executed_account"] == "first"
    assert failed["failure_reason"] == reason
    (payload,) = _usage(first["run_id"])
    assert payload["api_error_status"] == api_status
    args["prompt"] = "Reply PONG"
    # The spread gives the next run to the account used longest ago: "second".
    second = client.call("nspawn_run_start", args)
    completed = client.terminal(second["run_id"])
    assert completed["phase"] == "completed", completed
    assert completed["executed_account"] == "second"
    # Now "first" is the older one: only a wall keeps the run off it.
    third = client.call("nspawn_run_start", args)
    completed = client.terminal(third["run_id"])
    assert completed["phase"] == "completed", completed
    assert completed["executed_account"] == ("second" if cooled else "first")
    # The failed request keeps its original identity and is never replayed.
    assert (
        client.call("nspawn_run_show", {"run_id": first["run_id"]})["phase"] == "failed"
    )
    # `runs/drivers` holds the launch acks, not a run.
    runs = [path for path in (cwd / "runs").iterdir() if path.name != "drivers"]
    assert len(runs) == 3


@pytest.mark.parametrize("native_client", ["codex"], indirect=True)
def test_a_vendor_rejection_that_fails_the_run_records_a_typed_failure_reason(
    native_client,
):
    client, cwd = native_client
    run = client.call(
        "nspawn_run_start",
        {
            "provider": "first",
            "cwd": str(cwd),
            "read_only": True,
            "prompt": "fixture vendor 400",
            "class": DECLARED_CLASS,
        },
    )

    status = client.terminal(run["run_id"])

    assert status["phase"] == "failed", status
    reason = "VendorApiError (HTTP 400, invalid_request_error)"
    assert status["failure_reason"] == reason
    result = client.call("nspawn_result_show", {"run_id": run["run_id"]})
    assert result["failure_reason"] == reason
    # The vendor's own words stay in the warning, as before, and only there.
    assert "is not supported when using Codex" in status["warning"]
    assert "supported" not in reason


@pytest.mark.parametrize("native_client", ["codex_preference"], indirect=True)
def test_a_codex_turn_that_refuses_the_model_hands_the_witness_on(native_client):
    """codex relays the refused model's HTTP 400 only in its message text
    (codex 0.157.1): the typed reason is the route's refusal, so an unpinned
    verification goes on to its next preference, through the real driver."""
    client, cwd = native_client
    run = client.call(
        "nspawn_run_start",
        {
            "cwd": str(cwd),
            "read_only": True,
            "prompt": "fixture vendor 400",
            "class": "verification",
            "timeout_ms": 60_000,
            "producer": ["MiniMax-M3"],
        },
    )

    first = client.terminal(run["run_id"])
    found = re.search(r"continued as run (\S+) on ", first["warning"] or "")
    assert found is not None, first
    second = client.terminal(found[1])

    reason = "VendorApiError (HTTP 400, invalid_request_error)"
    assert (first["executed_account"], second["executed_account"]) == (
        "first",
        "second",
    )
    assert f"skipped first ({reason} on run {run['run_id']})" in second["warning"]


@pytest.mark.parametrize("native_client", ["agy"], indirect=True)
@pytest.mark.parametrize("snapshot", [None, {"native_usage": {}}])
def test_native_resume_refuses_unmeasured_usage_before_launch(native_client, snapshot):
    client, cwd = native_client
    args = {
        "provider": "first",
        "cwd": str(cwd),
        "prompt": "Reply PONG",
        "class": DECLARED_CLASS,
        "read_only": True,
        "timeout_ms": 5000,
    }
    accepted = client.call("nspawn_run_start", args)
    status = client.terminal(accepted["run_id"])
    assert status["phase"] == "completed", status
    before = client.observed("vendor-pid").read_text()
    _store_write(
        "UPDATE runs SET last_item=? WHERE run_id=?",
        (json.dumps(snapshot), accepted["run_id"]),
    )
    args["session_id"] = status["thread_id"]
    response = client.request(
        "tools/call", {"name": "nspawn_run_start", "arguments": args}
    )
    assert "lacks a valid cumulative usage snapshot" in response["error"]["message"]
    assert client.observed("vendor-pid").read_text() == before


@pytest.mark.parametrize("native_client", ["codex", "fugu"], indirect=True)
@pytest.mark.parametrize("moving", [False, True])
def test_native_readonly_closes_inherited_mcp_catalog(native_client, moving):
    client, cwd = native_client
    (cwd / "lazy-plugin").touch()
    if moving:
        (cwd / "moving-catalog").touch()
    plugin = cwd / "readonly-plugin"
    plugin.mkdir()
    (plugin / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "nmemory": {
                        "command": "/fixture/memory",
                        "args": ["--project", "pynott"],
                    },
                    "nspawn": {"command": "/fixture/spawn"},
                }
            }
        )
    )
    with (cwd / "spawn.toml").open("a") as config:
        config.write("\n[defaults]\nplugins=[" + json.dumps(str(plugin)) + "]\n")
    (cwd / "mcp-catalog.json").write_text(
        json.dumps(
            [
                {
                    "name": "inherited",
                    "enabled": True,
                    "transport": {
                        "type": "stdio",
                        "command": "/fixture/never-launch",
                        "env": {"KEY": "synthetic-inventory-only-secret"},
                    },
                }
            ]
        )
    )
    args = {
        "provider": "first",
        "cwd": str(cwd),
        "prompt": "Reply PONG",
        "class": DECLARED_CLASS,
        "read_only": True,
    }
    for _ in range(2):
        accepted = client.call("nspawn_run_start", args)
        status = client.terminal(accepted["run_id"])
        assert status["phase"] == "completed", status
        settings = json.loads((cwd / "thread-request.json").read_text())["config"]
        servers = settings["mcp_servers"]
        assert settings["features.apps"] is False
        assert settings["plugins"]["extra@runtime"] == {
            "mcp_servers": {"runtime_extra": {"enabled": False}}
        }
        assert servers["inherited"] == {
            "command": "/fixture/never-launch",
            "enabled": False,
        }
        assert servers["nspawn"]["enabled"] is False
        assert servers["nmemory"]["args"] == ["--project", "pynott", "--read-only"]
        assert servers["nmemory"]["enabled_tools"] == ["nmemory_context_show"]
        assert (cwd / "mcp-status-checked").exists()
        for path in Path(status["run_dir"]).rglob("*"):
            if path.is_file():
                assert b"synthetic-inventory-only-secret" not in path.read_bytes()
        args["session_id"] = status["thread_id"]


@pytest.mark.parametrize("native_client", ["codex"], indirect=True)
def test_native_readonly_catalog_drift_refuses_before_model_turn(native_client):
    client, cwd = native_client
    (cwd / "mcp-catalog-drift").touch()
    run = client.call(
        "nspawn_run_start",
        {
            "provider": "first",
            "cwd": str(cwd),
            "prompt": "Reply PONG",
            "class": DECLARED_CLASS,
            "read_only": True,
        },
    )
    status = client.terminal(run["run_id"])
    assert status["phase"] == "failed", status
    assert not (cwd / "model-turn-started").exists()


@pytest.mark.parametrize("native_client", ["codex"], indirect=True)
def test_native_readonly_requires_declared_memory_before_model(native_client):
    client, cwd = native_client
    plugin = cwd / "plugin"
    plugin.mkdir()
    (plugin / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"nmemory": {"command": "/fixture/memory"}}})
    )
    with (cwd / "spawn.toml").open("a") as config:
        config.write("\n[defaults]\nplugins=[" + json.dumps(str(plugin)) + "]\n")
    (cwd / "missing-final-memory").touch()
    run = client.call(
        "nspawn_run_start",
        {
            "provider": "first",
            "cwd": str(cwd),
            "prompt": "Reply PONG",
            "class": DECLARED_CLASS,
            "read_only": True,
        },
    )
    status = client.terminal(run["run_id"])
    assert status["phase"] == "failed", status
    assert not (cwd / "model-turn-started").exists()


@pytest.mark.parametrize("native_client", ["codex", "fugu"], indirect=True)
def test_native_read_paths_preserve_and_attenuate_on_resume(native_client):
    client, cwd = native_client
    granted = cwd.parent / (cwd.name + "-granted")
    forbidden = cwd.parent / (cwd.name + "-forbidden")
    granted.mkdir()
    forbidden.mkdir()
    args = {
        "provider": "first",
        "cwd": str(cwd),
        "prompt": "Reply PONG",
        "class": DECLARED_CLASS,
        "read_only": True,
        "read_paths": [str(granted)],
    }
    for _ in range(2):
        run = client.call("nspawn_run_start", args)
        status = client.terminal(run["run_id"])
        assert status["phase"] == "completed", status
        request = json.loads((cwd / "thread-request.json").read_text())
        assert "sandbox" not in request
        profile = request["config"]["permissions"][request["permissions"]]
        assert profile["filesystem"][":root"] == "deny"
        assert profile["filesystem"][str(cwd.resolve())] == "read"
        assert profile["filesystem"][str(granted.resolve())] == "read"
        assert profile["network"]["enabled"] is False
        assert "write" not in profile["filesystem"].values()
        args["session_id"] = status["thread_id"]
    refused = client.request(
        "tools/call",
        {
            "name": "nspawn_run_start",
            "arguments": {
                **args,
                "read_paths": [str(granted), str(forbidden)],
            },
        },
    )
    assert "widen" in refused["error"]["message"]
    args.pop("read_paths")
    run = client.call("nspawn_run_start", args)
    assert client.terminal(run["run_id"])["phase"] == "completed"
    request = json.loads((cwd / "thread-request.json").read_text())
    profile = request["config"]["permissions"][request["permissions"]]
    assert str(granted.resolve()) not in profile["filesystem"]


@pytest.mark.parametrize("native_client", ["codex"], indirect=True)
def test_native_installed_skills_preserve_grants_and_refuse_version_drift(
    native_client,
):
    client, cwd = native_client
    home = cwd.parent / f"{cwd.name}-account"
    (cwd / "first").rename(home)
    config = cwd / "spawn.toml"
    config.write_text(
        config.read_text().replace(
            json.dumps(str(cwd / "first")), json.dumps(str(home))
        )
    )
    (home / "config.toml").write_text('[plugins."skills@runtime"]\nenabled=true\n')
    source = cwd / "declared-plugin"
    cache = home / "plugins/cache/runtime/skills/v1"
    manifest = {"name": "skills", "version": "v1", "skills": "./skills/"}
    for root in (source, cache):
        (root / ".codex-plugin").mkdir(parents=True)
        (root / ".codex-plugin/plugin.json").write_text(json.dumps(manifest))
        (root / "skills/readback").mkdir(parents=True)
        (root / "skills/readback/SKILL.md").write_text("Read back the request.\n")
    (source / ".mcp.json").write_text(
        '{"mcpServers":{"nmemory":{"command":"/fixture/memory"}}}'
    )
    with (cwd / "spawn.toml").open("a") as config:
        config.write("\n[defaults]\nplugins=[" + json.dumps(str(source)) + "]\n")
    (cwd / "skill-plugin.json").write_text(
        json.dumps(
            {
                "summary": {
                    "id": "skills@runtime",
                    "name": "skills",
                    "enabled": True,
                    "installed": True,
                },
                "skills": [
                    {"enabled": True, "path": str(cache / "skills/readback/SKILL.md")}
                ],
                "mcpServers": [],
            }
        )
    )
    args = {
        "provider": "first",
        "cwd": str(cwd),
        "prompt": "Reply PONG",
        "class": DECLARED_CLASS,
        "read_only": True,
    }
    for _ in range(2):
        run = client.call("nspawn_run_start", args)
        status = client.terminal(run["run_id"])
        assert status["phase"] == "completed", status
        request = json.loads((cwd / "thread-request.json").read_text())
        profile = request["config"]["permissions"][request["permissions"]]
        assert profile["filesystem"][str(cache / "skills")] == "read"
        assert str(home) not in profile["filesystem"]
        assert "write" not in profile["filesystem"].values()
        assert profile["filesystem"][":root"] == "deny"
        assert profile["network"]["enabled"] is False
        args["session_id"] = status["thread_id"]
    before = (cwd / "vendor-pid").read_text()
    manifest["version"] = "v2"
    cache.rename(cache.with_name("v2"))
    for root in (source, cache.with_name("v2")):
        (root / ".codex-plugin/plugin.json").write_text(json.dumps(manifest))
    response = client.request(
        "tools/call", {"name": "nspawn_run_start", "arguments": args}
    )
    assert "widen" in response["error"]["message"]
    assert (cwd / "vendor-pid").read_text() == before


@pytest.mark.parametrize("native_client", ["codex"], indirect=True)
@pytest.mark.parametrize("mismatch", ["wrong-read-profile", "read-profile-parent"])
def test_native_read_profile_mismatch_refuses_before_model(native_client, mismatch):
    client, cwd = native_client
    (cwd / mismatch).touch()
    run = client.call(
        "nspawn_run_start",
        {
            "provider": "first",
            "cwd": str(cwd),
            "prompt": "Reply PONG",
            "class": DECLARED_CLASS,
            "read_only": True,
        },
    )
    assert client.terminal(run["run_id"])["phase"] == "failed"
    assert not (cwd / "model-turn-started").exists()


@pytest.mark.parametrize("native_client", ["codex", "agy", "fugu"], indirect=True)
def test_native_mcp_executes_and_resumes_only_its_account(native_client):
    client, cwd = native_client
    arguments = {
        "provider": "first",
        "model": "test-model",
        "prompt": "Reply PONG",
        "class": DECLARED_CLASS,
        "cwd": str(cwd),
        "read_only": True,
        "timeout_ms": 5000,
    }
    first_thread = None
    for _ in range(2):
        accepted = client.call("nspawn_run_start", arguments)
        status = client.terminal(accepted["run_id"])
        assert status["phase"] == "completed", status
        assert status["has_result"] is True
        assert status["total_seq"] > 0
        assert (
            client.call("nspawn_result_show", {"run_id": accepted["run_id"]})[
                "final_text"
            ]
            == "PONG"
        )
        assert status["executed_account"] == "first"
        assert status["backend"] in ("codex", "agy")
        if status["backend"] == "agy":
            assert status["tokens"] == 3
        if first_thread is not None:
            assert status["thread_id"] == first_thread
        first_thread = status["thread_id"]
        arguments["session_id"] = first_thread
    arguments["provider"] = "second"
    rejected = client.request(
        "tools/call", {"name": "nspawn_run_start", "arguments": arguments}
    )
    assert "error" in rejected, rejected

    arguments["provider"] = "first"
    arguments["read_only"] = False
    before = sorted(path.name for path in client.run_root.iterdir())
    widened = client.request(
        "tools/call", {"name": "nspawn_run_start", "arguments": arguments}
    )
    # A resume cannot become a writer: a writable dispatch needs
    # `native_write`, and is refused before any run exists.
    assert widened["error"]["data"]["type"] == "InvalidRunRequest", widened
    assert "native_write" in widened["error"]["message"], widened
    assert sorted(path.name for path in client.run_root.iterdir()) == before

    arguments["read_only"] = True
    config = cwd / "spawn.toml"
    current = config.read_text()
    config.write_text(
        current.replace('kind="codex"', 'kind="claude"').replace(
            'kind="agy"', 'kind="claude"'
        )
    )
    changed = client.request(
        "tools/call", {"name": "nspawn_run_start", "arguments": arguments}
    )
    assert "error" in changed
    assert sorted(path.name for path in client.run_root.iterdir()) == before


@pytest.mark.parametrize("native_client", ["codex", "agy", "fugu"], indirect=True)
def test_native_mcp_deadline_reaps_its_child(native_client):
    client, cwd = native_client
    run = client.call(
        "nspawn_run_start",
        {
            "provider": "first",
            "prompt": "wait for deadline",
            "class": DECLARED_CLASS,
            "cwd": str(cwd),
            "read_only": True,
            # The deadline covers preparation too; codex runs its catalog and
            # metadata launches together so the vendor is born inside it
            # (test_native_mcp_preparation_launches_overlap).
            "timeout_ms": 500,
        },
    )
    status = client.terminal(run["run_id"])
    assert status["phase"] == "failed", status
    assert status["failure_reason"] == "RunDeadlineExceeded"
    # The 500 ms deadline covers preparation, so under host load it can expire
    # before the vendor interpreter has written `vendor-pid` (measured: the
    # file was absent in 22 of 30 runs under load). The claim is that no
    # vendor process outlives the deadline, born late or early; read that
    # from every live process running this test's vendor binary.
    assert (
        client.vendor_pids()
        if client.kind == "agy"
        else live_processes_running(cwd / "vendor")
    ) == []
    pid_file = client.observed("vendor-pid", run["run_id"])
    if pid_file.exists() and client.kind != "agy":
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)


@pytest.mark.parametrize("native_client", ["codex"], indirect=True)
def test_native_mcp_preparation_launches_overlap(native_client):
    """The CLI catalog and the metadata app-server read independent facts, so
    the deadline pays for the slower of them, not their sum: the metadata
    launch is born while a slow inventory is still answering."""
    client, cwd = native_client
    (cwd / "slow-inventory").touch()
    run = client.call(
        "nspawn_run_start",
        {
            "provider": "first",
            "prompt": "Reply PONG",
            "class": DECLARED_CLASS,
            "cwd": str(cwd),
            "read_only": True,
            "timeout_ms": 15000,
        },
    )
    status = client.terminal(run["run_id"])
    assert status["phase"] == "completed", status
    events = [
        json.loads(line) for line in (cwd / "launches.log").read_text().splitlines()
    ]
    inventory_end = next(
        t for edge, kind, t in events if (edge, kind) == ("end", "inventory")
    )
    births = [t for edge, kind, t in events if (edge, kind) == ("start", "app")]
    assert len(births) == 2, events  # the metadata inspector, then the turn
    assert births[0] < inventory_end, events


@pytest.mark.parametrize("native_client", ["codex", "agy", "fugu"], indirect=True)
def test_native_mcp_redacts_initial_and_rotated_credentials(native_client):
    client, cwd = native_client
    run = client.call(
        "nspawn_run_start",
        {
            "provider": "first",
            "prompt": "rotate fixture credential",
            "class": DECLARED_CLASS,
            "cwd": str(cwd),
            "read_only": True,
            "timeout_ms": 15000,
        },
    )
    status = client.terminal(run["run_id"])
    assert status["phase"] == "completed", status
    result = client.call("nspawn_result_show", {"run_id": run["run_id"]})
    assert result["final_text"] == "[redacted-credential] [redacted-credential]"
    run_dir = Path(status["run_dir"])
    for name in ("stdout", "stderr"):
        data = (run_dir / name).read_text()
        assert "[redacted-credential]" in data
        assert "synthetic-native-access-token" not in data
        assert "synthetic-rotated-access-token" not in data


@pytest.mark.parametrize("native_client", ["codex"], indirect=True)
@pytest.mark.parametrize("execution_class", [None, "judgment"])
def test_native_mcp_selects_capable_route_before_default_or_rank(
    native_client, execution_class
):
    client, cwd = native_client
    config = cwd / "spawn.toml"
    config.write_text(
        config.read_text().replace("[bins]\n", '[bins]\nagy="/never-start/agy"\n', 1)
    )
    with config.open("a") as stream:
        stream.write("""
[[provider]]
name="incompatible"
kind="agy"
model="test-model"
models=["test-model"]
default=true
strength=9
[provider.accounts.agy1]
home="/never-read/account"
""")
    args = {
        "cwd": str(cwd),
        "prompt": "Reply PONG",
        "class": DECLARED_CLASS,
        "read_only": True,
        "system_prompt": "Use the caller's explicit instructions.",
        "timeout_ms": 5000,
    }
    if execution_class is not None:
        args["class"] = execution_class
    accepted = client.call("nspawn_run_start", args)
    status = client.terminal(accepted["run_id"])
    assert status["phase"] == "completed", status
    assert status["executed_provider"] == "codex"
    request = json.loads((cwd / "thread-request.json").read_text())
    assert request["developerInstructions"] == args["system_prompt"]
    args["provider"] = "incompatible"
    refused = client.request(
        "tools/call", {"name": "nspawn_run_start", "arguments": args}
    )
    assert "AGY does not support" in refused["error"]["message"]


@pytest.mark.parametrize("native_client", ["agy"], indirect=True)
def test_native_mcp_canceled_agy_turn_preserves_resume_and_usage(native_client):
    client, cwd = native_client
    args = {
        "provider": "first",
        "cwd": str(cwd),
        "prompt": "cancel fixture turn",
        "class": DECLARED_CLASS,
        "read_only": True,
        "timeout_ms": 5000,
    }
    run = client.call("nspawn_run_start", args)
    status = client.terminal(run["run_id"])
    assert status["phase"] == "failed"
    assert status["warning"] == "AGY turn canceled"
    assert status["failure_reason"] == "NativeTurnFailed (CANCELED)"
    assert status["thread_id"]
    assert status["tokens"] == 3
    args.update(prompt="Reply PONG", session_id=status["thread_id"])
    resumed = client.call("nspawn_run_start", args)
    completed = client.terminal(resumed["run_id"])
    assert completed["phase"] == "completed", completed
    assert completed["thread_id"] == status["thread_id"]
    assert completed["tokens"] == 3


@pytest.mark.parametrize("native_client", ["codex"], indirect=True)
def test_native_mcp_scope_reaches_the_vendor_thread_configuration(native_client):
    client, cwd = native_client
    plugin = cwd / "plugin"
    plugin.mkdir()
    (plugin / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "nmemory": {"command": "/fixture/memory"},
                    "nspawn": {"command": "/fixture/spawn"},
                }
            }
        )
    )
    with (cwd / "spawn.toml").open("a") as config:
        config.write("\n[defaults]\nplugins=[" + json.dumps(str(plugin)) + "]\n")
    run = client.call(
        "nspawn_run_start",
        {
            "provider": "first",
            "cwd": str(cwd),
            "prompt": "Reply PONG",
            "class": DECLARED_CLASS,
            "read_only": True,
            "timeout_ms": 5000,
        },
    )
    assert client.terminal(run["run_id"])["phase"] == "completed"
    request = json.loads((cwd / "thread-request.json").read_text())
    servers = request["config"]["mcp_servers"]
    assert servers["nspawn"]["command"] == "/fixture/spawn"
    assert "NOTT_NSPAWN_CONFIG" in servers["nspawn"]["env_vars"]
    assert "NOTT_NMEMORY_CONFIG" in servers["nmemory"]["env_vars"]


@pytest.mark.parametrize("native_client", ["codex", "fugu"], indirect=True)
def test_optional_mcp_credential_is_scoped_and_redacted_before_persistence(
    native_client,
):
    client, cwd = native_client
    plugin = cwd / "plugin"
    plugin.mkdir()
    (plugin / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "nmemory": {
                        "command": "/fixture/memory",
                        "env_vars": ["OPENAI_API_KEY"],
                    },
                    "nspawn": {"command": "/fixture/spawn"},
                }
            }
        )
    )
    with (cwd / "spawn.toml").open("a") as config:
        config.write("\n[defaults]\nplugins=[" + json.dumps(str(plugin)) + "]\n")
    args = {
        "provider": "first",
        "cwd": str(cwd),
        "prompt": "echo fixture MCP credential",
        "class": DECLARED_CLASS,
        "read_only": True,
        "timeout_ms": 5000,
    }
    for _ in range(2):
        run = client.call("nspawn_run_start", args)
        status = client.terminal(run["run_id"])
        assert status["phase"] == "completed", status
        result = client.call("nspawn_result_show", {"run_id": run["run_id"]})
        assert result["final_text"] == "[redacted-credential]"
        assert (cwd / "mcp-env-verified").exists()
        assert "synthetic-mcp-only-credential" not in json.dumps(status)
        run_dir = cwd / "runs" / run["run_id"]
        for name in ("stdout", "stderr"):
            data = (run_dir / name).read_text()
            assert "[redacted-credential]" in data
            assert "synthetic-mcp-only-credential" not in data
        row = _store_rows(
            "SELECT last_item FROM runs WHERE run_id = ?", (run["run_id"],)
        )
        events = _store_rows(
            "SELECT payload FROM run_events WHERE run_id = ?", (run["run_id"],)
        )
        assert row and events
        assert "synthetic-mcp-only-credential" not in json.dumps(
            [tuple(r) for r in row + events]
        )
        args["session_id"] = status["thread_id"]


def _writer_slices(run_id: str) -> list[Path]:
    """The cgroups a run's native writer recorded before anything ran in them.

    By its key: the admission's own effort profile is a lifecycle payload too.
    """
    return [
        Path(payload["writer_scope"]["cgroup"])
        for payload in (
            json.loads(row[0])
            for row in _store_rows(
                "SELECT payload FROM run_events WHERE run_id = ? AND "
                "kind = 'lifecycle'",
                (run_id,),
            )
        )
        if "writer_scope" in payload
    ]


@pytest.mark.needs("systemd_user", "cgroup", "bwrap")
@pytest.mark.parametrize("native_client", ["codex", "fugu"], indirect=True)
def test_native_mcp_close_stops_its_tree_and_releases_writer_claim(native_client):
    """A WRITER through this served process, a native write, closed over stdio:
    its tree and its slice are gone and the claim it held is taken again."""
    client, cwd = native_client
    (cwd / ".git").mkdir()
    with (cwd / "spawn.toml").open("a") as config:
        config.write(f"\n[native_write]\nroots = [{json.dumps(str(cwd.parent))}]\n")
    args = {
        "provider": "first",
        "cwd": str(cwd),
        "prompt": "wait for close",
        "class": DECLARED_CLASS,
        "native_write": True,
        "timeout_ms": 5000,
    }
    run = client.call("nspawn_run_start", args)
    receipt = cwd / "vendor-tree.json"
    deadline = time.monotonic() + 3
    while not receipt.exists() and time.monotonic() < deadline:
        client.call("nspawn_run_show", {"run_id": run["run_id"], "timeout_ms": 100})
    assert receipt.exists()
    pids = json.loads(receipt.read_text())
    (slice_path,) = _writer_slices(run["run_id"])
    assert slice_path.exists()
    closed = client.call("nspawn_run_close", {"run_id": run["run_id"]})
    assert closed["phase"] == "closed"
    assert closed["claim_released"] is True
    assert not slice_path.exists()
    assert closed["tree"]["survivors"] == []
    for pid in pids.values():
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    assert {item["pid"] for item in closed["tree"]["checked"]} >= set(pids.values())
    assert (
        client.call("nspawn_run_show", {"run_id": run["run_id"]})["phase"] == "closed"
    )
    # The released claim is taken again: a second WRITER on the same cwd is
    # admitted, claims the worktree and runs in a slice of its own.
    args["prompt"] = "Reply PONG"
    next_run = client.call("nspawn_run_start", args)
    assert client.terminal(next_run["run_id"])["phase"] == "completed"
    (next_slice,) = _writer_slices(next_run["run_id"])
    assert next_slice != slice_path


@pytest.mark.parametrize("native_client", ["agy"], indirect=True)
def test_native_mcp_close_stops_an_agy_tree_and_keeps_serving(native_client):
    """AGY has no native write mode, so it never writes; its
    tree is stopped under a reader, which holds no claim to release."""
    client, cwd = native_client
    args = {
        "provider": "first",
        "cwd": str(cwd),
        "prompt": "wait for close",
        "class": DECLARED_CLASS,
        "read_only": True,
        "timeout_ms": 5000,
    }
    run = client.call("nspawn_run_start", args)
    receipt = client.observed("vendor-tree.json", run["run_id"])
    deadline = time.monotonic() + 3
    while not receipt.exists() and time.monotonic() < deadline:
        client.call("nspawn_run_show", {"run_id": run["run_id"], "timeout_ms": 100})
    assert receipt.exists()
    pids = client.owning_host_pids(json.loads(receipt.read_text()))
    (cage,) = _store_rows(
        "SELECT process_pid FROM runs WHERE run_id=?", (run["run_id"],)
    )
    cage_pid = cage[0]
    assert isinstance(cage_pid, int) and cage_pid not in pids.values()
    os.kill(cage_pid, 0)
    ancestry = {}
    for name, pid in pids.items():
        chain = [pid]
        while chain[-1] != cage_pid:
            status = (Path("/proc") / str(chain[-1]) / "status").read_text()
            parent = int(
                next(
                    line.split()[1]
                    for line in status.splitlines()
                    if line.startswith("PPid:")
                )
            )
            assert parent > 1 and parent not in chain
            chain.append(parent)
        ancestry[name] = chain
    closed = client.call("nspawn_run_close", {"run_id": run["run_id"]})
    assert closed["phase"] == "closed"
    assert closed["tree"]["survivors"] == []
    assert cage_pid in {item["pid"] for item in closed["tree"]["checked"]}
    for pid in (*pids.values(), cage_pid):
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    (client.run_root / run["run_id"] / "fixture-close-proof.json").write_text(
        json.dumps(
            {"cage": cage_pid, "native": pids, "ancestry": ancestry, "closed": closed}
        )
    )
    assert (
        client.call("nspawn_run_show", {"run_id": run["run_id"]})["phase"] == "closed"
    )
    args["prompt"] = "Reply PONG"
    next_run = client.call("nspawn_run_start", args)
    assert client.terminal(next_run["run_id"])["phase"] == "completed"


@pytest.mark.parametrize("native_client", ["codex"], indirect=True)
def test_native_mcp_writable_dispatch_knocks_only_on_the_test_door(
    native_client: Any,
) -> None:
    """Named for the body door the served process once dialed for a writable
    dispatch. That mode is retired: a writable dispatch that does not name
    native_write is refused as the caller's error, and no run exists."""
    client, cwd = native_client
    answer = client.request(
        "tools/call",
        {
            "name": "nspawn_run_start",
            "arguments": {
                "provider": "first",
                "cwd": str(cwd),
                "prompt": "never admitted",
                "class": DECLARED_CLASS,
            },
        },
    )
    assert answer["error"]["data"]["type"] == "InvalidRunRequest", answer
    assert "native_write" in answer["error"]["message"]
    # `runs/drivers` holds the per-call drivers' launch acks, not a run.
    runs = cwd / "runs"
    assert not runs.exists() or [p for p in runs.iterdir() if p.name != "drivers"] == []


@pytest.mark.parametrize("native_client", ["codex"], indirect=True)
@pytest.mark.parametrize("per_run", [None, "Follow the run instructions."])
def test_native_prompt_preserves_account_canonical_and_vendor_base(
    native_client, per_run
):
    client, cwd = native_client
    account = "Account instructions.\n" * 1700
    canonical = "Canonical instructions.\n" * 1700
    shared = cwd / "owner-config.toml"
    shared.write_text("developer_instructions=" + json.dumps(account) + "\n")
    shared.chmod(0o600)
    (cwd / "first/config.toml").symlink_to(shared)
    prompt_file = cwd / "canonical.md"
    prompt_file.write_text(canonical)
    prompt_file.chmod(0o600)
    with (cwd / "spawn.toml").open("a") as stream:
        stream.write(
            "\n[defaults]\nsystem_prompt=" + json.dumps(str(prompt_file)) + "\n"
        )
    args = {
        "provider": "first",
        "cwd": str(cwd),
        "prompt": "Reply PONG",
        "class": DECLARED_CLASS,
        "read_only": True,
        "timeout_ms": 5000,
    }
    if per_run is not None:
        args["system_prompt"] = per_run
    expected = "\n\n".join([account, canonical] + ([per_run] if per_run else []))
    for resumed in (False, True):
        accepted = client.call("nspawn_run_start", args)
        status = client.terminal(accepted["run_id"])
        assert status["phase"] == "completed", status
        request = json.loads((cwd / "thread-request.json").read_text())
        assert request["developerInstructions"] == expected
        assert "baseInstructions" not in request
        assert not any("instruction" in key for key in request["config"])
        assert ("threadId" in request) is resumed
        args["session_id"] = status["thread_id"]
    assert shared.read_text() == "developer_instructions=" + json.dumps(account) + "\n"
    assert prompt_file.read_text() == canonical


@pytest.mark.parametrize("native_client", ["codex"], indirect=True)
@pytest.mark.parametrize("source", ["account", "canonical"])
@pytest.mark.parametrize(
    "damage", ["missing", "directory", "oversize", "utf8", "nul", "invalid"]
)
def test_native_prompt_sources_refuse_before_vendor_launch(
    native_client, source, damage
):
    client, cwd = native_client
    path = cwd / ("first/config.toml" if source == "account" else "canonical.md")
    if source == "canonical":
        with (cwd / "spawn.toml").open("a") as stream:
            stream.write("\n[defaults]\nsystem_prompt=" + json.dumps(str(path)) + "\n")
    if damage == "missing":
        if source == "account":
            path.symlink_to(cwd / "absent-config")
    elif damage == "directory":
        path.mkdir()
    else:
        content = {
            "oversize": b"x" * (65536 + 1),
            "utf8": b"\xff",
            "nul": b'developer_instructions="\\u0000"'
            if source == "account"
            else b"\x00",
            "invalid": b"developer_instructions=42" if source == "account" else b"   ",
        }[damage]
        path.write_bytes(content)
        path.chmod(0o600)
    result = client.request(
        "tools/call",
        {
            "name": "nspawn_run_start",
            "arguments": {
                "provider": "first",
                "cwd": str(cwd),
                "prompt": "Reply PONG",
                "class": DECLARED_CLASS,
                "read_only": True,
            },
        },
    )
    assert "error" in result, result
    assert "native" in result["error"]["message"]
    assert not (cwd / "vendor-pid").exists()


@pytest.mark.parametrize(
    "native_client, observed, api_named_openai",
    [("codex", True, False), ("fugu", False, False), ("fugu", False, True)],
    indirect=["native_client"],
)
def test_native_account_quota_reaches_public_report(
    native_client, observed, api_named_openai
):
    client, cwd = native_client
    if api_named_openai:
        for path in (
            cwd / "spawn.toml",
            cwd / "first/config.toml",
            cwd / "second/config.toml",
        ):
            path.write_text(path.read_text().replace("sakana_test", "openai"))
        refused = client.request(
            "tools/call",
            {
                "name": "nspawn_run_start",
                "arguments": {
                    "provider": "first",
                    "cwd": str(cwd),
                    "prompt": "quota fixture",
                    "class": DECLARED_CLASS,
                    "read_only": True,
                },
            },
        )
        assert "API auth requires an explicit" in refused["error"]["message"]
        assert not (cwd / "model-turn-started").exists()
        return
    run = client.call(
        "nspawn_run_start",
        {
            "provider": "first",
            "cwd": str(cwd),
            "prompt": "quota fixture",
            "class": DECLARED_CLASS,
            "read_only": True,
        },
    )
    status = client.terminal(run["run_id"])
    assert status["phase"] == "completed", status
    report = client.call(
        "nspawn_quota_show", {"provider": status["executed_provider"], "refresh": 0}
    )
    account = next(
        a
        for a in report["providers"][0]["accounts"]
        if a["account_alias"] == status["executed_account"]
    )
    assert account["state"] == ("observed" if observed else "never_observed")
    if observed:
        assert account["source"] == "run-wire"
        assert [
            (w["window"], w["utilization"], w["resets_at"]) for w in account["windows"]
        ] == [("codex.primary.seven_day", 0.25, 4102444800)]
    else:
        assert account["windows"] == []


@pytest.mark.parametrize("native_client", ["codex"], indirect=True)
def test_native_quota_update_does_not_freshen_an_older_window(native_client):
    client, cwd = native_client
    client.call("nspawn_quota_show", {"refresh": 0})
    _store_write(
        "INSERT INTO account_usage VALUES ('first','older-window',0.1,4102444800,1)"
    )
    run = client.call(
        "nspawn_run_start",
        {
            "provider": "first",
            "cwd": str(cwd),
            "prompt": "quota fixture",
            "class": DECLARED_CLASS,
            "read_only": True,
        },
    )
    status = client.terminal(run["run_id"])
    assert status["phase"] == "completed", status
    report = client.call(
        "nspawn_quota_show", {"provider": status["executed_provider"], "refresh": 0}
    )
    account = next(
        a for a in report["providers"][0]["accounts"] if a["account_alias"] == "first"
    )
    windows = {w["window"]: w for w in account["windows"]}
    assert windows["codex.primary.seven_day"]["state"] == "observed"
    assert windows["older-window"]["observed_at"] == 1
    assert windows["older-window"]["state"] == "stale"


@pytest.mark.parametrize("native_client", ["codex"], indirect=True)
def test_native_quota_absence_preserves_model_result(native_client):
    client, cwd = native_client
    run = client.call(
        "nspawn_run_start",
        {
            "provider": "first",
            "cwd": str(cwd),
            "prompt": "quota malformed fixture",
            "class": DECLARED_CLASS,
            "read_only": True,
        },
    )
    status = client.terminal(run["run_id"])
    assert status["phase"] == "completed", status
    assert "native quota unavailable" in status["warning"]
    assert (
        client.call("nspawn_result_show", {"run_id": run["run_id"]})["final_text"]
        == "PONG"
    )


@pytest.mark.parametrize("native_client", ["codex"], indirect=True)
def test_native_quota_later_absence_preserves_last_observation(native_client):
    client, cwd = native_client
    run = client.call(
        "nspawn_run_start",
        {
            "provider": "first",
            "cwd": str(cwd),
            "prompt": "quota followed by absence fixture",
            "class": DECLARED_CLASS,
            "read_only": True,
        },
    )
    status = client.terminal(run["run_id"])
    assert status["phase"] == "completed", status
    report = client.call(
        "nspawn_quota_show", {"provider": status["executed_provider"], "refresh": 0}
    )
    account = next(
        a for a in report["providers"][0]["accounts"] if a["account_alias"] == "first"
    )
    assert account["state"] == "observed"
    assert account["windows"][0]["utilization"] == 0.25
    assert "no quota windows" in status["warning"]


@pytest.mark.parametrize("native_client", ["codex"], indirect=True)
def test_native_cold_quota_refresh_needs_no_model(native_client):
    client, cwd = native_client
    passive = client.call("nspawn_quota_show", {"refresh": 0})
    assert all(
        a["state"] == "never_observed" for a in passive["providers"][0]["accounts"]
    )
    assert not (cwd / "first/quota-read-called").exists()
    report = client.call("nspawn_quota_show", {"refresh": 1})
    for account in report["providers"][0]["accounts"]:
        assert account["state"] == "observed", account
        assert account["source"] == "codex-native"
        assert account["windows"][0]["utilization"] == 0.25
    assert (cwd / "first/quota-read-called").exists()
    assert not (cwd / "first/thread-request.json").exists()
    assert not (cwd / "second/thread-request.json").exists()
    assert not list((cwd / "runs").glob(".quota-*"))


@pytest.mark.parametrize("native_client", ["codex"], indirect=True)
def test_native_cold_quota_refuses_wrong_account(native_client):
    client, cwd = native_client
    (cwd / "first/wrong-quota-account").touch()
    report = client.call("nspawn_quota_show", {"refresh": 1})
    account = next(
        a for a in report["providers"][0]["accounts"] if a["account_alias"] == "first"
    )
    assert account["state"] == "no_source"
    assert "identity" in account["absence"]
    assert account["windows"] == []


@pytest.mark.parametrize("native_client", ["codex"], indirect=True)
def test_native_cold_quota_refuses_wrong_home(native_client):
    client, cwd = native_client
    (cwd / "first/wrong-quota-home").touch()
    report = client.call("nspawn_quota_show", {"refresh": 1})
    account = next(
        a for a in report["providers"][0]["accounts"] if a["account_alias"] == "first"
    )
    assert "home identity" in account["absence"]
    assert not (cwd / "first/quota-read-called").exists()
    assert not list((cwd / "runs").glob(".quota-*"))
