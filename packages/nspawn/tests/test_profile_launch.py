"""Prelaunch check/exec identity, with no account or native inference."""

import hashlib
import json
import os
import select
import selectors
import signal
import socket
import subprocess
import threading
import time
from contextlib import ExitStack
from pathlib import Path

import pytest
from supervisor_support import _drain_worker_threads, _stop_every_reaper, registry
from test_admission import profile

from nspawn.supervisor import Supervisor

__all__ = ["_drain_worker_threads", "_stop_every_reaper", "registry"]

from nspawn import claude_restricted, cli, network, route
from nspawn.admission import (
    AdmissionRefusal,
    PrelaunchProfileV1,
    document_digest,
    policy_generation,
)
from nspawn.claude_restricted import compose_node_launch, node_tool_settings
from nspawn.network import egress_start


def test_native_file_denies_use_literal_absolute_permission_paths(tmp_path):
    private = str(tmp_path / "admin[account]*")
    configuration = str(tmp_path / "settings.json")
    settings = node_tool_settings(
        {"administrative": [private], "read": [], "write": []},
        str(tmp_path / "scratch"),
        [configuration],
    )
    denied = settings["settings"]["permissions"]["deny"]
    assert f"Read(/{configuration})" in denied, (
        "one slash denotes a settings-relative path in the native grammar"
    )
    escaped = private.replace("[", "\\[").replace("]", "\\]").replace("*", "\\*")
    assert f"Read(/{escaped}/**)" in denied, (
        "administrative names must not become gitignore wildcards"
    )


def test_outer_cage_cannot_admit_tools_with_native_admin_or_credential_privilege(
    tmp_path,
) -> None:
    prepared, schema = boundary_profile(tmp_path)
    raw = prepared.data()
    raw["constructor"]["options"].pop("sandbox")
    raw["selection"]["constructor_baseline"] = document_digest(raw["constructor"])
    prepared = PrelaunchProfileV1(raw)
    with pytest.raises(AdmissionRefusal, match="tool boundary"):
        compose_node_launch(
            prepared,
            consumer_argv=(prepared.data()["runtime"]["node"]["path"], "-c", "true"),
            host_auth={"CLAUDE_CODE_OAUTH_TOKEN": "private-fixture"},
            schema=schema,
            grant_root=tmp_path / "run",
            descriptor=9,
        )


def test_script_adapter_resolves_mjs_through_the_native_node_runtime(
    tmp_path: Path,
    monkeypatch,
) -> None:
    entry = tmp_path / "adapter.mjs"
    entry.write_text("export {};\n")
    runtime = tmp_path / "node"
    runtime.touch()
    monkeypatch.setattr(
        route.shutil,
        "which",
        lambda command: str(runtime) if command == "node" else str(entry),
    )
    assert route.adapter_command("claude", []) == [str(runtime), str(entry)]


def test_profile_launch_arguments_remain_explicit_and_paired() -> None:
    accepted = cli._route_arguments(
        [
            "claude",
            "--route",
            "account-a@home",
            "--profile",
            "/private/profile.json",
            "--profile-digest",
            "sha256:" + "a" * 64,
            "--grant-root",
            "/private",
            "--check",
        ],
        "start",
    )
    assert accepted is not None
    assert accepted[1]["--profile"] == "/private/profile.json"
    assert accepted[1]["--grant-root"] == "/private"
    assert accepted[2] is True


@pytest.mark.parametrize(
    "arguments",
    [
        ["claude", "--profile", "/private/profile.json", "--check"],
        ["claude", "--profile-digest", "sha256:" + "a" * 64, "--check"],
        [
            "qwen",
            "--profile",
            "/private/profile.json",
            "--profile-digest",
            "sha256:" + "a" * 64,
            "--grant-root",
            "/private",
            "--check",
        ],
    ],
)
def test_incomplete_or_cross_provider_profile_cannot_fall_back_to_the_stock_adapter(
    arguments, monkeypatch
) -> None:
    calls = []
    monkeypatch.setattr(
        route,
        "route_launch",
        lambda *args, **kwargs: calls.append(args) or (["/bin/true"], {}),
    )
    assert cli.cmd_route_start(arguments) == 2
    assert calls == []


def test_profile_check_returns_bound_composition_without_launching_legacy_adapter(
    tmp_path, monkeypatch, capsys
) -> None:
    prepared, _ = boundary_profile(tmp_path)
    raw = prepared.data()
    for name in ("consumer", "sdk"):
        entry = tmp_path / (name + ".mjs")
        entry.write_text("export {};\n")
        raw["runtime"][name] = {
            "path": str(entry),
            "digest": "sha256:" + hashlib.sha256(entry.read_bytes()).hexdigest(),
        }
    raw["runtime"]["files"] = [
        raw["runtime"][name]
        for name in ("launcher", "node", "consumer", "sdk", "sdk_package", "native")
    ]
    prepared = PrelaunchProfileV1(raw)
    staged = tmp_path / "run" / "prelaunch.json"
    staged.write_bytes(prepared.encoded)
    staged.chmod(0o600)
    calls = []
    monkeypatch.setattr(
        route,
        "route_launch",
        lambda *args, **kwargs: calls.append(args) or (["/bin/true"], {}),
    )
    monkeypatch.setattr(route, "read_roster", lambda: None)
    monkeypatch.setattr(
        route,
        "route_start",
        lambda *args, **kwargs: (
            raw["route_ref"],
            {
                "CLAUDE_CODE_OAUTH_TOKEN": "private-fixture",
                "CLAUDE_CODE_EXECUTABLE": raw["runtime"]["native"]["path"],
            },
            [],
        ),
    )
    assert (
        cli.cmd_route_start(
            [
                "claude",
                "--route",
                raw["route_ref"],
                "--profile",
                str(staged),
                "--profile-digest",
                prepared.digest,
                "--grant-root",
                str(staged.parent),
                "--check",
            ]
        )
        == 0
    )
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["profile_digest"] == prepared.digest
    assert receipt["composition"]["runtime"] == raw["runtime"]
    assert calls == []


def boundary_profile(
    tmp_path: Path, audit: bool = False
) -> tuple[PrelaunchProfileV1, Path]:
    root = tmp_path / "run"
    root.mkdir(mode=0o700)
    project = tmp_path / "project"
    project.mkdir()
    readonly = project / "settings.json"
    readonly.write_text("{}")
    schema = root / "schema.json"
    schema.write_text("{}")
    runtime = str(Path("/usr/bin/bash").resolve())
    raw = profile()
    raw["cwd"] = str(project)
    raw["runtime"] = {
        name: {
            "path": runtime,
            "digest": "sha256:"
            + hashlib.sha256(Path(runtime).read_bytes()).hexdigest(),
        }
        for name in ("launcher", "node", "consumer", "sdk", "native")
    }
    raw["runtime"]["sdk_package"] = dict(raw["runtime"]["sdk"])
    raw["runtime"]["files"] = [dict(raw["runtime"]["sdk"])]
    raw["scope"] = {
        "read": ["/usr", "/etc/ssl/certs", str(project)],
        "write": [] if audit else [str(project)],
        "audit": audit,
        "permission_ceiling": "read-only" if audit else "write",
        "administrative": [str(root)],
        "network": [],
    }
    raw["configuration"] = [
        {
            "path": str(readonly),
            "format": "json",
            "document": "{}",
            "digest": "sha256:" + hashlib.sha256(b"{}").hexdigest(),
            "role": "project",
        }
    ]
    raw["project"] = {
        "id": "private-project",
        "root": str(project),
        "plugin_parent": str(readonly),
        "plugin_view": str(readonly),
        "mcp_configuration": {"mcpServers": {}},
    }
    raw["constructor"]["options"] = {
        "env": {"HOME": str(root), "TMPDIR": str(root), "CLAUDE_CONFIG_DIR": str(root)},
        **node_tool_settings(raw["scope"], str(root), [str(readonly)]),
    }  # type: ignore[index]
    raw["selection"]["constructor_baseline"] = document_digest(raw["constructor"])  # type: ignore[index]
    return PrelaunchProfileV1(raw), schema


def test_full_process_boundary_precedes_startup_effects_and_pins_configuration(
    tmp_path: Path,
) -> None:
    prepared, schema = boundary_profile(tmp_path)
    raw = prepared.data()
    allowed = Path(raw["cwd"]) / "receipt"
    outside = tmp_path / "outside"
    outside.write_text("untouched")
    configuration = raw["configuration"][0]["path"]
    script = 'printf admitted > "$1"; printf escaped > "$2"; printf mutated > "$3"; test -z "$HOST_CANARY"; printf done'
    with route.profile_seal(prepared) as descriptor:
        composed = compose_node_launch(
            prepared,
            consumer_argv=(
                raw["runtime"]["node"]["path"],
                "-c",
                script,
                "boundary",
                str(allowed),
                str(outside),
                configuration,
            ),
            host_auth={"CLAUDE_CODE_OAUTH_TOKEN": "fixture-auth-only"},
            schema=schema,
            grant_root=schema.parent,
            descriptor=descriptor,
        )
        assert "HOST_CANARY" not in composed.env
        result = subprocess.run(
            composed.argv,
            env=composed.env,
            pass_fds=(descriptor,),
            capture_output=True,
            timeout=10,
            check=False,
        )
    assert result.returncode == 0, result.stderr.decode()
    assert allowed.read_text() == "admitted"
    assert outside.read_text() == "untouched"
    assert Path(configuration).read_text() == "{}"
    assert result.stdout == b"done"


def test_audit_boundary_refuses_project_writes_before_the_consumer(
    tmp_path: Path,
) -> None:
    prepared, schema = boundary_profile(tmp_path, audit=True)
    raw = prepared.data()
    denied = Path(raw["cwd"]) / "denied"
    started = schema.parent / "started"
    with route.profile_seal(prepared) as descriptor:
        composed = compose_node_launch(
            prepared,
            consumer_argv=(
                raw["runtime"]["node"]["path"],
                "-c",
                'printf started > "$1"; printf denied > "$2"',
                "boundary",
                str(started),
                str(denied),
            ),
            host_auth={"CLAUDE_CODE_OAUTH_TOKEN": "fixture-auth-only"},
            schema=schema,
            grant_root=schema.parent,
            descriptor=descriptor,
        )
        result = subprocess.run(
            composed.argv,
            env=composed.env,
            pass_fds=(descriptor,),
            capture_output=True,
            timeout=10,
            check=False,
        )
    assert result.returncode != 0
    assert started.read_text() == "started"
    assert b"Read-only file system" in result.stderr
    assert not denied.exists()


def test_native_inner_bubblewrap_has_private_temporary_root(tmp_path: Path) -> None:
    prepared, schema = boundary_profile(tmp_path)
    raw = prepared.data()
    marker = "nott-inner-" + raw["activation_id"]
    host_marker = Path("/tmp") / marker
    assert not host_marker.exists()
    script = (
        'set -e; "$1" --new-session --die-with-parent --ro-bind / / '
        "--dev /dev --unshare-pid --unshare-user --proc /proc -- /usr/bin/true; "
        'printf private > "/tmp/$2"; printf "%s" "$TMPDIR"'
    )
    with route.profile_seal(prepared) as descriptor:
        composed = compose_node_launch(
            prepared,
            consumer_argv=(
                raw["runtime"]["node"]["path"],
                "-c",
                script,
                "boundary",
                "/usr/bin/bwrap",
                marker,
            ),
            host_auth={"CLAUDE_CODE_OAUTH_TOKEN": "fixture-auth-only"},
            schema=schema,
            grant_root=schema.parent,
            descriptor=descriptor,
        )
        result = subprocess.run(
            composed.argv,
            env=composed.env,
            pass_fds=(descriptor,),
            capture_output=True,
            timeout=10,
            check=False,
        )
    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout == raw["constructor"]["options"]["env"]["TMPDIR"].encode()
    assert not host_marker.exists()


def large_profile(tmp_path: Path, monkeypatch):
    prepared, _ = boundary_profile(tmp_path)
    raw = prepared.data()
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    manifest = []
    for index in range(1900):
        path = plugin / f"resource-{index}.txt"
        path.write_text(f"admitted-{index}")
        manifest.append(
            {
                "path": str(path),
                "digest": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    consumer = plugin / "consumer.mjs"
    consumer.write_text(
        "import fs from 'node:fs';\n"
        f"if (fs.existsSync({json.dumps(str(plugin / 'unmanifested'))})) throw Error('extra input');\n"
        f"process.stdout.write(fs.readFileSync({json.dumps(manifest[0]['path'])}));\n"
    )
    for name, path in (
        ("node", Path("/usr/bin/node").resolve()),
        ("consumer", consumer),
    ):
        raw["runtime"][name] = {
            "path": str(path),
            "digest": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    raw["runtime"]["files"] = [
        *(
            raw["runtime"][name]
            for name in ("launcher", "node", "consumer", "sdk", "sdk_package", "native")
        ),
        *manifest,
    ]
    raw["project"]["plugin_parent"] = str(plugin)
    raw["project"]["plugin_view"] = str(plugin)
    raw["policy"]["trust_filter"]["runtime_inventory"] = {
        str(plugin): {"directories": ["."], "links": {}}
    }
    raw["policy"]["generation_digest"] = policy_generation(raw)
    prepared = PrelaunchProfileV1(raw)
    staged = tmp_path / "run" / "prelaunch.json"
    staged.write_bytes(prepared.encoded)
    staged.chmod(0o600)
    monkeypatch.setattr(route, "read_roster", lambda: None)
    monkeypatch.setattr(
        route,
        "route_start",
        lambda *args, **kwargs: (
            raw["route_ref"],
            {"CLAUDE_CODE_OAUTH_TOKEN": "private-fixture"},
            [],
        ),
    )
    return prepared, staged, manifest[0]


def test_darwin_preparation_refuses_a_route_without_the_product_executable(
    tmp_path: Path, monkeypatch
) -> None:
    prepared, staged, _ = large_profile(tmp_path, monkeypatch)
    monkeypatch.setattr(route.sys, "platform", "darwin")

    def compose(*args, **kwargs):
        pytest.fail("The route reached launch composition without an executable.")

    monkeypatch.setattr(claude_restricted, "compose_node_launch", compose)
    with (
        pytest.raises(AdmissionRefusal, match="selected native runtime"),
        route.profile_prepare(
            "claude",
            prepared.data()["route_ref"],
            path=staged,
            digest=prepared.digest,
            grant_root=staged.parent,
        ),
    ):
        pytest.fail("The route reached execution without its product executable.")


def test_required_manifest_crosses_native_argument_ceiling_without_losing_files(
    tmp_path: Path, monkeypatch
) -> None:
    prepared, staged, _ = large_profile(tmp_path, monkeypatch)
    with route.profile_prepare(
        "claude",
        prepared.data()["route_ref"],
        path=staged,
        digest=prepared.digest,
        grant_root=staged.parent,
    ) as launch:
        result = subprocess.run(
            launch.argv,
            cwd=launch.cwd,
            env=launch.env,
            pass_fds=launch.descriptors,
            capture_output=True,
            timeout=20,
            check=False,
        )
    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout == b"admitted-0"


def installed_profile(tmp_path: Path, monkeypatch):
    prepared, staged, identity = large_profile(tmp_path, monkeypatch)
    raw = prepared.data()
    root = Path(raw["project"]["plugin_parent"])
    alias = root / "installed-alias.txt"
    alias.symlink_to(Path(identity["path"]).name)
    raw["policy"]["trust_filter"]["runtime_inventory"] = {
        str(root): {
            "directories": ["."],
            "links": {alias.name: identity["path"]},
        }
    }
    raw["policy"]["generation_digest"] = policy_generation(raw)
    prepared = PrelaunchProfileV1(raw)
    staged.write_bytes(prepared.encoded)
    return prepared, staged, identity, alias


def test_installed_runtime_uses_original_readonly_root_without_bulk_capture(
    tmp_path: Path, monkeypatch
) -> None:
    prepared, staged, _, _ = installed_profile(tmp_path, monkeypatch)
    root = prepared.data()["project"]["plugin_parent"]
    with route.profile_prepare(
        "claude",
        prepared.data()["route_ref"],
        path=staged,
        digest=prepared.digest,
        grant_root=staged.parent,
    ) as launch:
        descriptor = int(launch.argv[launch.argv.index("--args") + 1])
        options = os.pread(descriptor, 1024 * 1024, 0).decode().split("\0")
        mounts = [
            tuple(options[index + 1 : index + 3])
            for index, option in enumerate(options)
            if option == "--ro-bind"
        ]
        assert (root, root) in mounts
        assert len(launch.descriptors) == len(prepared.data()["configuration"]) + 2
        result = subprocess.run(
            launch.argv,
            cwd=launch.cwd,
            env=launch.env,
            pass_fds=launch.descriptors,
            capture_output=True,
            timeout=20,
            check=False,
        )
    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout == b"admitted-0"


@pytest.mark.parametrize("mutation", ["bytes", "extra", "alias"])
def test_installed_runtime_changes_are_refused_at_the_actual_execute_port(
    tmp_path: Path, monkeypatch, mutation: str
) -> None:
    prepared, staged, identity, alias = installed_profile(tmp_path, monkeypatch)
    with route.profile_prepare(
        "claude",
        prepared.data()["route_ref"],
        path=staged,
        digest=prepared.digest,
        grant_root=staged.parent,
    ) as launch:
        if mutation == "bytes":
            Path(identity["path"]).write_text("changed after inventory")
        elif mutation == "extra":
            (alias.parent / "unmanifested").write_text("not admitted")
        else:
            alias.unlink()
            alias.symlink_to("resource-1.txt")
        with pytest.raises(AdmissionRefusal, match="runtime"):
            route.profile_verify(launch)


def test_configuration_keeps_native_canonical_identity_and_captured_bytes(
    tmp_path: Path, monkeypatch
) -> None:
    prepared, staged, _ = large_profile(tmp_path, monkeypatch)
    raw = prepared.data()
    configuration = raw["configuration"][0]["path"]
    consumer = Path(raw["runtime"]["consumer"]["path"])
    native = (
        "const fs=require('node:fs'),cp=require('node:child_process');"
        "const selected=process.argv[1],bytes=fs.readFileSync(selected,'utf8'),"
        "canonical=fs.realpathSync(selected);"
        "const result=cp.spawnSync('/usr/bin/bwrap',"
        "['--new-session','--die-with-parent','--ro-bind','/','/',"
        "'--ro-bind','/dev/null',canonical,'--dev','/dev','--unshare-pid',"
        "'--unshare-user','--proc','/proc','--','/usr/bin/true'],"
        "{encoding:'utf8'});"
        "console.log(JSON.stringify({selected,canonical,bytes}));"
        "process.stderr.write(result.stderr);process.exitCode=result.status;"
    )
    consumer.write_text(
        "import {spawnSync} from 'node:child_process';\n"
        "const result=spawnSync('/usr/bin/bun',"
        + json.dumps(["-e", native, configuration])
        + ",{encoding:'utf8'});\n"
        "process.stdout.write(result.stdout);process.stderr.write(result.stderr);\n"
        "process.exitCode=result.status;\n"
    )
    identity = {
        "path": str(consumer),
        "digest": "sha256:" + hashlib.sha256(consumer.read_bytes()).hexdigest(),
    }
    raw["runtime"]["consumer"] = identity
    raw["runtime"]["files"] = [
        identity if item["path"] == str(consumer) else item
        for item in raw["runtime"]["files"]
    ]
    raw["policy"]["generation_digest"] = policy_generation(raw)
    prepared = PrelaunchProfileV1(raw)
    staged.write_bytes(prepared.encoded)
    with route.profile_prepare(
        "claude",
        raw["route_ref"],
        path=staged,
        digest=prepared.digest,
        grant_root=staged.parent,
    ) as launch:
        Path(configuration).write_text('{"changed":"after capture"}')
        result = subprocess.run(
            launch.argv,
            cwd=launch.cwd,
            env=launch.env,
            pass_fds=launch.descriptors,
            capture_output=True,
            timeout=20,
            check=False,
        )
    assert result.returncode == 0, result.stderr.decode()
    assert json.loads(result.stdout) == {
        "selected": configuration,
        "canonical": configuration,
        "bytes": "{}",
    }


@pytest.mark.parametrize("mutation", ["bytes", "extra", "link"])
def test_configuration_refuses_changed_snapshot_before_execution(
    tmp_path: Path, monkeypatch, mutation
) -> None:
    prepared, staged, _ = large_profile(tmp_path, monkeypatch)
    actual_compose = claude_restricted.compose_node_launch

    def changed(profile, **kwargs):
        configuration = kwargs.get("configuration")
        if configuration:
            source = Path(next(iter(configuration.values())))
            if mutation == "bytes":
                source.chmod(0o600)
                source.write_text("changed after capture")
                source.chmod(0o444)
            else:
                source.parent.chmod(0o700)
                if mutation == "extra":
                    (source.parent / "unmanifested").write_text("not admitted")
                else:
                    source.unlink()
                    source.symlink_to(tmp_path / "outside")
                source.parent.chmod(0o555)
        return actual_compose(profile, **kwargs)

    monkeypatch.setattr(claude_restricted, "compose_node_launch", changed)
    with (
        pytest.raises(AdmissionRefusal, match="configuration"),
        route.profile_prepare(
            "claude",
            prepared.data()["route_ref"],
            path=staged,
            digest=prepared.digest,
            grant_root=staged.parent,
        ),
    ):
        pytest.fail("changed configuration reached execution")


def test_installed_manifest_refuses_changed_bytes_and_extra_inputs_before_exec(
    tmp_path: Path, monkeypatch
) -> None:
    prepared, staged, identity = large_profile(tmp_path, monkeypatch)
    with route.profile_prepare(
        "claude",
        prepared.data()["route_ref"],
        path=staged,
        digest=prepared.digest,
        grant_root=staged.parent,
    ) as launch:
        Path(identity["path"]).write_text("changed after capture")
        (Path(identity["path"]).parent / "unmanifested").write_text("not admitted")
        with pytest.raises(AdmissionRefusal, match="runtime"):
            route.profile_verify(launch)


def test_installed_manifest_refuses_stale_identity_before_preparation(
    tmp_path: Path, monkeypatch
) -> None:
    prepared, staged, identity = large_profile(tmp_path, monkeypatch)
    Path(identity["path"]).write_text("changed before capture")
    with (
        pytest.raises(AdmissionRefusal, match="content identity"),
        route.profile_prepare(
            "claude",
            prepared.data()["route_ref"],
            path=staged,
            digest=prepared.digest,
            grant_root=staged.parent,
        ),
    ):
        pytest.fail("stale input reached launch")


@pytest.mark.parametrize("mutation", ["extra", "link", "bytes"])
def test_installed_manifest_refuses_topology_or_content_changes_before_bootstrap(
    tmp_path: Path, monkeypatch, mutation
) -> None:
    prepared, staged, _ = large_profile(tmp_path, monkeypatch)
    source = Path(prepared.data()["project"]["plugin_parent"])
    if mutation == "extra":
        (source / "extra").write_text("unmanifested")
    elif mutation == "link":
        (source / "extra").symlink_to(staged)
    else:
        (source / "resource-0.txt").write_text("rebound")
    with (
        pytest.raises(AdmissionRefusal, match="runtime"),
        route.profile_prepare(
            "claude",
            prepared.data()["route_ref"],
            path=staged,
            digest=prepared.digest,
            grant_root=staged.parent,
        ),
    ):
        pytest.fail("changed installed inputs reached launch")


def test_installed_manifest_refuses_a_project_overlap(
    tmp_path: Path, monkeypatch
) -> None:
    prepared, staged, _ = large_profile(tmp_path, monkeypatch)
    raw = prepared.data()
    raw["scope"]["write"].append(raw["project"]["plugin_parent"])
    raw["constructor"]["options"].update(
        node_tool_settings(
            raw["scope"],
            raw["constructor"]["options"]["env"]["TMPDIR"],
            [item["path"] for item in raw["configuration"]],
        )
    )
    raw["selection"]["constructor_baseline"] = document_digest(raw["constructor"])
    prepared = PrelaunchProfileV1(raw)
    staged.write_bytes(prepared.encoded)
    with (
        pytest.raises(AdmissionRefusal, match="runtime root"),
        route.profile_prepare(
            "claude",
            prepared.data()["route_ref"],
            path=staged,
            digest=prepared.digest,
            grant_root=staged.parent,
        ),
    ):
        pytest.fail("writable runtime root reached launch")


def test_profile_check_cleans_its_grouped_stage(tmp_path: Path, monkeypatch) -> None:
    prepared, staged, _ = large_profile(tmp_path, monkeypatch)
    stages = []
    actual_prepare = route.profile_prepare

    def tracked(*args, **kwargs):
        from contextlib import contextmanager

        @contextmanager
        def capture():
            with actual_prepare(*args, **kwargs) as launch:
                assert launch.stage is not None
                stages.append(launch.stage)
                assert launch.stage.is_dir()
                yield launch

        return capture()

    monkeypatch.setattr(route, "profile_prepare", tracked)
    assert (
        cli.cmd_route_start(
            [
                "claude",
                "--route",
                prepared.data()["route_ref"],
                "--profile",
                str(staged),
                "--profile-digest",
                prepared.digest,
                "--grant-root",
                str(staged.parent),
                "--check",
            ]
        )
        == 0
    )
    assert stages and not stages[0].exists()


@pytest.mark.parametrize("ending", ["eof", "term", "kill", "child-kill"])
def test_grouped_stage_outlives_its_exact_cage_and_is_cleaned_after_quiescence(
    registry, tmp_path: Path, monkeypatch, ending
) -> None:
    prepared, staged, _ = large_profile(tmp_path, monkeypatch)
    raw = prepared.data()
    consumer = Path(raw["runtime"]["consumer"]["path"])
    consumer.write_text(
        "process.stdout.write(JSON.stringify({jsonrpc:'2.0',method:'peer/ready'})+'\\n');\n"
        "process.stdin.resume();\n"
        "process.stdin.on('end', () => process.exit(23));\n"
    )
    raw["runtime"]["consumer"]["digest"] = (
        "sha256:" + hashlib.sha256(consumer.read_bytes()).hexdigest()
    )
    for identity in raw["runtime"]["files"]:
        if identity["path"] == str(consumer):
            identity["digest"] = raw["runtime"]["consumer"]["digest"]
    prepared = PrelaunchProfileV1(raw)
    staged.write_bytes(prepared.encoded)
    sv = Supervisor(registry, tmp_path / "managed-runs", host_control=True)
    run_id = None
    peer = None
    try:
        constructor = raw["constructor"]
        admitted = sv.execute(
            None,
            raw["cwd"],
            activation={
                "provider": "claude",
                "account_route": "claude:" + raw["route_ref"],
                "model": constructor["model"],
                "effort": constructor["effort"],
                "profile": str(staged),
                "profile_digest": prepared.digest,
                "grant_root": str(staged.parent),
            },
        )
        run_id = admitted.run_id
        managed = sv._sessions[run_id]
        peer = socket.socket(socket.AF_UNIX)
        peer.settimeout(10)
        peer.connect(managed.data()["path"])
        reader = peer.makefile("rb")
        assert json.loads(reader.readline())["method"] == "peer/ready"
        stage = route.profile_stage(prepared, staged.parent)
        assert stage.is_dir(), "the exact running cage still owns its projections"
        process = managed.spawned
        pid = process.identity.pid
        assert Path(f"/proc/{pid}/exe").resolve().name == "bwrap"
        if ending == "eof":
            process.close_stdin()
        elif ending == "child-kill":
            children = Path(f"/proc/{pid}/task/{pid}/children").read_text().split()
            assert len(children) == 1, (
                "only the exact cage's namespace child is targeted"
            )
            os.kill(int(children[0]), signal.SIGKILL)
        else:
            os.kill(pid, signal.SIGTERM if ending == "term" else signal.SIGKILL)
        assert (
            process.wait(timeout=10)
            == {
                "eof": 23,
                "term": -signal.SIGTERM,
                "kill": -signal.SIGKILL,
                "child-kill": 128 + signal.SIGKILL,
            }[ending]
        )
        assert sv.close(run_id).phase.value == "closed"
        deadline = time.monotonic() + 5
        while stage.exists():
            assert time.monotonic() < deadline, (
                "the owning custodian did not clean its stage"
            )
            time.sleep(0.01)
        reader.close()
    finally:
        if run_id is not None:
            sv.close(run_id)
        if peer is not None:
            peer.close()
        sv.shutdown()


@pytest.mark.parametrize("descriptors", ["ordinary", "startup", "relay"])
def test_private_egress_admits_exact_destination_and_refuses_another(
    tmp_path: Path, monkeypatch, descriptors
) -> None:
    with socket.socket() as target, ExitStack() as owned:
        target.bind(("127.0.0.1", 0))
        target.listen()
        port = target.getsockname()[1]
        held = []
        if descriptors != "ordinary":
            while not held or held[-1].fileno() < 1100:
                held.append(owned.enter_context(open(os.devnull, "rb")))
        actual_pipe = os.pipe

        def readiness_pipe():
            pair = actual_pipe()
            assert (max(pair) >= 1024) == (descriptors != "ordinary")
            return pair

        monkeypatch.setattr(network.os, "pipe", readiness_pipe)
        if descriptors == "relay":
            actual_select = select.select

            def startup_wait(readers, writers, errors, timeout):
                if len(readers) == 1 and isinstance(readers[0], int):
                    # Isolate the original relay after allowing only startup's
                    # high pipe. The admitted CONNECT/forwarding stays real.
                    with selectors.DefaultSelector() as ready:
                        ready.register(readers[0], selectors.EVENT_READ)
                        return ([readers[0]] if ready.select(timeout) else [], [], [])
                assert all(reader.fileno() >= 1024 for reader in readers)
                return actual_select(readers, writers, errors, timeout)

            monkeypatch.setattr(select, "select", startup_wait)

        def respond() -> None:
            with target.accept()[0] as connection:
                connection.sendall(b"native-target-receipt")

        path = tmp_path / "egress.sock"
        with egress_start(path, (f"127.0.0.1:{port}",)):
            worker = threading.Thread(target=respond)
            worker.start()
            with socket.socket(socket.AF_UNIX) as allowed:
                allowed.settimeout(5)
                allowed.connect(str(path))
                allowed.sendall(f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\n\r\n".encode())
                result = b""
                while chunk := allowed.recv(4096):
                    result += chunk
                assert b"200 Connection established" in result
                assert b"native-target-receipt" in result
            with socket.socket(socket.AF_UNIX) as denied:
                denied.settimeout(5)
                denied.connect(str(path))
                denied.sendall(b"CONNECT 127.0.0.1:9 HTTP/1.1\r\n\r\n")
                assert b"403 Forbidden" in denied.recv(4096)
        worker.join(5)
        assert not worker.is_alive()
        assert not path.exists()
