"""Real kernel boundary for AGY's owning composition, using an NDJSON peer.

These tests do not call the vendor or authenticate an account. Before the
composer exists, the peer crosses AgyTurn's current uncaged argv boundary;
the identical filesystem and process oracles then measure its outer cage.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nspawn.agy import AgyTurn
from nspawn.custody import AggregateScope
from nspawn.redact import SecretNeedles

PEER = r"""
import json, os, subprocess, sys, time
from pathlib import Path
checkout, home, run, scratch, outside, extra = map(Path, sys.argv[1:7])
print(json.dumps({"event": "init", "conversation_id": "outer-cage-peer"}), flush=True)
json.loads(sys.stdin.readline())

def succeeds(action):
    try:
        action()
    except OSError:
        return False
    return True

if len(sys.argv) > 7:
    code = "import os,time; from pathlib import Path; Path(%r).write_text(str(os.getpid())); time.sleep(30)" % str(run / "descendant")
    subprocess.Popen([sys.executable, "-c", code], start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)
    until = time.monotonic() + 3
    while not (run / "descendant").exists():
        if time.monotonic() > until:
            raise RuntimeError("descendant never entered its native turn")
        time.sleep(.01)
    observed = {"descendant_started": True}
else:
    state = home / ".gemini" / "antigravity-cli"
    status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line)
    observed = {
        "project_write": succeeds(lambda: (checkout / "answer").write_text("project")),
        "native_state_write": succeeds(lambda: (state / "history").write_text("native")),
        "scratch_write": succeeds(lambda: (scratch / "work").write_text("scratch")),
        "home_write": succeeds(lambda: (home / "ungranted").write_text("outside")),
        "outside_write": succeeds(lambda: (outside / "canary").write_text("changed")),
        "outside_read": succeeds(lambda: (outside / "canary").read_text()),
        "git_write": succeeds(lambda: (checkout / ".git" / "config").write_text("changed")),
        "extra_write": succeeds(lambda: (extra / "input").write_text("changed")),
        "extra_read": (extra / "input").read_text(),
        "uid_zero": succeeds(lambda: os.setuid(0)),
        "capabilities": int(status["CapEff"], 16),
        "no_new_privileges": int(status["NoNewPrivs"]),
        "host_bus": Path("/run/user/%d/bus" % os.getuid()).exists(),
        "host_cgroup": Path("/sys/fs/cgroup/cgroup.procs").exists(),
        "home": os.environ["HOME"],
        "resolver_readable": succeeds(lambda: Path("/etc/resolv.conf").read_bytes()),
        "resolver_readonly": (
            bool(os.statvfs("/etc/resolv.conf").f_flag & os.ST_RDONLY)
            if Path("/etc/resolv.conf").exists() else False
        ),
    }
print(json.dumps({"event": "result", "result": {
    "conversation_id": "outer-cage-peer", "status": "SUCCESS",
    "response": json.dumps(observed), "usage": {}, "duration_seconds": 0
}}), flush=True)
"""


@pytest.fixture
def layout(tmp_path: Path) -> dict[str, Path]:
    locations = {}
    for name in ("checkout", "home", "run", "scratch", "outside", "extra"):
        directory = tmp_path / name
        directory.mkdir(mode=0o700)
        locations[name] = directory
    (locations["checkout"] / ".git").mkdir()
    (locations["checkout"] / ".git" / "config").write_text("original")
    (locations["home"] / ".gemini" / "antigravity-cli").mkdir(parents=True, mode=0o700)
    (locations["outside"] / "canary").write_text("original")
    (locations["extra"] / "input").write_text("read-only")
    peer = tmp_path / "peer.py"
    peer.write_text(PEER)
    peer.chmod(0o600)
    locations["peer"] = peer
    return locations


def _compose(
    layout: dict[str, Path],
    *,
    writable: bool = True,
    descendant: bool = False,
    **overrides: Any,
) -> Any:
    argv = (
        str(Path("/usr/bin/python3").resolve()),
        str(layout["peer"]),
        *(
            str(layout[name])
            for name in ("checkout", "home", "run", "scratch", "outside", "extra")
        ),
        *(("descendant",) if descendant else ()),
    )
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "HOME": str(layout["home"]),
        "TMPDIR": str(layout["scratch"]),
    }
    if importlib.util.find_spec("nspawn.agy_restricted") is None:
        # The existing owning AgyTurn start forwards native argv without an
        # externally enforced filesystem or PID namespace. Keep RED causal.
        return SimpleNamespace(argv=argv, env=env, cwd=layout["checkout"])
    module = importlib.import_module("nspawn.agy_restricted")
    arguments = {
        "argv": argv,
        "env": env,
        "checkout": layout["checkout"],
        "home": layout["home"],
        "run_dir": layout["run"],
        "scratch": layout["scratch"],
        "writable": writable,
        "read_paths": (layout["extra"],),
        "administrative_paths": (layout["home"] / ".gemini" / "antigravity-cli",),
        "runtime_paths": (layout["peer"],),
    }
    arguments.update(overrides)
    return module.compose_restricted_launch(**arguments)


def _turn(
    layout: dict[str, Path], *, writable: bool, **overrides: Any
) -> dict[str, Any]:
    launch = _compose(layout, writable=writable, **overrides)
    with AgyTurn.start(
        launch.argv,
        cwd=launch.cwd,
        env=launch.env,
        run_dir=layout["run"],
        needles=SecretNeedles(),
    ) as turn:
        result = turn.run("Measure only this disposable peer.", timeout=8)
    assert result.status == "SUCCESS" and result.conversation_id == "outer-cage-peer"
    assert result.final_text is not None
    return json.loads(result.final_text)


def test_owner_controlled_0750_home_remains_the_native_read_only_home(
    layout: dict[str, Path],
) -> None:
    # This is the owning installed account's mode, not an administrative root.
    layout["home"].chmod(0o750)
    observed = _turn(layout, writable=True)
    assert observed["home"] == str(layout["home"])
    assert observed["home_write"] is False, observed
    assert observed["native_state_write"] and observed["scratch_write"], observed
    assert observed["outside_write"] is False and observed["git_write"] is False
    assert observed["no_new_privileges"] == 1 and observed["capabilities"] == 0


def test_explicit_resolver_alias_is_readable_and_read_only_in_the_real_cage(
    layout: dict[str, Path],
) -> None:
    logical = Path("/etc/resolv.conf")
    source = logical.resolve(strict=True)
    assert logical.is_symlink() and source.is_file()
    observed = _turn(
        layout,
        writable=True,
        runtime_paths=(layout["peer"], source),
        runtime_aliases=((source, logical),),
    )
    assert observed["resolver_readable"] is True, observed
    assert observed["resolver_readonly"] is True, observed
    assert observed["host_bus"] is False and observed["host_cgroup"] is False


@pytest.mark.parametrize("mode", [0o770, 0o777])
def test_writable_by_another_principal_home_is_refused(
    layout: dict[str, Path], mode: int
) -> None:
    module = importlib.import_module("nspawn.agy_restricted")
    layout["home"].chmod(mode)
    with pytest.raises(module.RestrictedLaunchRefused):
        _compose(layout)
    assert not (layout["checkout"] / "answer").exists()


@pytest.mark.parametrize("changed", ["different-source", "other-alias", "duplicate"])
def test_resolver_mapping_cannot_admit_another_runtime_alias(
    layout: dict[str, Path], changed: str
) -> None:
    module = importlib.import_module("nspawn.agy_restricted")
    logical = Path("/etc/resolv.conf")
    source = logical.resolve(strict=True)
    aliases = ((source, logical),)
    if changed == "different-source":
        aliases = ((layout["extra"] / "input", logical),)
    elif changed == "other-alias":
        aliases = ((source, Path("/etc/passwd")),)
    else:
        aliases = (*aliases, *aliases)
    with pytest.raises(module.RestrictedLaunchRefused):
        _compose(layout, runtime_aliases=aliases)
    assert not (layout["checkout"] / "answer").exists()


@pytest.mark.parametrize("writable", [True, False], ids=["writer", "reviewer"])
def test_real_agy_boundary_keeps_only_admitted_destinations_writable(
    layout: dict[str, Path], writable: bool
) -> None:
    observed = _turn(layout, writable=writable)
    assert observed["native_state_write"] and observed["scratch_write"], observed
    assert observed["project_write"] is writable, observed
    assert observed["home"] == str(layout["home"])
    assert observed["extra_read"] == "read-only", observed
    for forbidden in (
        "home_write",
        "outside_write",
        "outside_read",
        "git_write",
        "extra_write",
    ):
        assert observed[forbidden] is False, (forbidden, observed)
    assert (layout["outside"] / "canary").read_text() == "original"
    assert (layout["checkout"] / ".git" / "config").read_text() == "original"
    assert (layout["extra"] / "input").read_text() == "read-only"


def test_real_agy_boundary_has_no_ungranted_privilege_or_host_control_port(
    layout: dict[str, Path],
) -> None:
    observed = _turn(layout, writable=True)
    assert observed["uid_zero"] is False and observed["capabilities"] == 0, observed
    assert observed["no_new_privileges"] == 1, observed
    assert observed["host_bus"] is False and observed["host_cgroup"] is False, observed


def test_reviewer_origin_records_the_actual_read_only_checkout(
    layout: dict[str, Path],
) -> None:
    launch = _compose(layout, writable=False)
    assert layout["checkout"] in launch.read_paths
    assert layout["checkout"] not in launch.write_paths
    observed = _turn(layout, writable=False)
    assert observed["project_write"] is False and observed["extra_read"] == "read-only"


@pytest.mark.parametrize(
    "changed", ["scratch-link", "home-selection", "broad-administration"]
)
def test_changed_admission_inputs_refuse_before_any_native_effect(
    layout: dict[str, Path],
    changed: str,
) -> None:
    module = importlib.import_module("nspawn.agy_restricted")
    arguments: dict[str, Any] = {}
    if changed == "scratch-link":
        link = layout["scratch"].with_name("scratch-link")
        link.symlink_to(layout["scratch"], target_is_directory=True)
        arguments["scratch"] = link
    elif changed == "home-selection":
        env = _compose(layout).env
        arguments["env"] = {**env, "HOME": str(layout["outside"])}
    else:
        arguments["administrative_paths"] = (layout["checkout"].parent,)
    before = (layout["outside"] / "canary").read_bytes()
    with pytest.raises(module.RestrictedLaunchRefused):
        _compose(layout, **arguments)
    assert (layout["outside"] / "canary").read_bytes() == before
    assert not (layout["run"] / "descendant").exists()


def _members(scope: AggregateScope) -> set[int]:
    assert scope.path is not None
    if not scope.path.exists():
        return set()
    return {
        int(pid)
        for path in scope.path.rglob("cgroup.procs")
        for pid in path.read_text().split()
    }


def test_real_agy_setsid_descendant_cannot_outlive_its_caged_turn(
    layout: dict[str, Path],
) -> None:
    launch = _compose(layout, descendant=True)
    # Real owning aggregate custody cleans the RED's escaped descendant too;
    # never leave a private peer running merely because its assertion fails.
    with AggregateScope(memory_bytes=256 * 1024**2, tasks=32) as scope:
        with AgyTurn.start(
            scope.command(list(launch.argv)),
            cwd=launch.cwd,
            env=scope.environment(launch.env),
            run_dir=layout["run"],
            needles=SecretNeedles(),
        ) as turn:
            result = turn.run("Start only the disposable descendant.", timeout=8)
        assert result.status == "SUCCESS" and result.final_text is not None
        assert json.loads(result.final_text) == {"descendant_started": True}
        deadline = time.monotonic() + 2
        while _members(scope) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert _members(scope) == set(), "setsid descendant survived the native turn"


@pytest.mark.parametrize("writable", [False, True])
def test_real_agy_runtime_loads_ca_bundle(
    layout: dict[str, Path], writable: bool
) -> None:
    """The host CA bundle remains readable in both native process modes."""
    import subprocess
    import sys

    from nspawn import native
    from nspawn.owner_roster import NativeAccount
    from nspawn.rows import NativeSessionContext

    if not Path("/etc/ca-certificates").is_dir():
        pytest.skip("This host has no extracted CA bundle directory")
    program = r"""
import json, os, ssl
from pathlib import Path
observed = {"bundle_exists": Path("/etc/ssl/certs/ca-certificates.crt").exists()}
try:
    context = ssl.create_default_context(cafile="/etc/ssl/certs/ca-certificates.crt")
    observed["trusted_ca_count"] = context.cert_store_stats()["x509_ca"]
    observed["ca_readonly"] = bool(os.statvfs("/etc/ca-certificates").f_flag & os.ST_RDONLY)
except (OSError, ssl.SSLError) as error:
    observed.update(trusted_ca_count=0, error_type=type(error).__name__)
print(json.dumps(observed))
"""
    launch = native.NativeLaunch(
        argv=(str(Path(sys.executable).resolve()), "-c", program),
        env={"HOME": str(layout["home"]), "PATH": "/usr/bin:/bin"},
        needles=SecretNeedles.none(),
        context=NativeSessionContext(str(layout["home"])),
        account=NativeAccount(home=layout["home"]),
    )
    confined = native.agy_confined(
        launch, cwd=layout["checkout"], run_dir=layout["run"], writable=writable
    )
    result = subprocess.run(
        confined.argv,
        env=confined.env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0
    observed = json.loads(result.stdout)
    assert observed["trusted_ca_count"] > 0, observed
    assert observed["ca_readonly"] is True, observed
