"""Writing a provider's accounts: reorder and remove, the rest of the file kept.

The app's quota menu makes an account the default or takes one out of the roster.
Only the roster changes: the account's home and its credential stay where they are,
every byte the owner wrote outside those accounts stays as he wrote it, and the file
as it was is kept beside it before a byte moves.
"""

from __future__ import annotations

import os
import stat
import threading
from pathlib import Path
from typing import Any

import pytest
import sample_roster
from sample_roster import TEXT as SAMPLE

from nspawn import owner_roster
from nspawn.config import MAX_STORE_CONFIG_BYTES
from nspawn.owner_roster import (
    NativeAccount,
    Roster,
    RosterEditRefused,
    RosterError,
    make_default_account,
    remove_account,
    write_accounts,
)


def _copy(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "spawn.toml"
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def _backups(path: Path) -> list[Path]:
    return sorted(path.parent.glob(f"{path.name}.bak-*"))


def _accounts(path: Path, provider: str) -> dict[str, NativeAccount]:
    found = Roster.read(path).select(provider)
    assert found is not None
    return found.accounts


def _left(tmp_path: Path) -> list[str]:
    return sorted(p.name for p in tmp_path.iterdir())


def test_the_default_is_the_first_account_the_picker_tries(tmp_path: Path) -> None:
    roster = Roster.read(sample_roster.write(tmp_path))
    qwen, claude = roster.select("qwen"), roster.select("claude")
    assert qwen is not None and claude is not None
    # Declared order, not the alphabet: the roster names the 3090 first.
    assert qwen.ordered_accounts() == ["server", "m5"]
    assert claude.ordered_accounts() == claude.discovered_accounts()


def test_reordering_moves_each_account_table_whole_with_its_comments(
    tmp_path: Path,
) -> None:
    text = SAMPLE
    path = _copy(tmp_path, text)
    before = _accounts(path, "qwen")
    write_accounts(path, "qwen", ["m5", "server"])
    after = path.read_text(encoding="utf-8")
    assert list(_accounts(path, "qwen")) == ["m5", "server"]
    assert _accounts(path, "qwen") == before
    # The comment above the server's table travels with it, below m5's.
    lead = "# `bootstrap` is argv, never a string"
    assert after.index("[provider.accounts.m5]") < after.index(lead)
    assert after.index(lead) < after.index("[provider.accounts.server]")
    # Everything outside qwen's accounts is the same text.
    head = text[: text.index(lead)]
    assert after.startswith(head)
    tail = text[text.index("# m1 is NOT in.") :]
    assert after.endswith(tail)
    written = [line for line in after.splitlines() if line.strip()]
    assert sorted(written) == sorted(line for line in text.splitlines() if line.strip())


def test_removing_an_account_drops_its_table_and_nothing_else(tmp_path: Path) -> None:
    text = SAMPLE
    path = _copy(tmp_path, text)
    before = Roster.read(path)
    write_accounts(path, "qwen", ["server"])
    after = path.read_text(encoding="utf-8")
    assert list(_accounts(path, "qwen")) == ["server"]
    assert "[provider.accounts.m5]" not in after and "11434" not in after
    # The note on m1 belongs to the provider, not to the table it followed.
    assert "# m1 is NOT in." in after and "## ----- System1" in after
    rest = Roster.read(path)
    assert [p for p in rest.providers if p.name != "qwen"] == [
        p for p in before.providers if p.name != "qwen"
    ]


def test_a_backup_keeps_the_file_byte_for_byte_and_its_mode(tmp_path: Path) -> None:
    # CRLF line ends: a text read hands back LF, and a backup written from it is not
    # the file that was there.
    raw = SAMPLE.encode().replace(b"\n", b"\r\n")
    path = tmp_path / "spawn.toml"
    path.write_bytes(raw)
    os.chmod(path, 0o600)
    backup = write_accounts(path, "qwen", ["m5", "server"])
    assert backup is not None and _backups(path) == [backup]
    assert backup.read_bytes() == raw
    assert b"\n" not in path.read_bytes().replace(b"\r\n", b"")
    for kept in (path, backup):
        assert stat.S_IMODE(kept.stat().st_mode) == 0o600
    # Two edits in the same second keep two backups.
    second = write_accounts(path, "qwen", ["server", "m5"])
    assert second != backup and len(_backups(path)) == 2
    assert path.read_bytes() == raw


def test_discovered_accounts_become_declared_and_keep_their_credentials(
    tmp_path: Path,
) -> None:
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    for alias in ("alpha", "beta", "gamma"):
        (secrets / f"{alias}.token").write_text(f"fixture-{alias}\n")
    path = _copy(
        tmp_path,
        "# the owner's note\n"
        f'[[provider]]\nname = "claude"\nsecrets = "{secrets}"\nmodel = "m"\n\n'
        f'[[provider]]\nname = "fugu"\nkind = "claude"\nsecrets = "{secrets}-fugu"\n'
        'auth_env = "ANTHROPIC_AUTH_TOKEN"\n\n[provider.env]\nANTHROPIC_BASE_URL = "https://fugu.invalid"\n',
    )
    (tmp_path / "secrets-fugu").mkdir()
    for alias in ("max", "pro"):
        (tmp_path / f"secrets-fugu/{alias}.token").write_text(f"fixture-{alias}\n")
    write_accounts(path, "claude", ["gamma", "alpha"])
    write_accounts(path, "fugu", ["pro", "max"])
    roster = Roster.read(path)
    claude, fugu = roster.select("claude"), roster.select("fugu")
    assert claude is not None and fugu is not None
    # The variable each account was already delivered under (`_deliver_credential`).
    oauth = NativeAccount(home=None, auth_env="CLAUDE_CODE_OAUTH_TOKEN")
    assert claude.accounts == {"gamma": oauth, "alpha": oauth}
    assert list(claude.accounts) == ["gamma", "alpha"]
    gateway = NativeAccount(home=None, auth_env="ANTHROPIC_AUTH_TOKEN")
    assert fugu.accounts == {"pro": gateway, "max": gateway}
    assert list(fugu.accounts) == ["pro", "max"] and fugu.auth_env is None
    assert fugu.env == {"ANTHROPIC_BASE_URL": "https://fugu.invalid"}
    # The same token file each alias read before; none moved or went away.
    assert claude.resolve_token_file("gamma") == secrets / "gamma.token"
    assert sorted(p.name for p in secrets.iterdir()) == [
        "alpha.token",
        "beta.token",
        "gamma.token",
    ]
    assert path.read_text(encoding="utf-8").startswith("# the owner's note\n")


@pytest.mark.parametrize(
    "accounts",
    [
        (
            'accounts = { one = { home = "~/.codex-one" }, '
            'two = { home = "~/.codex-two" } }  # the owner\'s note\n'
        ),
        'accounts = [\n  "~/.codex-one",\n  "~/.codex-two",\n]\n',
        'accounts.one.home = "/one"\naccounts.two.home = "/two"\n',
        '\n[provider.accounts]\none = { home = "/one" }\ntwo = { home = "/two" }\n',
    ],
    ids=["inline", "homes", "dotted", "one-table"],
)
def test_accounts_not_each_in_their_own_table_are_refused_unwritten(
    tmp_path: Path, accounts: str
) -> None:
    text = (
        '[bins]\ncodex = "/usr/bin/codex"\n\n'
        '[[provider]]\nname = "codex"\nkind = "codex"\nmodel = "gpt"\n'
        + accounts
        + '\n[roles]\nreviewer = "codex"\n'
    )
    path = _copy(tmp_path, text)
    alias = max(_accounts(path, "codex"))
    with pytest.raises(RosterEditRefused, match="own"):
        write_accounts(path, "codex", [alias])
    assert path.read_text(encoding="utf-8") == text and _backups(path) == []


def test_a_moved_table_keeps_keys_the_loader_does_not_read(tmp_path: Path) -> None:
    text = (
        '[[provider]]\nname = "qwen"\nkind = "claude"\nsecrets = "/s"\n'
        'note = """\nfirst\n\n\nafter two blank lines\n"""\n\n'
        '[provider.accounts."a.b"]\nbase_url = "http://127.0.0.1:1"  # the 3090\n'
        'auth_env = "T"\nmachine = "server"\n\n'
        '[provider.accounts."a.b".env]\nW = "1"\n\n'
        '[provider.accounts.c]\nbase_url = "http://127.0.0.1:2"\nauth_env = "T"\n'
    )
    path = _copy(tmp_path, text)
    before = _accounts(path, "qwen")
    write_accounts(path, "qwen", ["c", "a.b"])
    assert list(_accounts(path, "qwen")) == ["c", "a.b"]
    assert _accounts(path, "qwen") == before
    after = path.read_text(encoding="utf-8")
    assert 'machine = "server"' in after and "# the 3090" in after
    assert '"""\nfirst\n\n\nafter two blank lines\n"""' in after
    assert after.index("[provider.accounts.c]") < after.index(
        '[provider.accounts."a.b"]'
    )


def test_a_provider_auth_env_with_a_comment_is_refused_unwritten(
    tmp_path: Path,
) -> None:
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    for alias in ("max", "pro"):
        (secrets / f"{alias}.token").write_text("fixture\n")
    text = (
        f'[[provider]]\nname = "fugu"\nsecrets = "{secrets}"\n'
        'auth_env = "ANTHROPIC_AUTH_TOKEN"  # the gateway reads this one\n'
    )
    path = _copy(tmp_path, text)
    with pytest.raises(RosterEditRefused, match="plain line"):
        write_accounts(path, "fugu", ["pro", "max"])
    assert path.read_text(encoding="utf-8") == text and _backups(path) == []


@pytest.mark.parametrize(
    ("provider", "aliases", "reason"),
    [
        ("nobody", ["server"], "declares no provider"),
        ("qwen", [], "keeps"),
        ("qwen", ["server", "server"], "keeps"),
        ("qwen", ["server", "m1"], "keeps"),
    ],
)
def test_a_list_that_is_not_its_own_accounts_is_refused_unwritten(
    tmp_path: Path, provider: str, aliases: list[str], reason: str
) -> None:
    text = SAMPLE
    path = _copy(tmp_path, text)
    with pytest.raises(RosterEditRefused, match=reason):
        write_accounts(path, provider, aliases)
    assert path.read_text(encoding="utf-8") == text and _backups(path) == []


def test_a_rewrite_that_changes_anything_else_is_refused_unwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A valid rewrite that also changes a key outside the accounts: only the
    # comparison with the document as it was can see it.
    text = SAMPLE
    path = _copy(tmp_path, text)
    move = owner_roster._moved_tables

    def stray(*args: Any) -> str:
        moved: str = move(*args)
        return moved.replace('model_fast = "qwen3.8-27b"', 'model_fast = "other"')

    monkeypatch.setattr(owner_roster, "_moved_tables", stray)
    with pytest.raises(RosterEditRefused, match="nothing was written"):
        write_accounts(path, "qwen", ["m5", "server"])
    assert path.read_text(encoding="utf-8") == text and _backups(path) == []
    assert _left(tmp_path) == [".spawn.toml.lock", "spawn.toml"]


def test_a_rewrite_its_own_reader_refuses_is_not_written(tmp_path: Path) -> None:
    # Declaring discovered accounts grows the file past the reader's ceiling.
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    for n in range(40):
        (secrets / f"account-{n:02}.token").write_text("fixture\n")
    head = f'[[provider]]\nname = "claude"\nsecrets = "{secrets}"\n'
    text = "# " + "x" * (MAX_STORE_CONFIG_BYTES - len(head) - 100) + "\n" + head
    path = _copy(tmp_path, text)
    Roster.read(path)
    with pytest.raises(RosterEditRefused, match="its own reader"):
        write_accounts(path, "claude", [f"account-{n:02}" for n in reversed(range(40))])
    assert path.read_text(encoding="utf-8") == text and _backups(path) == []
    assert _left(tmp_path) == [".spawn.toml.lock", "secrets", "spawn.toml"]


@pytest.mark.parametrize("failing", [1, 2], ids=["staging", "backup"])
def test_a_file_whose_creation_failed_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing: int
) -> None:
    text = SAMPLE
    path = _copy(tmp_path, text)
    calls: list[int] = []
    sync = os.fsync

    def fsync(descriptor: int) -> None:
        calls.append(descriptor)
        if len(calls) == failing:
            raise OSError(5, "Input/output error")
        sync(descriptor)

    monkeypatch.setattr(os, "fsync", fsync)
    with pytest.raises(RosterError, match="could not be written"):
        write_accounts(path, "qwen", ["m5", "server"])
    assert path.read_text(encoding="utf-8") == text
    assert _left(tmp_path) == [".spawn.toml.lock", "spawn.toml"]


def test_a_file_that_changed_while_edited_is_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = SAMPLE
    path = _copy(tmp_path, text)
    move = owner_roster._moved_tables

    def by_hand(*args: Any) -> str:
        path.write_text(text + "\n# edited by hand meanwhile\n", encoding="utf-8")
        moved: str = move(*args)
        return moved

    monkeypatch.setattr(owner_roster, "_moved_tables", by_hand)
    with pytest.raises(RosterEditRefused, match="changed while"):
        write_accounts(path, "qwen", ["m5", "server"])
    assert path.read_text(encoding="utf-8").endswith("# edited by hand meanwhile\n")
    assert _backups(path) == []


def test_two_writers_take_turns_and_neither_edit_is_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    for alias in ("alpha", "beta", "gamma"):
        (secrets / f"{alias}.token").write_text("fixture\n")
    path = _copy(tmp_path, f'[[provider]]\nname = "claude"\nsecrets = "{secrets}"\n')
    first = path.read_bytes()
    replace = os.replace
    second = threading.Thread(target=remove_account, args=(path, "claude", "gamma"))

    def late(source: Path, target: Path) -> None:
        # The first writer has compared and is about to replace: the second runs
        # its whole edit now, unless something makes it wait.
        if second.ident is None:
            second.start()
            second.join(timeout=1)
        replace(source, target)

    monkeypatch.setattr(os, "replace", late)
    remove_account(path, "claude", "beta")
    second.join(timeout=10)
    assert not second.is_alive()
    assert list(_accounts(path, "claude")) == ["alpha"]
    backups = _backups(path)
    assert len(backups) == 2 and first in [b.read_bytes() for b in backups]


def test_a_roster_that_is_not_utf8_is_a_roster_error(tmp_path: Path) -> None:
    path = tmp_path / "spawn.toml"
    path.write_bytes(b'[[provider]]\nname = "caf\xe9"\n')
    with pytest.raises(RosterError, match="not UTF-8"):
        Roster.read(path)
    with pytest.raises(RosterError, match="not UTF-8"):
        write_accounts(path, "claude", ["alpha"])


def test_making_a_default_and_removing_an_account(tmp_path: Path) -> None:
    text = SAMPLE
    path = _copy(tmp_path, text)
    assert make_default_account(path, "qwen", "server") is None
    assert _backups(path) == []
    with pytest.raises(RosterEditRefused, match="server is the default of qwen"):
        remove_account(path, "qwen", "server")
    with pytest.raises(RosterEditRefused, match="qwen has no account 'm1'"):
        make_default_account(path, "qwen", "m1")
    assert make_default_account(path, "qwen", "m5") is not None
    assert remove_account(path, "qwen", "server") is not None
    assert list(_accounts(path, "qwen")) == ["m5"]


def test_a_linked_roster_is_written_through_its_link(tmp_path: Path) -> None:
    real = _copy(tmp_path, SAMPLE)
    link = tmp_path / "linked.toml"
    link.symlink_to(real)
    write_accounts(link, "qwen", ["m5", "server"])
    assert link.is_symlink() and list(_accounts(real, "qwen")) == ["m5", "server"]


def test_a_roster_gone_while_edited_is_a_roster_error_with_nothing_left(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _copy(tmp_path, SAMPLE)
    move = owner_roster._moved_tables

    def gone(*args: Any) -> str:
        path.unlink()
        moved: str = move(*args)
        return moved

    monkeypatch.setattr(owner_roster, "_moved_tables", gone)
    with pytest.raises(RosterError, match="could not be written"):
        write_accounts(path, "qwen", ["m5", "server"])
    assert _left(tmp_path) == [".spawn.toml.lock"]
