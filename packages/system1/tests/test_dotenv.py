"""System1 resolves private file values without a shell parent."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import nott_system1 as system1
from nott_system1.renamed import RENAMED, RETIRED


def private_file(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o600)
    return path


def test_empty_environment_process_reads_private_endpoint(tmp_path: Path) -> None:
    private_file(
        tmp_path / ".nott/.env", "NOTT_SYSTEM1_ENDPOINT=http://127.0.0.1:9/ask\n"
    )
    source = (
        "from pathlib import Path\n"
        f"Path.home = classmethod(lambda cls: Path({str(tmp_path)!r}))\n"
        "from nott_system1 import route\n"
        "print(route().endpoint)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", source],
        env={},
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "http://127.0.0.1:9/ask"
    assert result.stderr == ""


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("  NOTT_SYSTEM1_MODEL=m  \n", "m"),
        ("\n# NOTT_SYSTEM1_MODEL=x\nNOTT_SYSTEM1_MODEL=m", "m"),
        ("export NOTT_SYSTEM1_MODEL=m", "m"),
        ("export  NOTT_SYSTEM1_MODEL=x\nNOTT_SYSTEM1_MODEL=m", "m"),
        ("NOTT_SYSTEM1_MODEL=a=b", "a=b"),
        ("NOTT_SYSTEM1_MODEL\nNOTT_SYSTEM1_MODEL=m", "m"),
        ("NOTT_SYSTEM1_MODEL=m#kept", "m#kept"),
        ("NOTT_SYSTEM1_MODEL=  m  ", "m"),
        ('NOTT_SYSTEM1_MODEL=""m"', "m"),
        ("NOTT_SYSTEM1_MODEL=''m'", "m"),
        ("NOTT_SYSTEM1_MODEL=\nNOTT_SYSTEM1_MODEL=m", "m"),
        ("NOTT_SYSTEM1_MODEL=first\nNOTT_SYSTEM1_MODEL=last", "first"),
        ("OTHER_KEY=discard\nNOTT_SYSTEM1_MODEL=$HOME/$(literal)", "$HOME/$(literal)"),
    ],
)
def test_fish_assignment_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str, expected: str
) -> None:
    path = private_file(tmp_path / ".env", text)
    monkeypatch.setenv("NOTT_TEST_SYSTEM1_FILE", str(path))
    monkeypatch.delenv("NOTT_SYSTEM1_MODEL", raising=False)
    assert system1.route().model == expected


@pytest.mark.parametrize("value", ["process", ""])
def test_process_presence_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    path = private_file(tmp_path / ".env", "NOTT_SYSTEM1_MODEL=file")
    monkeypatch.setenv("NOTT_TEST_SYSTEM1_FILE", str(path))
    monkeypatch.setenv("NOTT_SYSTEM1_MODEL", value)
    assert system1.route().model == (value or None)


def test_changed_home_does_not_read_another_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private_file(tmp_path / "owner/.nott/.env", "NOTT_SYSTEM1_MODEL=owner")
    monkeypatch.setenv("HOME", str(tmp_path / "worker"))
    monkeypatch.delenv("NOTT_TEST_SYSTEM1_FILE")
    assert system1.route().model is None


def test_no_environment_or_file_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = private_file(tmp_path / ".env", "NOTT_SYSTEM1_MODEL=file\nOTHER_KEY=discard")
    monkeypatch.setenv("NOTT_TEST_SYSTEM1_FILE", str(path))
    before_file = path.read_bytes()
    before_env = hashlib.sha256(repr(sorted(os.environ.items())).encode()).digest()
    assert system1.route().model == "file"
    after_env = hashlib.sha256(repr(sorted(os.environ.items())).encode()).digest()
    assert path.read_bytes() == before_file
    assert after_env == before_env


@pytest.mark.parametrize("scope", sorted(system1.SCOPES))
def test_scoped_file_route_and_partial_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scope: str
) -> None:
    endpoint, model, key = system1.route_variables(scope)
    full = private_file(
        tmp_path / "full",
        f"{endpoint}=http://127.0.0.1:9/{scope}\n{model}=scoped\n{key}=test-key",
    )
    monkeypatch.setenv("NOTT_TEST_SYSTEM1_FILE", str(full))
    route = system1.route(scope)
    assert (route.endpoint, route.model, route.auth_env) == (
        f"http://127.0.0.1:9/{scope}",
        "scoped",
        key,
    )
    partial = private_file(
        tmp_path / "partial", f"{endpoint}=http://127.0.0.1:9/{scope}"
    )
    monkeypatch.setenv("NOTT_TEST_SYSTEM1_FILE", str(partial))
    with pytest.raises(system1.System1Unavailable, match=key):
        system1.route(scope)


@pytest.mark.parametrize("old", [*RENAMED.values(), *RETIRED])
def test_old_file_spelling_is_refused_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, old: str
) -> None:
    path = private_file(tmp_path / ".env", f"{old}=test-secret")
    monkeypatch.setenv("NOTT_TEST_SYSTEM1_FILE", str(path))
    with pytest.raises(system1.System1Unavailable, match=old) as failure:
        system1.route()
    assert "test-secret" not in str(failure.value)


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o620, 0o602])
def test_wide_mode_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: int
) -> None:
    path = private_file(tmp_path / ".env", "NOTT_SYSTEM1_API_KEY=test-secret")
    path.chmod(mode)
    monkeypatch.setenv("NOTT_TEST_SYSTEM1_FILE", str(path))
    with pytest.raises(
        system1.System1Unavailable, match="file mode must be private"
    ) as failure:
        system1.route()
    assert "test-secret" not in str(failure.value)


@pytest.mark.parametrize(
    "fault", ["symlink", "hardlink", "directory", "large", "utf8", "owner"]
)
def test_unsafe_file_refusal_has_no_secret_or_parser_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    path = private_file(tmp_path / ".env", "NOTT_SYSTEM1_API_KEY=test-secret")
    if fault == "symlink":
        link = tmp_path / "link"
        link.symlink_to(path)
        path = link
    elif fault == "hardlink":
        os.link(path, tmp_path / "other")
    elif fault == "directory":
        path = tmp_path
    elif fault == "large":
        path.write_text("x" * (64 * 1024 + 1))
    elif fault == "utf8":
        path.write_bytes(b"NOTT_SYSTEM1_API_KEY=test-secret\xff")
    elif fault == "owner":
        uid = os.geteuid()
        monkeypatch.setattr(system1.os, "geteuid", lambda: uid + 1)
    monkeypatch.setenv("NOTT_TEST_SYSTEM1_FILE", str(path))
    with pytest.raises(system1.System1Unavailable) as failure:
        system1.route()
    assert str(path) in str(failure.value)
    assert "test-secret" not in str(failure.value)
    assert failure.value.__suppress_context__ or failure.value.__context__ is None


def test_cache_retains_only_system1_and_restart_loads_edits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = private_file(
        tmp_path / ".env", "NOTT_SYSTEM1_MODEL=first\nOTHER_KEY=unrelated"
    )
    monkeypatch.setenv("NOTT_TEST_SYSTEM1_FILE", str(path))
    assert system1._dotenv(path) == {"NOTT_SYSTEM1_MODEL": "first"}
    path.write_text("NOTT_SYSTEM1_MODEL=second")
    assert system1.route().model == "first"
    child = subprocess.run(
        [sys.executable, "-c", "from nott_system1 import route; print(route().model)"],
        env={"NOTT_TEST_SYSTEM1_FILE": str(path)},
        capture_output=True,
        text=True,
        check=True,
    )
    assert child.stdout.strip() == "second"


def test_custom_auth_name_has_no_file_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = private_file(tmp_path / ".env", "CUSTOM_KEY=test-secret")
    monkeypatch.setenv("NOTT_TEST_SYSTEM1_FILE", str(path))
    assert system1.env("CUSTOM_KEY") is None


def test_read_access_time_is_not_a_file_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = private_file(tmp_path / ".env", "NOTT_SYSTEM1_MODEL=read")
    monkeypatch.setenv("NOTT_TEST_SYSTEM1_FILE", str(path))
    original = os.fstat
    calls = 0

    def accessed(fd: int) -> os.stat_result:
        nonlocal calls
        metadata = original(fd)
        calls += 1
        fields = list(metadata)
        if calls == 2:
            fields[7] += 1
        return os.stat_result(
            fields,
            {
                "st_mtime_ns": metadata.st_mtime_ns,
                "st_ctime_ns": metadata.st_ctime_ns,
            },
        )

    monkeypatch.setattr(system1.os, "fstat", accessed)
    assert system1.route().model == "read"


def test_concurrent_first_reads_keep_one_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = private_file(tmp_path / ".env", "NOTT_SYSTEM1_MODEL=one")
    monkeypatch.setenv("NOTT_TEST_SYSTEM1_FILE", str(path))
    original = os.open
    reads = 0

    def slow_open(target: Path, flags: int) -> int:
        nonlocal reads
        reads += 1
        time.sleep(0.05)
        return original(target, flags)

    with monkeypatch.context() as patch:
        patch.setattr(system1.os, "open", slow_open)
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert list(pool.map(system1.env, ["NOTT_SYSTEM1_MODEL"] * 2)) == [
                "one",
                "one",
            ]
    assert reads == 1


def test_partial_process_question_never_uses_the_base_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in zip(
        system1.route_variables(), ["http://127.0.0.1:9/base", "base", "base-key"]
    ):
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("NOTT_SYSTEM1_TAINT_ENDPOINT", "http://127.0.0.1:9/question")
    with pytest.raises(system1.System1Unavailable, match="NOTT_SYSTEM1_TAINT_API_KEY"):
        system1.route("TAINT")
