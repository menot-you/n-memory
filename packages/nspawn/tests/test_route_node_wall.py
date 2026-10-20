"""`route show` picks a new node's account; `route wall` walls a node's account.

Both run through `nspawn` itself (`cli.main`) on this test's own registry and roster.
The Codex frames are the ones recorded from codex-acp 2.1.1
(`harness/tests/acp/fixtures/codex-acp-2.1.1-*.jsonl`).
"""

from __future__ import annotations

import io
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from registry_reset import sql, store
from workspace_root import workspace_path

from nspawn import cli
from nspawn.registry import Registry, store_path
from nspawn.rows import AccountUsageRow

FRAMES = "harness/tests/acp/fixtures"


def recorded(name: str) -> dict[str, Any]:
    """The error the adapter answered the recorded prompt with."""
    for line in (
        workspace_path(f"{FRAMES}/codex-acp-2.1.1-{name}.jsonl")
        .read_text()
        .splitlines()
    ):
        entry = json.loads(line)
        if entry["dir"] == "in" and "error" in entry["frame"]:
            error: dict[str, Any] = entry["frame"]["error"]
            return error
    raise AssertionError(f"no error frame in {name}")


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Three Codex accounts and two AGY accounts, every home in `tmp_path`."""
    for name in ("NOTT_NODE_CODEX_ADAPTER", "CODEX_PATH", "CODEX_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    roster = tmp_path / "spawn.toml"
    roster.write_text(
        f'[bins]\ncodex = "{tmp_path / "codex"}"\nagy = "{tmp_path / "agy"}"\n\n'
        '[[provider]]\nname = "codex"\nkind = "codex"\nmodel = "gpt-m"\n'
        'model_fast = "gpt-fast"\n'
        + "".join(
            f'[provider.accounts.{alias}]\nhome = "{tmp_path / alias}"\n'
            for alias in ("codex-a", "codex-b", "codex-c")
        )
        + '\n[[provider]]\nname = "antigravity"\nkind = "agy"\nmodel = "gemini"\n'
        + "".join(
            f'[provider.accounts.{alias}]\nhome = "{tmp_path / alias}"\n'
            for alias in ("agy1", "agy2")
        )
    )
    monkeypatch.setenv("NOTT_NSPAWN_ROSTER", str(roster))
    return tmp_path


def registry() -> Registry:
    opened = Registry(store(), "route-node-wall-test").connect()
    opened.open(now=int(time.time()))
    return opened


def live_run(alias: str, run: str) -> None:
    owner = registry()
    try:
        sql(
            owner,
            "INSERT INTO runs(run_id, worktree, phase, sandbox, model, owner_instance,"
            " created_at, updated_at, backend, executed_account)"
            " VALUES (?, ?, 'running', 'read-only', 'gpt-m', ?, 1, 2, 'codex', ?)",
            (run, f"/tmp/{run}", owner.instance_id, alias),
        )
    finally:
        owner.close()


def wall(alias: str, until: int) -> None:
    owner = registry()
    try:
        owner.record_account_wall(
            alias, until, "default_1h", "quota_exhausted", int(time.time())
        )
    finally:
        owner.close()


def walls() -> dict[str, Any]:
    owner = registry()
    try:
        return {
            row.account_alias: row
            for row in owner.get_account_wall_rows(int(time.time()))
        }
    finally:
        owner.close()


def run(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
    body: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any] | None, str]:
    if body is not None:
        monkeypatch.setattr(
            sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(body).encode()))
        )
    code = cli.main(arguments)
    captured = capsys.readouterr()
    shown = json.loads(captured.out) if code == 0 and captured.out else None
    return code, shown, captured.err


def iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def route_of(root: Path, alias: str) -> str:
    return f"{alias}@{root / alias}"


# --- route wall ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("frame", "limit", "lasting"),
    [
        ("usage-limit", "quota_exhausted", 3600),
        ("auth-required", "credential_rejected", 300),
    ],
)
def test_a_recorded_codex_failure_walls_its_account(
    root: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    frame: str,
    limit: str,
    lasting: int,
) -> None:
    store()
    now = int(time.time())
    code, shown, err = run(
        capsys,
        monkeypatch,
        ["route", "wall", "codex", "--route", route_of(root, "codex-a")],
        {"observed_at": now, "error": recorded(frame)},
    )
    assert code == 0, err
    assert shown is not None and shown["version"] == 1
    assert shown["wall"]["account_alias"] == "codex-a"
    assert shown["wall"]["limit_type"] == limit
    # The frame names no reset of its own: nSPAWN's default stands in.
    assert shown["wall"]["cooling_until"] == now + lasting
    assert shown["reset"] is None, "codex-b and codex-c are free"
    assert set(walls()) == {"codex-a"}


def test_a_claude_login_refusal_walls_its_account_and_never_a_bare_home(
    root: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A roster account is walled as a Codex one is. A bare home is no roster
    account: its wall is read back, and nothing is recorded."""
    store()
    home = root / "claude-home"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    now = int(time.time())
    failure = {"observed_at": now, "error": recorded("auth-required")}
    # Without Claude accounts in the roster, no Claude route has an account to wall.
    for unrouted in (str(home), f"claude-a@{home}"):
        code, shown, err = run(
            capsys,
            monkeypatch,
            ["route", "wall", "claude", "--route", unrouted],
            failure,
        )
        assert code == 0, err
        assert shown == {"version": 1, "wall": None, "reset": None}
    secrets = root / "claude-secrets"
    secrets.mkdir(mode=0o700)
    for alias in ("claude-a", "claude-b"):
        (secrets / f"{alias}.token").write_text(f"token-{alias}\n")
    roster = root / "spawn.toml"
    roster.write_text(
        roster.read_text()
        + f'\n[[provider]]\nname = "claude"\nkind = "claude"\nsecrets = "{secrets}"\n'
        + "".join(
            f'[provider.accounts.{alias}]\nauth_env = "CLAUDE_CODE_OAUTH_TOKEN"\n'
            for alias in ("claude-a", "claude-b")
        )
    )
    code, shown, err = run(
        capsys, monkeypatch, ["route", "wall", "claude", "--route", str(home)], failure
    )
    assert code == 0, err
    assert shown is not None and shown["wall"]["account_alias"] == str(home)
    assert shown["wall"]["limit_type"] == "credential_rejected"
    assert walls() == {}
    code, shown, err = run(
        capsys,
        monkeypatch,
        ["route", "wall", "claude", "--route", f"claude-a@{home}"],
        failure,
    )
    assert code == 0, err
    assert shown is not None and shown["wall"]["cooling_until"] == now + 300
    assert shown["reset"] is None, "claude-b is free"
    assert set(walls()) == {"claude-a"}


def test_a_failure_that_names_no_account_failure_walls_nothing(
    root: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    store()
    quota = recorded("usage-limit")
    # The same words without the structured field: free text never makes a wall.
    words = {
        "code": -32603,
        "message": "Internal error",
        "data": {"message": quota["data"]["message"]},
    }
    for error in (words, {"code": -32001, "message": "vendor quota refused"}):
        code, shown, err = run(
            capsys,
            monkeypatch,
            ["route", "wall", "codex", "--route", route_of(root, "codex-a")],
            {"observed_at": int(time.time()), "error": error},
        )
        assert code == 0, err
        assert shown == {"version": 1, "wall": None, "reset": None}
    assert walls() == {}


def test_the_last_free_account_walled_names_the_earliest_reset(
    root: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    now = int(time.time())
    wall("codex-b", now + 600)
    wall("codex-c", now + 900)
    code, shown, err = run(
        capsys,
        monkeypatch,
        ["route", "wall", "codex", "--route", route_of(root, "codex-a")],
        {"observed_at": now, "error": recorded("usage-limit")},
    )
    assert code == 0, err
    assert shown is not None and shown["reset"] == now + 600


def test_an_agy_wall_is_the_one_its_shim_recorded(
    root: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    store()
    now = int(time.time())
    arguments = ["route", "wall", "agy", "--route", route_of(root, "agy1")]
    body = {"observed_at": now, "error": {"code": -32603, "message": "quota"}}
    code, shown, err = run(capsys, monkeypatch, arguments, body)
    assert code == 0, err
    assert shown == {"version": 1, "wall": None, "reset": None}
    wall("agy1", now + 1200)
    before = walls()["agy1"]
    code, shown, _ = run(capsys, monkeypatch, arguments, body)
    assert code == 0 and shown is not None
    assert shown["wall"]["cooling_until"] == now + 1200
    assert shown["reset"] is None
    # Read back, never written again.
    assert walls() == {"agy1": before}


def test_route_wall_refuses_a_node_inside_a_cage(
    root: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOTT_NSPAWN_NODE_SESSION", "n")
    code, _, err = run(
        capsys,
        monkeypatch,
        ["route", "wall", "codex", "--route", route_of(root, "codex-a")],
        {"observed_at": int(time.time()), "error": recorded("usage-limit")},
    )
    assert code == 2 and "node environment" in err


# --- route show, a new node ------------------------------------------------------------


def test_a_new_node_takes_the_least_loaded_unwalled_account(
    root: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    live_run("codex-a", "r-a")
    code, shown, err = run(capsys, monkeypatch, ["route", "show", "codex"])
    assert code == 0, err
    assert shown is not None and shown["route"] == route_of(root, "codex-b")
    wall("codex-b", int(time.time()) + 600)
    live_run("codex-c", "r-c")
    live_run("codex-c", "r-c2")
    _, shown, _ = run(capsys, monkeypatch, ["route", "show", "codex"])
    assert shown is not None and shown["route"] == route_of(root, "codex-a")


def test_an_account_named_is_taken_unless_it_is_walled(
    root: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    live_run("codex-c", "r-c")
    arguments = ["route", "show", "codex", "--account", "codex-c"]
    code, shown, err = run(capsys, monkeypatch, arguments)
    assert code == 0, err
    assert shown is not None and shown["route"] == route_of(root, "codex-c")
    until = int(time.time()) + 4321
    wall("codex-c", until)
    code, shown, err = run(capsys, monkeypatch, arguments)
    assert code == 2 and shown is None
    assert f"the first account resets at {iso(until)}" in err
    code, _, err = run(
        capsys, monkeypatch, ["route", "show", "codex", "--account", "nobody"]
    )
    assert code == 2 and "nobody" in err


def test_every_account_walled_refuses_with_the_earliest_reset(
    root: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    now = int(time.time())
    for alias, until in (
        ("codex-a", now + 900),
        ("codex-b", now + 300),
        ("codex-c", now + 600),
    ):
        wall(alias, until)
    # No other provider can serve either: the requested provider's refusal stands.
    wall("agy1", now + 900)
    wall("agy2", now + 900)
    code, shown, err = run(
        capsys,
        monkeypatch,
        ["route", "show", "codex", "--move", "claude,codex,qwen,agy"],
    )
    assert code == 2 and shown is None
    assert f"the first account resets at {iso(now + 300)}" in err


def test_a_walled_provider_starts_the_node_on_another_with_an_alert(
    root: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    now = int(time.time())
    for alias in ("codex-a", "codex-b", "codex-c"):
        wall(alias, now + 600)
    wall("agy1", now + 600)
    # Without --move (a wall move, an owner's move) the provider never changes.
    code, shown, err = run(capsys, monkeypatch, ["route", "show", "codex"])
    assert code == 2 and shown is None
    assert "the first account resets at" in err
    code, shown, err = run(
        capsys,
        monkeypatch,
        ["route", "show", "codex", "--move", "claude,codex,qwen,agy"],
    )
    assert code == 0, err
    assert shown is not None
    assert shown["provider"] == "agy"
    assert shown["route"] == route_of(root, "agy2")
    alert = shown["alert"]
    assert alert.startswith("codex: ")
    assert 'every allowed account of provider "codex" is cooling down' in alert
    assert f"the first account resets at {iso(now + 600)}" in alert
    assert alert.endswith("this node runs on agy, model gemini")
    # Only a provider the caller can run is given, and a name it cannot is usage.
    code, shown, err = run(
        capsys, monkeypatch, ["route", "show", "codex", "--move", "qwen"]
    )
    assert code == 2 and shown is None
    assert "the first account resets at" in err
    code, _, _ = run(
        capsys, monkeypatch, ["route", "show", "codex", "--move", "nobody"]
    )
    assert code == 2
    # A pin is the owner's choice and a route is the node's own: neither moves.
    pinned = [
        "route",
        "show",
        "codex",
        "--move",
        "claude,codex,qwen,agy",
        "--account",
        "codex-a",
    ]
    code, shown, err = run(capsys, monkeypatch, pinned)
    assert code == 2 and shown is None
    recorded = [
        "route",
        "show",
        "codex",
        "--move",
        "claude,codex,qwen,agy",
        "--route",
        route_of(root, "codex-a"),
    ]
    code, shown, err = run(capsys, monkeypatch, recorded)
    assert code == 0, err
    assert shown is not None and shown["route"] == route_of(root, "codex-a")
    assert "alert" not in shown and "provider" not in shown


def test_the_move_never_takes_a_named_only_provider(
    root: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    roster = root / "spawn.toml"
    roster.write_text(
        roster.read_text().replace(
            'name = "antigravity"\n', 'name = "antigravity"\nnamed_only = true\n'
        )
    )
    for alias in ("codex-a", "codex-b", "codex-c"):
        wall(alias, int(time.time()) + 600)
    arguments = ["route", "show", "codex", "--move", "agy"]
    code, shown, err = run(capsys, monkeypatch, arguments)
    assert code == 2 and shown is None
    assert 'no other provider serves: agy: provider "antigravity" is named_only' in err


def test_the_fallback_never_takes_an_api_key_account(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("NOTT_NODE_CODEX_ADAPTER", "CODEX_PATH", "CODEX_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-home"))
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "keyed.token").write_text("not-a-real-key\n")
    roster = tmp_path / "spawn.toml"
    roster.write_text(
        f'[bins]\ncodex = "{tmp_path / "codex"}"\n\n'
        '[[provider]]\nname = "codex"\nkind = "codex"\nmodel = "gpt-m"\n'
        f'[provider.accounts.codex-a]\nhome = "{tmp_path / "codex-a"}"\n\n'
        '[[provider]]\nname = "claude"\nkind = "claude"\n'
        f'model = "claude-opus-5"\nsecrets = "{secrets}"\n'
        '[provider.accounts.keyed]\nauth_env = "ANTHROPIC_API_KEY"\n'
    )
    monkeypatch.setenv("NOTT_NSPAWN_ROSTER", str(roster))
    wall("codex-a", int(time.time()) + 600)
    code, shown, err = run(
        capsys,
        monkeypatch,
        ["route", "show", "codex", "--move", "claude,codex,qwen,agy"],
    )
    assert code == 2 and shown is None
    assert 'every allowed account of provider "codex" is cooling down' in err
    assert "no other provider serves: claude: " in err


def test_an_agy_provider_is_found_by_its_kind(
    root: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    live_run("agy1", "r-g")
    code, shown, err = run(capsys, monkeypatch, ["route", "show", "agy"])
    assert code == 0, err
    assert shown is not None and shown["route"] == route_of(root, "agy2")


def test_unreadable_walls_pick_no_account(
    root: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A directory where the registry file belongs: no store opens there.
    store_path().mkdir(parents=True)
    code, shown, err = run(capsys, monkeypatch, ["route", "show", "codex"])
    assert code != 0 and shown is None
    assert "account walls cannot be read; no account is picked" in err
    # A node's own route needs no store, as before.
    code, shown, err = run(
        capsys,
        monkeypatch,
        ["route", "show", "codex", "--route", route_of(root, "codex-b")],
    )
    assert code == 0, err
    assert shown is not None and shown["route"] == route_of(root, "codex-b")


def test_an_account_is_never_named_beside_a_route(
    root: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    arguments = ["route", "show", "codex", "--route", route_of(root, "codex-b")]
    code, _, _ = run(capsys, monkeypatch, [*arguments, "--account", "codex-a"])
    assert code == 2


@pytest.mark.parametrize("mode", ["load", "wall", "pin", "recorded", "all-excluded"])
def test_an_excluded_account_is_never_picked(
    root: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    roster = root / "spawn.toml"
    excluded = (
        ["codex-b"] if mode != "all-excluded" else ["codex-a", "codex-b", "codex-c"]
    )
    roster.write_text(
        roster.read_text().replace(
            'name = "codex"\n',
            'name = "codex"\nexcluded_accounts = ' + json.dumps(excluded) + "\n",
        )
    )
    live_run("codex-a", "busy-a")
    live_run("codex-c", "busy-c")
    until = int(time.time()) + 600
    if mode == "wall":
        wall("codex-a", until)
        wall("codex-c", until + 300)
    arguments = ["route", "show", "codex"]
    if mode == "pin":
        arguments += ["--account", "codex-b"]
    if mode == "recorded":
        # A node made on it before the exclusion.
        arguments += ["--route", route_of(root, "codex-b")]
    code, shown, err = run(capsys, monkeypatch, arguments)
    if mode == "load":
        assert code == 0, err
        assert shown is not None and shown["route"] == route_of(root, "codex-a")
    else:
        assert code == 2 and shown is None, (code, shown, err)
        if mode == "wall":
            assert f"the first account resets at {iso(until)}" in err
        else:
            assert "excluded" in err


def test_an_unknown_excluded_alias_is_refused(
    root: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roster = root / "spawn.toml"
    roster.write_text(
        roster.read_text().replace(
            'name = "codex"\n',
            'name = "codex"\nexcluded_accounts = ["codex-typo"]\n',
        )
    )
    store()
    code, shown, err = run(capsys, monkeypatch, ["route", "show", "codex"])
    assert code == 2 and shown is None
    assert "codex-typo" in err and "excluded_accounts" in err


def test_a_recorded_wall_survives_a_bad_exclusion_alias(
    root: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roster = root / "spawn.toml"
    roster.write_text(
        roster.read_text().replace(
            'name = "codex"\n',
            'name = "codex"\nexcluded_accounts = ["codex-typo"]\n',
        )
    )
    store()
    code, shown, err = run(
        capsys,
        monkeypatch,
        ["route", "wall", "codex", "--route", route_of(root, "codex-a")],
        {"observed_at": int(time.time()), "error": recorded("usage-limit")},
    )
    assert code == 0, err
    assert shown is not None and shown["wall"]["account_alias"] == "codex-a"
    assert "codex-typo" in shown["warning"]
    assert set(walls()) == {"codex-a"}


# --- route show, work spread over the accounts -----------------------------------------

DAY = 86400


def ended_run(alias: str, run: str, at: int) -> None:
    """A run on `alias` that started at `at` (epoch ms) and has ended: no load."""
    owner = registry()
    try:
        sql(
            owner,
            "INSERT INTO runs(run_id, worktree, phase, sandbox, model, owner_instance,"
            " created_at, updated_at, backend, executed_account)"
            " VALUES (?, ?, 'completed', 'read-only', 'gpt-m', ?, ?, ?, 'codex', ?)",
            (run, f"/tmp/{run}", owner.instance_id, at, at, alias),
        )
    finally:
        owner.close()


def usage(alias: str, *windows: tuple[str, float, int]) -> None:
    """`alias`'s windows `(name, utilization, resets_at)`, read together."""
    owner = registry()
    try:
        owner.record_account_usage(
            tuple(
                AccountUsageRow(alias, name, used, resets_at, int(time.time()))
                for name, used, resets_at in windows
            )
        )
    finally:
        owner.close()


def picked(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> str:
    """The account `route show` gives a new node."""
    code, shown, err = run(capsys, monkeypatch, ["route", "show", "codex"])
    assert code == 0 and shown is not None, err
    return str(shown["route"]).partition("@")[0]


def test_idle_accounts_with_no_data_take_turns(
    root: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    store()
    taken = []
    for at in (1000, 2000, 3000, 4000):
        taken.append(picked(capsys, monkeypatch))
        # The node's run on that account, ended before the next dispatch.
        ended_run(taken[-1], f"r-{at}", at)
    assert sorted(taken[:3]) == ["codex-a", "codex-b", "codex-c"]
    assert taken[3] == taken[0], "the least recently used account is next"


def test_weekly_quota_that_resets_sooner_goes_first(
    root: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    now = int(time.time())
    week = "codex.primary.seven_day"
    usage("codex-a", (week, 0.5, now + 6 * DAY))
    usage("codex-b", (week, 0.5, now + DAY))
    usage("codex-c", (week, 0.5, now + 6 * DAY))
    assert picked(capsys, monkeypatch) == "codex-b"
    # The same week with a nearly full five-hour window scores below the others.
    usage(
        "codex-b", (week, 0.5, now + DAY), ("codex.primary.five_hour", 0.9, now + 3600)
    )
    assert picked(capsys, monkeypatch) != "codex-b"


def test_a_cooled_account_is_taken_again_after_its_reset(
    root: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    now = int(time.time())
    ended_run("codex-c", "r-c", 1000)
    ended_run("codex-a", "r-a", 2000)
    ended_run("codex-b", "r-b", 3000)
    wall("codex-c", now + 600)
    usage("codex-c", ("codex.primary.seven_day", 1.0, now + 600))
    assert picked(capsys, monkeypatch) == "codex-a"
    # The reset passes: the wall and the spent window both end.
    owner = registry()
    try:
        sql(owner, "UPDATE account_walls SET cooling_until = ?", (now - 1,))
        sql(owner, "UPDATE account_usage SET resets_at = ?", (now - 1,))
    finally:
        owner.close()
    assert picked(capsys, monkeypatch) == "codex-c"


def test_parallel_runs_spread_by_occupancy(
    root: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    store()
    taken = []
    for name in ("p-1", "p-2", "p-3"):
        taken.append(picked(capsys, monkeypatch))
        live_run(taken[-1], name)
    assert sorted(taken) == ["codex-a", "codex-b", "codex-c"]


# --- a node on an account it must leave ------------------------------------------------


def test_a_node_on_a_cooling_account_keeps_its_route_until_its_wall_moves_it(
    root: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    now = int(time.time())
    wall("codex-a", now + 600)
    route = route_of(root, "codex-a")
    # Its session and history live in that account's home: the route resolves.
    code, shown, err = run(
        capsys, monkeypatch, ["route", "show", "codex", "--route", route]
    )
    assert code == 0 and shown is not None and shown["route"] == route, err
    # Its refused prompt walls the account, and the next pick is another one.
    code, shown, err = run(
        capsys,
        monkeypatch,
        ["route", "wall", "codex", "--route", route],
        {"observed_at": now, "error": recorded("usage-limit")},
    )
    assert code == 0 and shown is not None, err
    assert shown["wall"]["account_alias"] == "codex-a"
    assert picked(capsys, monkeypatch) != "codex-a"


def test_a_node_on_an_excluded_account_still_records_its_wall(
    root: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    roster = root / "spawn.toml"
    roster.write_text(
        roster.read_text().replace(
            'name = "codex"\n', 'name = "codex"\nexcluded_accounts = ["codex-b"]\n'
        )
    )
    store()
    # Without its wall the host cannot move the node off the account.
    code, shown, err = run(
        capsys,
        monkeypatch,
        ["route", "wall", "codex", "--route", route_of(root, "codex-b")],
        {"observed_at": int(time.time()), "error": recorded("usage-limit")},
    )
    assert code == 0 and shown is not None, err
    assert shown["wall"]["account_alias"] == "codex-b"
    assert picked(capsys, monkeypatch) != "codex-b"
