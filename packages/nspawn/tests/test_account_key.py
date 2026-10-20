"""The owner pastes an account's key in the app, and nSPAWN writes its token file. Every secrets directory and roster here is under tmp; no message names
the key."""

from __future__ import annotations

import contextlib
import io
import os
import stat
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from nspawn import owner_roster
from nspawn.owner_roster import (
    Roster,
    RosterEditRefused,
    make_default_account,
    write_account_key,
)

KEY = "fixture-" + "NEWKEY-0123456789"

ROSTER = """\
[bins]
codex = "/bin/true"
agy = "/bin/true"

[[provider]]
name = "claude"
kind = "claude"
secrets = "{root}/secrets/claude"

[[provider]]
name = "zai"
kind = "claude"
secrets = "{root}/secrets/zai"
auth_env = "ANTHROPIC_AUTH_TOKEN"

[[provider]]
name = "codex"
kind = "codex"
model = "gpt"

[provider.accounts.main]
home = "{root}/codex-main"

[[provider]]
name = "agy"
kind = "agy"
model = "gemini"

[provider.accounts.agy1]
home = "{root}"

[[provider]]
name = "qwen"
kind = "claude"
secrets = "{root}/secrets/qwen"

[provider.accounts.server]
base_url = "http://127.0.0.1:1"
auth_env = "ANTHROPIC_AUTH_TOKEN"
"""


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in ("claude", "zai", "qwen"):
        (tmp_path / "secrets" / name).mkdir(mode=0o700, parents=True)
    (tmp_path / "secrets" / "claude" / "first.token").write_text("old-first\n")
    (tmp_path / "secrets" / "zai" / "zai.token").write_text("old-zai\n")
    (tmp_path / "secrets" / "qwen" / "server.token").write_text("old-qwen\n")
    roster = tmp_path / "spawn.toml"
    roster.write_text(ROSTER.format(root=tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _leftovers(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if not p.name.endswith(".token"))


@pytest.mark.parametrize(
    ("provider", "alias"), [("claude", "first"), ("zai", "zai"), ("qwen", "server")]
)
def test_a_key_replaces_the_token_whole_and_the_roster_stays(
    host: Path, provider: str, alias: str
) -> None:
    roster = host / "spawn.toml"
    before = roster.read_bytes()
    write_account_key(roster, provider, alias, KEY, new=False)
    token = host / "secrets" / provider / f"{alias}.token"
    assert token.read_text() == KEY + "\n"
    assert stat.S_IMODE(token.stat().st_mode) == 0o600
    assert _leftovers(token.parent) == []
    assert roster.read_bytes() == before
    assert sorted(roster.parent.glob("spawn.toml.bak-*")) == []


def test_a_failed_replace_keeps_the_old_key_and_no_staged_file(
    host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_: object, **__: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(owner_roster.os, "replace", broken)
    with pytest.raises(OSError) as failed:
        write_account_key(host / "spawn.toml", "claude", "first", KEY, new=False)
    assert KEY not in str(failed.value)
    directory = host / "secrets" / "claude"
    assert (directory / "first.token").read_text() == "old-first\n"
    assert _leftovers(directory) == []


def test_a_linked_token_is_refused_and_both_ends_stay(host: Path) -> None:
    elsewhere = host / "elsewhere.token"
    elsewhere.write_text("old-elsewhere\n")
    link = host / "secrets" / "claude" / "first.token"
    link.unlink()
    link.symlink_to(elsewhere)
    with pytest.raises(RosterEditRefused, match="change it where it points"):
        write_account_key(host / "spawn.toml", "claude", "first", KEY, new=False)
    assert link.is_symlink() and link.readlink() == elsewhere
    assert elsewhere.read_text() == "old-elsewhere\n"
    assert _leftovers(link.parent) == []


@pytest.mark.parametrize(
    ("provider", "alias", "key", "says"),
    [
        ("claude", "first", "", "a key is"),
        ("claude", "first", "x" * 4097, "a key is"),
        ("claude", "first", f"{KEY} tail", "a key is"),
        ("claude", "first", f"{KEY}\n", "a key is"),
        ("claude", "first", f"{KEY}\x00", "a key is"),
        ("claude", "first", f"{KEY}\t", "a key is"),
        ("claude", "../first", KEY, "one file name"),
        ("claude", "a/b", KEY, "one file name"),
        ("claude", "..", KEY, "one file name"),
        ("claude", "ghost", KEY, "has no such account"),
        ("nobody", "first", KEY, "no such provider"),
        ("claude", "a\x00b", KEY, "one file name"),
        ("codex", "main", KEY, "log in on the host"),
        ("agy", "agy1", KEY, "log in on the host"),
    ],
)
def test_what_cannot_be_written_is_refused_without_the_key(
    host: Path, provider: str, alias: str, key: str, says: str
) -> None:
    roster = host / "spawn.toml"
    before = roster.read_bytes()
    tokens = {p: p.read_bytes() for p in (host / "secrets").rglob("*") if p.is_file()}
    with pytest.raises(RosterEditRefused, match=says) as refused:
        write_account_key(roster, provider, alias, key, new=False)
    if key:
        assert key.strip() not in str(refused.value)
    assert roster.read_bytes() == before
    assert {
        p: p.read_bytes() for p in (host / "secrets").rglob("*") if p.is_file()
    } == tokens


@pytest.mark.parametrize("mode", [0o750, 0o755])
def test_a_secrets_directory_others_can_read_is_refused(host: Path, mode: int) -> None:
    directory = host / "secrets" / "claude"
    directory.chmod(mode)
    with pytest.raises(RosterEditRefused, match="0700") as refused:
        write_account_key(host / "spawn.toml", "claude", "first", KEY, new=False)
    assert KEY not in str(refused.value)
    assert (directory / "first.token").read_text() == "old-first\n"


def test_a_secrets_directory_of_another_user_is_refused(
    host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(owner_roster.os, "getuid", lambda: os.stat(host).st_uid + 1)
    with pytest.raises(RosterEditRefused, match="0700"):
        write_account_key(host / "spawn.toml", "claude", "first", KEY, new=False)
    assert (host / "secrets/claude/first.token").read_text() == "old-first\n"


def test_a_new_account_gets_its_key_then_joins_the_roster(host: Path) -> None:
    roster = host / "spawn.toml"
    make_default_account(roster, "claude", "first")
    write_account_key(roster, "claude", "work", KEY, new=True)
    token = host / "secrets/claude/work.token"
    assert token.read_text() == KEY + "\n"
    assert stat.S_IMODE(token.stat().st_mode) == 0o600
    assert _leftovers(token.parent) == []
    found = Roster.read(roster).select("claude")
    assert found is not None and found.ordered_accounts() == ["first", "work"]


@pytest.mark.parametrize(
    ("provider", "alias", "says"),
    [
        ("claude", "work", "not declared yet"),
        ("qwen", "work", "no login"),
        ("claude", "first", "already there"),
        ("claude", "zai", "names a provider"),
    ],
)
def test_a_refused_registration_removes_the_new_file(
    host: Path, provider: str, alias: str, says: str
) -> None:
    roster = host / "spawn.toml"
    before = roster.read_bytes()
    directory = host / "secrets" / provider
    files = {p.name: p.read_bytes() for p in directory.iterdir()}
    with pytest.raises(RosterEditRefused, match=says) as refused:
        write_account_key(roster, provider, alias, KEY, new=True)
    assert KEY not in str(refused.value)
    assert {p.name: p.read_bytes() for p in directory.iterdir()} == files
    assert roster.read_bytes() == before


@pytest.mark.parametrize("new", [False, True])
@pytest.mark.parametrize(
    ("provider", "alias"), [("claude", KEY), (KEY, "first"), ("zai", KEY)]
)
def test_no_refusal_quotes_what_the_caller_sent(
    host: Path, provider: str, alias: str, new: bool
) -> None:
    """Codex 2b6967e9 #1: the key pasted in the name field came back in the refusal."""
    roster = host / "spawn.toml"
    before = roster.read_bytes()
    tokens = {p: p.read_bytes() for p in (host / "secrets").rglob("*") if p.is_file()}
    with pytest.raises(RosterEditRefused) as refused:
        write_account_key(roster, provider, alias, KEY, new=new)
    assert KEY not in str(refused.value)
    assert roster.read_bytes() == before
    assert {
        p: p.read_bytes() for p in (host / "secrets").rglob("*") if p.is_file()
    } == tokens


def test_a_vanished_token_never_redirects_the_key(
    host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex 2b6967e9 #3: `zai.token` gone after the account check made the write
    fall back to the first discovered account's file."""
    directory = host / "secrets" / "zai"
    (directory / "a.token").write_text("old-a\n")
    (directory / "zai.token").unlink()
    monkeypatch.setattr(
        owner_roster.Provider, "ordered_accounts", lambda self: ["a", "zai"]
    )
    write_account_key(host / "spawn.toml", "zai", "zai", KEY, new=False)
    assert (directory / "a.token").read_text() == "old-a\n"
    assert (directory / "zai.token").read_text() == KEY + "\n"


def test_a_refused_add_removes_only_the_file_it_made(
    host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex 2b6967e9 #2: the rollback deleted a key another writer had put there."""
    roster = host / "spawn.toml"
    make_default_account(roster, "claude", "first")
    token = host / "secrets/claude/work.token"

    def replaced_then_refused(*_: object) -> None:
        other = token.with_name("other")
        other.write_text("theirs\n")
        os.replace(other, token)
        raise RosterEditRefused("refused meanwhile")

    monkeypatch.setattr(owner_roster, "register_account", replaced_then_refused)
    with pytest.raises(RosterEditRefused, match="refused meanwhile"):
        write_account_key(roster, "claude", "work", KEY, new=True)
    assert token.read_text() == "theirs\n"


def test_a_refused_add_never_makes_its_file(
    host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude 71d75244 #2: the new file was a discovered account, and could sort
    first as the default, before the registration refused it."""
    made: list[object] = []
    monkeypatch.setattr(owner_roster.os, "link", lambda *a, **k: made.append(a))
    with pytest.raises(RosterEditRefused, match="not declared yet"):
        write_account_key(host / "spawn.toml", "claude", "aaa", KEY, new=True)
    assert made == []
    assert not (host / "secrets/claude/aaa.token").exists()


def test_a_hard_linked_token_is_refused_and_both_names_stay(host: Path) -> None:
    token = host / "secrets" / "claude" / "first.token"
    other = host / "copy.token"
    os.link(token, other)
    with pytest.raises(RosterEditRefused, match="change it where it points"):
        write_account_key(host / "spawn.toml", "claude", "first", KEY, new=False)
    assert token.read_text() == other.read_text() == "old-first\n"
    assert os.stat(token).st_ino == os.stat(other).st_ino


def test_a_linked_secrets_directory_is_refused(host: Path) -> None:
    """Codex 2b6967e9 #4: the directory is checked and written through one handle,
    never a path a link can be swapped into."""
    real = host / "secrets" / "claude"
    moved = host / "elsewhere"
    real.rename(moved)
    real.symlink_to(moved)
    with pytest.raises(RosterEditRefused, match="0700"):
        write_account_key(host / "spawn.toml", "claude", "first", KEY, new=False)
    assert (moved / "first.token").read_text() == "old-first\n"


def test_writers_of_one_directory_take_turns(host: Path) -> None:
    """One writer at a time holds the secrets directory, from the check to the
    rollback; another waits for it."""
    import fcntl
    import threading

    directory = host / "secrets" / "claude"
    held = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    fcntl.flock(held, fcntl.LOCK_EX)
    writer = threading.Thread(
        target=write_account_key,
        args=(host / "spawn.toml", "claude", "first", KEY),
        kwargs={"new": False},
    )
    try:
        writer.start()
        writer.join(0.5)
        assert writer.is_alive(), "the key was written while another writer held it"
        assert (directory / "first.token").read_text() == "old-first\n"
    finally:
        os.close(held)
    writer.join(5)
    assert (directory / "first.token").read_text() == KEY + "\n"


def test_the_staged_name_is_new_each_time(
    host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude 71d75244 #5: a staged name derived from the pid alone could already
    belong to another writer when the cleanup removed it."""
    names: list[str] = []
    real = os.replace

    def seen(src: object, dst: object, **kwargs: object) -> None:
        names.append(str(src))
        real(src, dst, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(owner_roster.os, "replace", seen)
    for _ in range(2):
        write_account_key(host / "spawn.toml", "claude", "first", KEY, new=False)
    assert len(names) == 2 and names[0] != names[1]


def _recorded(monkeypatch: pytest.MonkeyPatch, *failing: str) -> list[str]:
    """The writer's directory operations in order: link, unlink, replace, and a sync
    of the directory (never of the staged file). Each label in ``failing`` fails the
    next call it names, once, with EIO."""
    calls: list[str] = []
    pending = list(failing)
    real = {n: getattr(os, n) for n in ("link", "unlink", "replace", "fsync")}

    def named(name: str) -> object:
        def call(*args: object, **kwargs: object) -> object:
            if name == "fsync" and not stat.S_ISDIR(os.fstat(args[0]).st_mode):  # type: ignore[arg-type]
                return real[name](*args, **kwargs)
            first = str(args[0])
            label = (
                "sync-dir"
                if name == "fsync"
                else f"{name} {'staged' if first.endswith('.tmp') else first}"
            )
            calls.append(label)
            if label in pending:
                pending.remove(label)
                raise OSError(5, "Input/output error")
            return real[name](*args, **kwargs)

        return call

    for name in real:
        monkeypatch.setattr(owner_roster.os, name, named(name))
    return calls


def test_a_new_key_is_synced_after_its_staged_name_goes(
    host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude 963e58e5 A: the directory was synced while the staged name still
    linked the key, so a crash could leave a second name for it."""
    roster = host / "spawn.toml"
    make_default_account(roster, "claude", "first")
    calls = _recorded(monkeypatch)
    write_account_key(roster, "claude", "work", KEY, new=True)
    assert calls[:3] == ["link staged", "unlink staged", "sync-dir"], calls


def test_a_failed_sync_of_a_new_key_removes_it(
    host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex 9cbe1550 #1, Claude 963e58e5 B: a sync that failed after the link left
    the file, unregistered, while the answer said nothing changed. Removed and
    synced, nothing did change."""
    roster = host / "spawn.toml"
    make_default_account(roster, "claude", "first")
    declared = roster.read_bytes()
    calls = _recorded(monkeypatch, "sync-dir")
    with pytest.raises(OSError):
        write_account_key(roster, "claude", "work", KEY, new=True)
    assert not (host / "secrets/claude/work.token").exists()
    assert roster.read_bytes() == declared
    assert calls[-3:] == ["unlink work.token", "unlink staged", "sync-dir"], calls


@pytest.mark.parametrize("cleanup", ["unlink work.token", "sync-dir"])
def test_an_add_whose_rollback_fails_says_its_file_may_remain(
    host: Path, monkeypatch: pytest.MonkeyPatch, cleanup: str
) -> None:
    """Codex 4ed9c3fa #1: the rollback's own failure replaced the first one, and the
    answer said every key was as it was while the new file stayed."""
    make_default_account(host / "spawn.toml", "claude", "first")
    _recorded(monkeypatch, "sync-dir", cleanup)
    with pytest.raises(owner_roster.KeyStateUnknown, match="may remain") as failed:
        write_account_key(host / "spawn.toml", "claude", "work", KEY, new=True)
    assert KEY not in str(failed.value)
    assert isinstance(failed.value.__cause__, OSError)


def test_a_refused_add_whose_rollback_fails_keeps_its_reason(
    host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude 61aeda39: a refused registration's own reason was lost when its
    cleanup failed."""
    make_default_account(host / "spawn.toml", "claude", "first")
    _recorded(monkeypatch, "unlink work.token")
    monkeypatch.setattr(
        owner_roster,
        "register_account",
        lambda *_: (_ for _ in ()).throw(RosterEditRefused("refused meanwhile")),
    )
    with pytest.raises(owner_roster.KeyStateUnknown) as failed:
        write_account_key(host / "spawn.toml", "claude", "work", KEY, new=True)
    assert "refused meanwhile" in str(failed.value)
    assert "may remain" in str(failed.value)
    assert isinstance(failed.value.__cause__, RosterEditRefused)


def test_a_staged_name_that_would_not_go_is_gone_before_the_last_sync(
    host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex 4ed9c3fa #2: the retry removed the staged name after the last sync, so
    a crash could bring the key back under it."""
    make_default_account(host / "spawn.toml", "claude", "first")
    calls = _recorded(monkeypatch, "unlink staged")
    with pytest.raises(OSError):
        write_account_key(host / "spawn.toml", "claude", "work", KEY, new=True)
    directory = host / "secrets" / "claude"
    assert _leftovers(directory) == [] and not (directory / "work.token").exists()
    assert calls[-1] == "sync-dir" and "unlink staged" in calls[-3:], calls


def test_a_failed_replace_syncs_its_cleanup(
    host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex 4ed9c3fa #2: every cleanup ends in a sync of the directory."""
    calls = _recorded(monkeypatch, "replace staged")
    with pytest.raises(OSError) as failed:
        write_account_key(host / "spawn.toml", "claude", "first", KEY, new=False)
    assert not isinstance(failed.value, owner_roster.KeyStateUnknown)
    assert calls[-2:] == ["unlink staged", "sync-dir"], calls


@pytest.mark.parametrize("cleanup", ["unlink staged", "sync-dir"])
def test_a_failed_replace_whose_cleanup_fails_cannot_confirm_the_key(
    host: Path, monkeypatch: pytest.MonkeyPatch, cleanup: str
) -> None:
    """Codex 4ed9c3fa #1: only a writer that confirmed its cleanup may let the
    answer say every key is as it was."""
    _recorded(monkeypatch, "replace staged", cleanup)
    with pytest.raises(
        owner_roster.KeyStateUnknown, match="could not be confirmed"
    ) as failed:
        write_account_key(host / "spawn.toml", "claude", "first", KEY, new=False)
    assert isinstance(failed.value.__cause__, OSError)
    assert (host / "secrets/claude/first.token").read_text() == "old-first\n"


def test_a_refused_add_syncs_its_removal(
    host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex 9cbe1550 #3: a removed token could come back after a crash."""
    calls = _recorded(monkeypatch)
    monkeypatch.setattr(
        owner_roster,
        "register_account",
        lambda *_: (_ for _ in ()).throw(RosterEditRefused("refused meanwhile")),
    )
    make_default_account(host / "spawn.toml", "claude", "first")
    with pytest.raises(RosterEditRefused):
        write_account_key(host / "spawn.toml", "claude", "work", KEY, new=True)
    assert calls[-3:] == ["unlink work.token", "unlink staged", "sync-dir"], calls


def test_a_replaced_key_that_could_not_be_synced_says_so(
    host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex 9cbe1550 #1: after the replace the new key is in place; the failure
    must not claim the old one is."""
    _recorded(monkeypatch, "sync-dir")
    with pytest.raises(
        owner_roster.KeyStateUnknown, match="could not be confirmed"
    ) as failed:
        write_account_key(host / "spawn.toml", "claude", "first", KEY, new=False)
    assert KEY not in str(failed.value)
    assert (host / "secrets/claude/first.token").read_text() == KEY + "\n"


def test_a_failed_link_never_removes_a_file_that_reuses_its_inode(
    host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude 92d2efb8: the cleanup freed the staged inode before it checked the new
    name against it, so a file written by hand meanwhile on that reused inode was
    removed."""
    make_default_account(host / "spawn.toml", "claude", "first")
    token = host / "secrets" / "claude" / "work.token"
    made: list[int] = []
    real_stat, real_unlink = os.stat, os.unlink

    def broken_link(src: str, *_: object, **kwargs: int) -> None:
        made.append(real_stat(src, dir_fd=kwargs["src_dir_fd"]).st_ino)
        raise OSError(5, "Input/output error")

    def unlink(path: str, *args: object, **kwargs: object) -> None:
        real_unlink(path, *args, **kwargs)  # type: ignore[arg-type]
        if path.endswith(".tmp"):
            token.write_text("the owner's\n")

    def reused(path: str, *args: object, **kwargs: object) -> os.stat_result:
        found = real_stat(path, *args, **kwargs)  # type: ignore[arg-type]
        if path == "work.token" and made:
            return os.stat_result((found[0], made[0], *found[2:10]))
        return found

    monkeypatch.setattr(owner_roster.os, "link", broken_link)
    monkeypatch.setattr(owner_roster.os, "unlink", unlink)
    monkeypatch.setattr(owner_roster.os, "stat", reused)
    with pytest.raises(OSError):
        write_account_key(host / "spawn.toml", "claude", "work", KEY, new=True)
    assert token.read_text() == "the owner's\n"


def test_a_refused_add_never_removes_a_file_that_reuses_its_inode(
    host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude r5: after the staged name went, only the new name held the key's inode;
    the owner replaced that file by hand while the add waited, and the cleanup of the
    refused add removed the owner's file once its inode number came back. The model:
    a number the kernel frees is reused at once, unless a descriptor still holds it."""
    make_default_account(host / "spawn.toml", "claude", "first")
    token = host / "secrets" / "claude" / "work.token"
    real_stat = os.stat
    made: list[os.stat_result] = []

    def by_hand(*_: object) -> None:
        made.append(real_stat(token))
        token.unlink()
        token.write_text("the owner's\n")
        raise RosterEditRefused("already has an account 'work'")

    def held(found: os.stat_result) -> bool:
        def same(fd: str) -> bool:
            with contextlib.suppress(OSError, ValueError):
                opened = os.fstat(int(fd))
                return (opened.st_dev, opened.st_ino) == (found.st_dev, found.st_ino)
            return False

        return any(same(fd) for fd in os.listdir("/dev/fd"))

    def reused(path: str, *args: object, **kwargs: object) -> os.stat_result:
        found = real_stat(path, *args, **kwargs)  # type: ignore[arg-type]
        if path == "work.token" and made and not held(made[0]):
            return os.stat_result((found[0], made[0].st_ino, *found[2:10]))
        return found

    monkeypatch.setattr(owner_roster, "register_account", by_hand)
    monkeypatch.setattr(owner_roster.os, "stat", reused)
    with pytest.raises(RosterEditRefused):
        write_account_key(host / "spawn.toml", "claude", "work", KEY, new=True)
    assert token.read_text() == "the owner's\n"


def test_a_name_that_would_not_go_still_removes_the_staged_copy(
    host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude r5: a failure removing the new name skipped the staged name, and a
    hidden copy of the key stayed."""
    make_default_account(host / "spawn.toml", "claude", "first")
    _recorded(monkeypatch, "unlink staged", "unlink work.token")
    with pytest.raises(owner_roster.KeyStateUnknown):
        write_account_key(host / "spawn.toml", "claude", "work", KEY, new=True)
    assert _leftovers(host / "secrets" / "claude") == []


def _failing_close(monkeypatch: pytest.MonkeyPatch) -> None:
    """The writer's descriptor, the first one the writer closes, closes, then reports
    EIO, as a failed flush on close would."""
    real_close = os.close
    closed: list[int] = []

    def close(descriptor: int) -> None:
        real_close(descriptor)
        closed.append(descriptor)
        if len(closed) == 1:
            raise OSError(5, "Input/output error")

    monkeypatch.setattr(owner_roster.os, "close", close)


def test_a_replace_whose_file_would_not_close_keeps_the_old_key(
    host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex b3b05cf7: the file closed after the replace, so its error said every key
    was as it was while the new key was in place."""
    _failing_close(monkeypatch)
    with pytest.raises(OSError) as failed:
        write_account_key(host / "spawn.toml", "claude", "first", KEY, new=False)
    assert not isinstance(failed.value, owner_roster.KeyStateUnknown)
    assert (host / "secrets/claude/first.token").read_text() == "old-first\n"
    assert _leftovers(host / "secrets" / "claude") == []


def test_an_add_whose_file_would_not_close_adds_nothing(
    host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex b3b05cf7: the file closed after the account was registered, so its
    error said nothing changed while the account and its key were there."""
    roster = host / "spawn.toml"
    make_default_account(roster, "claude", "first")
    declared = roster.read_bytes()
    _failing_close(monkeypatch)
    with pytest.raises(OSError) as failed:
        write_account_key(roster, "claude", "work", KEY, new=True)
    assert not isinstance(failed.value, owner_roster.KeyStateUnknown)
    assert roster.read_bytes() == declared
    assert not (host / "secrets/claude/work.token").exists()
    assert _leftovers(host / "secrets" / "claude") == []


def _closes_fail_after(
    monkeypatch: pytest.MonkeyPatch, owner: object, publish: str
) -> None:
    """Every descriptor closed after ``owner.publish`` (the replace or the
    registration) closes, then reports EIO."""
    published: list[bool] = []
    real_close = os.close
    real_publish = getattr(owner, publish)

    def done(*args: object, **kwargs: object) -> object:
        answer = real_publish(*args, **kwargs)
        published.append(True)
        return answer

    def close(descriptor: int) -> None:
        real_close(descriptor)
        if published:
            raise OSError(5, "Input/output error")

    monkeypatch.setattr(owner, publish, done)
    monkeypatch.setattr(owner_roster.os, "close", close)


def test_a_published_key_is_reported_whatever_a_later_close_says(
    host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex 77252e8d #1: the directory closed after the replace without a guard, so
    its error said every key was as it was while the new key was in place."""
    _closes_fail_after(monkeypatch, owner_roster.os, "replace")
    write_account_key(host / "spawn.toml", "claude", "first", KEY, new=False)
    assert (host / "secrets/claude/first.token").read_text() == KEY + "\n"


def test_a_registered_account_is_reported_whatever_a_later_close_says(
    host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex 77252e8d #1: the same after a new account was registered."""
    roster = host / "spawn.toml"
    make_default_account(roster, "claude", "first")
    _closes_fail_after(monkeypatch, owner_roster, "register_account")
    write_account_key(roster, "claude", "work", KEY, new=True)
    assert (host / "secrets/claude/work.token").read_text() == KEY + "\n"
    assert "work" in Roster.read(roster).providers[0].ordered_accounts()


@pytest.mark.parametrize("step", ["fchmod", "write", "fsync"])
def test_a_writer_that_fails_before_it_closes_leaks_no_descriptor(
    host: Path, monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    """Codex 77252e8d #2: an `os.fdopen` that failed before it returned left the raw
    descriptor open; the writer now uses the raw descriptor alone. Each step fails
    once, on the writer's first call."""
    before = sorted(os.listdir("/dev/fd"))
    real = getattr(os, step)
    calls: list[str] = []

    def failing(*args: object, **kwargs: object) -> object:
        if not calls and sys._getframe(1).f_globals is vars(owner_roster):
            calls.append(step)
            raise OSError(5, "Input/output error")
        return real(*args, **kwargs)

    monkeypatch.setattr(owner_roster.os, step, failing)
    with pytest.raises(OSError):
        write_account_key(host / "spawn.toml", "claude", "first", KEY, new=False)
    monkeypatch.undo()
    assert sorted(os.listdir("/dev/fd")) == before
    assert (host / "secrets/claude/first.token").read_text() == "old-first\n"
    assert _leftovers(host / "secrets" / "claude") == []


def test_a_narrow_umask_still_writes_the_key(host: Path) -> None:
    """Codex 77252e8d #3: under umask 0400 the staged file was created write-only,
    and the read-only descriptor opened before its mode was set was refused."""
    previous = os.umask(0o400)
    try:
        write_account_key(host / "spawn.toml", "claude", "first", KEY, new=False)
    finally:
        os.umask(previous)
    token = host / "secrets/claude/first.token"
    assert token.read_text() == KEY + "\n"
    assert stat.S_IMODE(token.stat().st_mode) == 0o600


class _BrokenStream:
    def write(self, _: str) -> int:
        raise BrokenPipeError(32, "Broken pipe")

    def flush(self) -> None:
        raise BrokenPipeError(32, "Broken pipe")


def _closed_stream() -> io.StringIO:
    stream = io.StringIO()
    stream.close()
    return stream


@pytest.mark.parametrize("stream", [_BrokenStream, _closed_stream])
def test_a_published_key_is_reported_when_its_close_cannot_even_be_logged(
    host: Path, monkeypatch: pytest.MonkeyPatch, stream: Callable[[], object]
) -> None:
    """Codex e7099bda #1, 09a0495c: the log of a failed close raised on a broken
    stderr (OSError) or a closed one (ValueError), and that error said every key
    was as it was while the new key was in place."""
    _closes_fail_after(monkeypatch, owner_roster.os, "replace")
    monkeypatch.setattr(owner_roster.sys, "stderr", stream())
    write_account_key(host / "spawn.toml", "claude", "first", KEY, new=False)
    assert (host / "secrets/claude/first.token").read_text() == KEY + "\n"


def test_a_registered_account_keeps_its_key_whatever_the_lock_close_says(
    host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex e7099bda #2: the roster's lock closed after the roster was replaced
    without a guard, so its error removed the key of an account just registered."""
    roster = host / "spawn.toml"
    make_default_account(roster, "claude", "first")
    _closes_fail_after(monkeypatch, owner_roster.os, "replace")
    write_account_key(roster, "claude", "work", KEY, new=True)
    assert (host / "secrets/claude/work.token").read_text() == KEY + "\n"
    assert "work" in Roster.read(roster).providers[0].ordered_accounts()


@pytest.mark.parametrize("step", ["fchmod", "write", "fsync"])
def test_a_roster_file_that_fails_before_it_closes_leaks_no_descriptor(
    host: Path, monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    """Codex e7099bda #3: an `os.fdopen` that failed before it returned left the
    staged roster's raw descriptor open; the roster's writer now uses the raw
    descriptor alone. Each step fails once, on the roster writer's first call."""
    roster = host / "spawn.toml"
    make_default_account(roster, "claude", "first")
    token = host / "secrets" / "claude" / "work.token"
    token.write_text("by-hand\n")
    token.chmod(0o600)
    declared = roster.read_bytes()
    beside = sorted(p.name for p in host.iterdir())
    before = sorted(os.listdir("/dev/fd"))
    real = getattr(os, step)
    calls: list[str] = []

    def failing(*args: object, **kwargs: object) -> object:
        if not calls and sys._getframe(1).f_globals is vars(owner_roster):
            calls.append(step)
            raise OSError(5, "Input/output error")
        return real(*args, **kwargs)

    monkeypatch.setattr(owner_roster.os, step, failing)
    with pytest.raises(owner_roster.RosterError):
        owner_roster.register_account(roster, "claude", "work")
    monkeypatch.undo()
    assert sorted(os.listdir("/dev/fd")) == before
    assert roster.read_bytes() == declared
    assert sorted(p.name for p in host.iterdir()) == beside


@pytest.mark.parametrize("lock", ["secrets", "roster"])
def test_account_writer_lock_expires_without_a_write(
    host: Path,
    monkeypatch: pytest.MonkeyPatch,
    lock: str,
) -> None:
    import fcntl
    import threading

    monkeypatch.setattr(owner_roster, "ROSTER_LOCK_WAIT", 0.05, raising=False)
    path = host / "spawn.toml"
    make_default_account(path, "claude", "first")
    before = path.read_bytes()
    directory = host / "secrets/claude"
    tokens = {p.name: p.read_bytes() for p in directory.iterdir()}
    target = directory if lock == "secrets" else host / ".spawn.toml.lock"
    descriptor = os.open(target, os.O_RDONLY)
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    failures: list[Exception] = []

    def write() -> None:
        try:
            write_account_key(path, "claude", "work", KEY, new=True)
        except (owner_roster.RosterError, OSError) as error:
            failures.append(error)

    writer = threading.Thread(target=write, daemon=True)
    try:
        writer.start()
        writer.join(0.5)
        assert not writer.is_alive(), (
            "The account writer did not meet its lock deadline"
        )
        assert len(failures) == 1 and isinstance(failures[0], RosterEditRefused)
        assert "busy" in str(failures[0]) and "Retry" in str(failures[0])
        assert path.read_bytes() == before
        assert {p.name: p.read_bytes() for p in directory.iterdir()} == tokens
    finally:
        os.close(descriptor)
        writer.join(5)
