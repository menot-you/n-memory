"""A token provider's accounts follow its secrets directory (owner, 2026-10-21).

The owner put `tiago-no-tt.token` in `~/.nott/secrets/claude`, and nSPAWN did not
take it: a provider that declared its accounts read none of its token files. Now
each `<alias>.token` file is an account on the next roster read, with no roster
edit. A declared table gives its account settings and a place, a file that goes
takes its account with it, and an excluded account stays excluded. A file that
cannot be an account is skipped with a note the owner sees, and never stops the
roster. Every roster and secrets directory here is under tmp_path, never the live
ones.
"""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

import pytest

from nspawn import child, owner_roster, picker, route, supervisor
from nspawn.child import ChildEnvironmentError
from nspawn.owner_roster import (
    Provider,
    Roster,
    RosterEditRefused,
    make_default_account,
    remove_account,
    write_account_key,
)
from nspawn.picker import PickerError, pick_execution

NOW = 1_000_000

ROSTER = """\
[bins]
codex = "/bin/true"

[[provider]]
name = "claude"
kind = "claude"
model = "claude-opus-5-5"
secrets = "{root}/claude"{excluded}

[provider.accounts.first]
auth_env = "CLAUDE_CODE_OAUTH_TOKEN"
capacity = 3

[provider.accounts.second]
auth_env = "CLAUDE_CODE_OAUTH_TOKEN"

[[provider]]
name = "spare"
kind = "claude"
model = "claude-opus-5-5"
secrets = "{root}/spare"

[provider.accounts.extra]
auth_env = "CLAUDE_CODE_OAUTH_TOKEN"

[[provider]]
name = "qwen"
kind = "claude"
model = "qwen"
secrets = "{root}/qwen"

[provider.accounts.server]
base_url = "http://127.0.0.1:1"
auth_env = "ANTHROPIC_AUTH_TOKEN"

[[provider]]
name = "codex"
kind = "codex"
model = "gpt"
excluded_accounts = ["work"]

[provider.accounts.main]
home = "{root}/codex-main"

[provider.accounts.work]
home = "{root}/codex-work"
{tail}"""


def _write(root: Path, excluded: str = "", tail: str = "") -> Path:
    path = root / "spawn.toml"
    path.write_text(ROSTER.format(root=root, excluded=excluded, tail=tail))
    path.chmod(0o600)
    return path


def _token(root: Path, alias: str, provider: str = "claude") -> Path:
    """A token file as `claude setup-token` and the Add flow leave it: 0600."""
    path = root / provider / f"{alias}.token"
    path.write_text("sk\n")
    path.chmod(0o600)
    return path


def _claude(root: Path) -> Provider:
    found = Roster.read(root / "spawn.toml").select("claude")
    assert found is not None
    return found


def _outside(root: Path, name: str, mode: int) -> Path:
    """A key file outside every secrets directory, as the qwen service keeps one."""
    elsewhere = root / "elsewhere"
    elsewhere.mkdir(exist_ok=True)
    target = elsewhere / name
    target.write_text("sk-outside\n")
    target.chmod(mode)
    return target


def _notes(provider: Provider | None) -> str:
    assert provider is not None
    return "; ".join(owner_roster.account_notes(provider))


@pytest.fixture
def root(tmp_path: Path) -> Path:
    for provider in ("claude", "spare", "qwen"):
        (tmp_path / provider).mkdir()
        (tmp_path / provider).chmod(0o700)  # yours alone whatever the umask
    _token(tmp_path, "first")
    _token(tmp_path, "second")
    _token(tmp_path, "extra", provider="spare")
    _token(tmp_path, "server", provider="qwen")
    _write(tmp_path)
    return tmp_path


def test_a_token_file_the_roster_does_not_declare_is_an_account_on_the_next_read(
    root: Path,
) -> None:
    path = root / "spawn.toml"
    before = path.read_bytes()
    assert _claude(root).ordered_accounts() == ["first", "second"]
    _token(root, "tiago-no-tt")
    claude = _claude(root)
    assert claude.ordered_accounts() == ["first", "second", "tiago-no-tt"]
    # What `nspawn provider list` reports, and what a caller may name as a route.
    assert claude.discovered_accounts() == ["first", "second", "tiago-no-tt"]
    named = Roster.read(path).select("tiago-no-tt")
    assert named is not None and named.name == "claude"
    # The table the Add account flow writes: the provider's token variable.
    assert claude.accounts["tiago-no-tt"].auth_env == "CLAUDE_CODE_OAUTH_TOKEN"
    assert claude.resolve_token_file("tiago-no-tt") == root / "claude/tiago-no-tt.token"
    assert path.read_bytes() == before


def test_a_declared_table_keeps_its_settings_and_its_place(root: Path) -> None:
    _token(root, "aaa")
    claude = _claude(root)
    # A new name that sorts first is never the default.
    assert claude.ordered_accounts() == ["first", "second", "aaa"]
    assert claude.accounts["first"].capacity == 3
    picked = pick_execution(
        Roster.read(root / "spawn.toml"), NOW, requested_provider="claude"
    )
    assert picked.executed_account == "first"


def test_a_token_file_that_goes_takes_its_account_with_it(root: Path) -> None:
    _token(root, "tiago-no-tt")
    assert "tiago-no-tt" in _claude(root).ordered_accounts()
    (root / "claude/tiago-no-tt.token").unlink()
    # The roster entry wins: a declared account stays without its file, and
    # an unreadable directory never takes the declared accounts with it.
    (root / "claude/second.token").unlink()
    assert _claude(root).ordered_accounts() == ["first", "second"]


def test_an_excluded_token_file_stays_excluded(root: Path) -> None:
    _write(root, excluded='\nexcluded_accounts = ["tiago-no-tt"]')
    _token(root, "tiago-no-tt")
    roster = Roster.read(root / "spawn.toml")
    with pytest.raises(PickerError, match='"tiago-no-tt" is excluded'):
        pick_execution(roster, NOW, requested_provider="tiago-no-tt")
    assert pick_execution(
        roster, NOW, requested_provider="claude"
    ).executed_account == ("first")


def test_an_endpoint_provider_takes_no_token_file_it_does_not_declare(
    root: Path,
) -> None:
    """An endpoint account needs its base_url, which no file name can give."""
    _token(root, "m5", provider="qwen")
    qwen = Roster.read(root / "spawn.toml").select("qwen")
    assert qwen is not None and qwen.ordered_accounts() == ["server"]


def test_a_token_file_named_after_a_provider_is_no_account(root: Path) -> None:
    """A selector takes the provider for that name, never the account."""
    _token(root, "qwen")
    _token(root, "claude")
    claude = _claude(root)
    assert claude.ordered_accounts() == ["first", "second"]
    assert "qwen.token" in _notes(claude) and "claude.token" in _notes(claude)


def test_a_token_file_another_provider_has_is_no_account_and_stops_nothing(
    root: Path,
) -> None:
    """`server` is qwen's, `extra` is spare's, and `both` is in two directories.
    Each made `Roster.read` refuse the whole roster, every route with it."""
    _token(root, "server")
    _token(root, "extra")
    _token(root, "both")
    _token(root, "both", provider="spare")
    roster = Roster.read(root / "spawn.toml")
    claude, spare = roster.select("claude"), roster.select("spare")
    assert claude is not None and spare is not None
    assert claude.ordered_accounts() == ["first", "second"]
    assert spare.ordered_accounts() == ["extra"]
    for name in ("server.token", "extra.token", "both.token"):
        assert name in _notes(claude)
    assert "both.token" in _notes(spare)
    selected = roster.select("server")
    assert selected is not None and selected.name == "qwen"
    picked = pick_execution(roster, NOW, requested_provider="claude")
    assert picked.executed_account == "first"
    # What `nspawn provider list` shows the owner.
    listing = supervisor._provider_listing(claude, roster)
    assert any("server.token" in note for note in listing.warnings)


def test_a_token_file_whose_name_no_run_row_takes_is_no_account(root: Path) -> None:
    """Admission refuses an account outside the run-row grammar (rows.py)."""
    _token(root, "Tiago")
    _token(root, "a.b")
    claude = _claude(root)
    assert claude.ordered_accounts() == ["first", "second"]
    notes = _notes(claude)
    assert "account name" in notes
    # Such a name may be a pasted token, so no note repeats it.
    assert "Tiago" not in notes and "a.b" not in notes


def test_an_unsafe_token_file_is_no_account_and_a_note(root: Path) -> None:
    _token(root, "empty").write_text("")
    (root / "claude/link.token").symlink_to(_outside(root, "two-links.txt", 0o600))
    os.link(root / "elsewhere/two-links.txt", root / "elsewhere/second-name.txt")
    os.link(_token(root, "twin"), root / "twin-elsewhere")
    (root / "claude/big.token").write_bytes(b"x" * (64 * 1024 + 1))
    claude = _claude(root)
    assert claude.ordered_accounts() == ["first", "second"]
    for name in ("empty.token", "link.token", "twin.token", "big.token"):
        assert name in _notes(claude)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def test_a_token_file_made_by_hand_under_umask_022_is_tightened_and_joins(
    root: Path,
) -> None:
    """Every credential reader here takes a key at 0600 (vendor_home.py). A file
    made by hand under umask 022 is 0644: nSPAWN sets it to 0600 and says so. A
    declared account's own file is set the same way."""
    by_hand = _token(root, "by-hand")
    by_hand.chmod(0o644)
    (root / "claude/first.token").chmod(0o640)
    claude = _claude(root)
    assert claude.ordered_accounts() == ["first", "second", "by-hand"]
    assert _mode(by_hand) == 0o600 and _mode(root / "claude/first.token") == 0o600
    notes = owner_roster.account_notes(claude)
    assert "tightened by-hand.token to 0600" in notes
    assert "tightened first.token to 0600" in notes
    # Once at 0600, nothing changes and nothing is noted.
    assert owner_roster.account_notes(_claude(root)) == []


def test_a_token_file_nspawn_cannot_set_to_0600_does_not_join(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refused(descriptor: int, mode: int) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "fchmod", refused)
    by_hand = _token(root, "by-hand")
    by_hand.chmod(0o644)
    claude = _claude(root)
    assert claude.ordered_accounts() == ["first", "second"]
    assert _mode(by_hand) == 0o644
    assert "chmod 600" in _notes(claude) and "by-hand.token" in _notes(claude)


def test_a_token_file_whose_name_is_not_utf8_is_no_account_and_stops_nothing(
    root: Path,
) -> None:
    """Such a name raised UnicodeEncodeError, which stopped every dispatch."""
    raw = os.fsencode(root / "claude") + b"/\xf1.token"
    try:
        descriptor = os.open(raw, os.O_WRONLY | os.O_CREAT, 0o600)
    except OSError as error:
        if error.errno != errno.EILSEQ:
            raise
        pytest.skip("this file system refuses a name that is not UTF-8 (APFS)")
    os.write(descriptor, b"sk\n")
    os.close(descriptor)
    roster = Roster.read(root / "spawn.toml")
    claude = roster.select("claude")
    assert claude is not None and claude.ordered_accounts() == ["first", "second"]
    notes = _notes(claude)
    assert "account name" in notes and "\udcf1" not in notes
    notes.encode("utf-8")
    picked = pick_execution(roster, NOW, requested_provider="claude")
    assert picked.executed_account == "first"


def test_the_notes_on_a_run_are_bounded(root: Path) -> None:
    """Every run carries them, and a run warning holds at most 16 KiB."""
    for number in range(300):
        _token(root, f"empty-{number}").write_text("")
    roster = Roster.read(root / "spawn.toml")
    claude = roster.select("claude")
    assert claude is not None and len(owner_roster.account_notes(claude)) == 300
    picked = pick_execution(roster, NOW, requested_provider="claude")
    assert picked.warning is not None and "more" in picked.warning
    assert len(picked.warning.encode("utf-8")) <= 4096


def test_a_secrets_directory_others_can_reach_follows_no_file(root: Path) -> None:
    _token(root, "tiago-no-tt")
    (root / "claude").chmod(0o755)
    claude = _claude(root)
    assert claude.ordered_accounts() == ["first", "second"]
    assert "0700" in _notes(claude)


def test_a_preference_may_name_a_followed_account(root: Path) -> None:
    path = _write(root, tail='\n[preference]\nreview = ["tiago-no-tt"]\n')
    _token(root, "tiago-no-tt")
    assert Roster.read(path).preferences["review"] == [("tiago-no-tt", None)]


def test_the_editors_act_on_a_followed_account(root: Path) -> None:
    path = root / "spawn.toml"
    _token(root, "tiago-no-tt")
    _token(root, "zed")
    make_default_account(path, "claude", "tiago-no-tt")
    text = path.read_text()
    # Only the account that moves is declared; zed stays a followed file.
    assert "[provider.accounts.tiago-no-tt]" in text
    assert "[provider.accounts.zed]" not in text
    assert _claude(root).ordered_accounts() == ["tiago-no-tt", "first", "second", "zed"]
    with pytest.raises(RosterEditRefused, match="token file of zed"):
        remove_account(path, "claude", "zed")


def test_a_pasted_key_adds_its_account_once(root: Path) -> None:
    """The new file is followed before the Add flow declares it: still one account."""
    path = root / "spawn.toml"
    write_account_key(path, "claude", "newone", "sk-fixture-key", new=True)
    assert path.read_text().count("[provider.accounts.newone]") == 1
    assert _claude(root).ordered_accounts() == ["first", "second", "newone"]


def test_an_excluded_name_that_names_no_account_is_in_the_run_warning(
    root: Path,
) -> None:
    """A name left behind when its account went refused every dispatch to the
    provider (2026-10-21). It excludes nothing, the route still runs, and the
    owner sees the name in the run warning and the provider list."""
    _write(root, excluded='\nexcluded_accounts = ["gone"]')
    roster = Roster.read(root / "spawn.toml")
    picked = pick_execution(roster, NOW, requested_provider="claude")
    assert picked.executed_account == "first"
    assert picked.warning is not None and "gone" in picked.warning
    claude = roster.select("claude")
    assert claude is not None
    listing = supervisor._provider_listing(claude, roster)
    assert any("gone" in note for note in listing.warnings)


def test_removing_an_account_takes_it_out_of_excluded_accounts(root: Path) -> None:
    path = _write(root)
    remove_account(path, "codex", "work")
    codex = Roster.read(path).select("codex")
    assert codex is not None
    assert codex.ordered_accounts() == ["main"]
    assert codex.excluded_accounts == []


def test_removing_an_account_its_token_file_keeps_is_refused_unwritten(
    root: Path,
) -> None:
    """The file would bring the account back on the next read."""
    path = root / "spawn.toml"
    before = path.read_bytes()
    with pytest.raises(RosterEditRefused, match="token file"):
        remove_account(path, "claude", "second")
    assert path.read_bytes() == before


# --- round 4: one fenced reader for a Claude token file --------------------------


def test_the_child_and_the_route_read_a_token_file_only_through_the_fence(
    root: Path,
) -> None:
    """Both readers read the file by path with no check (child.py, route.py)."""
    claude = _claude(root)
    first = root / "claude/first.token"
    first.chmod(0o644)
    with pytest.raises(ChildEnvironmentError, match=r"first\.token.*chmod 600"):
        child._read_credential_bytes(claude, "first", {})
    with pytest.raises(route.RouteRefused, match=r"first\.token.*chmod 600"):
        route.account_token(claude, "first", "subscription account", "unavailable")
    first.chmod(0o600)
    assert child._read_credential_bytes(claude, "first", {}) == b"sk\n"
    second = root / "claude/second.token"
    second.unlink()
    second.symlink_to(_outside(root, "open.txt", 0o644))
    with pytest.raises(ChildEnvironmentError, match=r"second\.token.*chmod 600"):
        child._read_credential_bytes(claude, "second", {})


def test_a_provider_with_no_accounts_table_lists_only_fenced_token_files(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "fugu"
    directory.mkdir()
    directory.chmod(0o700)
    for alias in ("good", "open", "twin"):
        (directory / f"{alias}.token").write_text("sk\n")
        (directory / f"{alias}.token").chmod(0o600)
    (directory / "open.token").chmod(0o644)
    os.link(directory / "twin.token", tmp_path / "twin-elsewhere")
    (directory / "link.token").symlink_to(_outside(tmp_path, "open.txt", 0o644))
    fugu = Provider(name="fugu", kind="claude", secrets=directory)
    assert fugu.discovered_accounts() == ["good"]


def test_tightening_clears_only_the_group_and_other_bits(root: Path) -> None:
    """A 0400 key stays 0400; a 0444 one becomes 0400, not 0600."""
    read_only = _token(root, "read-only")
    read_only.chmod(0o444)
    claude = _claude(root)
    assert "read-only" in claude.ordered_accounts()
    assert _mode(read_only) == 0o400
    assert "tightened read-only.token to 0400" in owner_roster.account_notes(claude)


def test_a_mode_that_does_not_change_is_a_failure(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """vfat takes fchmod and keeps the mode: nSPAWN reads the mode back."""
    monkeypatch.setattr(os, "fchmod", lambda descriptor, mode: None)
    _token(root, "by-hand").chmod(0o644)
    claude = _claude(root)
    assert "by-hand" not in claude.ordered_accounts()
    assert "by-hand.token" in _notes(claude) and "tightened" not in _notes(claude)


@pytest.mark.parametrize(
    ("number", "advice"),
    [
        (errno.EPERM, True),
        (errno.EACCES, True),
        (errno.EROFS, True),
        (errno.EIO, False),
    ],
)
def test_a_failed_tightening_keeps_its_errno_and_advises_chmod_only_when_it_helps(
    root: Path, monkeypatch: pytest.MonkeyPatch, number: int, advice: bool
) -> None:
    def refused(descriptor: int, mode: int) -> None:
        raise OSError(number, os.strerror(number))

    monkeypatch.setattr(os, "fchmod", refused)
    _token(root, "by-hand").chmod(0o644)
    notes = _notes(_claude(root))
    assert errno.errorcode[number] in notes
    assert ("chmod 600" in notes) is advice


@pytest.mark.parametrize(
    ("number", "says"), [(errno.ENOENT, "went away"), (errno.ELOOP, "link")]
)
def test_a_file_that_goes_or_becomes_a_link_while_read_has_its_own_note(
    root: Path, monkeypatch: pytest.MonkeyPatch, number: int, says: str
) -> None:
    real_open = os.open

    def racing(path: os.PathLike[str] | str, flags: int, *rest: int) -> int:
        if str(path).endswith("racing.token"):
            raise OSError(number, os.strerror(number))
        return real_open(path, flags, *rest)

    _token(root, "racing").chmod(0o644)
    monkeypatch.setattr(os, "open", racing)
    claude = _claude(root)
    assert "racing" not in claude.ordered_accounts()
    assert says in _notes(claude) and "chmod 600" not in _notes(claude)


def test_a_tightened_note_leads_the_run_notes_and_the_tail_never_counts_it(
    root: Path,
) -> None:
    """The provider list never shows a tightened note again: the file is 0600."""
    for number in range(300):
        _token(root, f"empty-{number}").write_text("")
    _token(root, "zz-by-hand").chmod(0o644)
    roster = Roster.read(root / "spawn.toml")
    picked = pick_execution(roster, NOW, requested_provider="claude")
    assert picked.warning is not None
    assert "tightened zz-by-hand.token to 0600" in picked.warning
    shown = picked.warning.count("empty-")
    assert f"{300 - shown} more notes in nspawn provider list" in picked.warning


# --- round 5: a link the owner keeps in the 0700 directory -------------------------


def test_an_owner_link_in_the_0700_directory_reads_its_fenced_target(
    root: Path,
) -> None:
    """The qwen service keeps its key elsewhere, and the link lets it follow."""
    server = root / "qwen/server.token"
    server.unlink()
    server.symlink_to(_outside(root, "api_key.txt", 0o600))
    qwen = Roster.read(root / "spawn.toml").select("qwen")
    assert qwen is not None
    assert child._read_credential_bytes(qwen, "server", {}) == b"sk-outside\n"
    token = route.account_token(qwen, "server", "qwen account", "unavailable")
    assert token == "sk-outside"


@pytest.mark.parametrize("case", ["directory", "open", "two-links", "not-regular"])
def test_a_link_is_refused_unless_its_directory_and_its_target_pass(
    root: Path, case: str
) -> None:
    server = root / "qwen/server.token"
    server.unlink()
    target = _outside(root, "api_key.txt", 0o644 if case == "open" else 0o600)
    if case == "two-links":
        os.link(target, root / "elsewhere/second-name.txt")
    if case == "not-regular":
        target = root / "elsewhere"
    server.symlink_to(target)
    if case == "directory":
        (root / "qwen").chmod(0o755)
    qwen = Roster.read(root / "spawn.toml").select("qwen")
    assert qwen is not None
    with pytest.raises(ChildEnvironmentError) as refused:
        child._read_credential_bytes(qwen, "server", {})
    message = str(refused.value)
    assert "server.token" in message and "elsewhere" not in message
    assert ("chmod 600" in message) is (case == "open")
    # The reader never changes a target outside the fenced directory.
    if case == "open":
        assert _mode(target) == 0o644


def test_a_followed_link_joins_only_with_an_owner_only_target_it_never_changes(
    root: Path,
) -> None:
    (root / "claude/linked.token").symlink_to(_outside(root, "key.txt", 0o600))
    open_target = _outside(root, "open.txt", 0o644)
    (root / "claude/open-link.token").symlink_to(open_target)
    claude = _claude(root)
    assert claude.ordered_accounts() == ["first", "second", "linked"]
    notes = _notes(claude)
    assert "open-link.token" in notes and "chmod 600" in notes
    assert "elsewhere" not in notes and _mode(open_target) == 0o644
    assert child._read_credential_bytes(claude, "linked", {}) == b"sk-outside\n"


def test_a_provider_with_no_accounts_table_lists_an_owner_link_to_a_fenced_target(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "fugu"
    directory.mkdir()
    directory.chmod(0o700)
    (directory / "linked.token").symlink_to(_outside(tmp_path, "key.txt", 0o600))
    fugu = Provider(name="fugu", kind="claude", secrets=directory)
    assert fugu.discovered_accounts() == ["linked"]
    directory.chmod(0o755)
    assert fugu.discovered_accounts() == []


# --- a token link: never followed at the file, followed in its directory path -----


def _qwen_link(root: Path, target: Path | str) -> Provider:
    server = root / "qwen/server.token"
    server.unlink()
    server.symlink_to(target)
    qwen = Roster.read(root / "spawn.toml").select("qwen")
    assert qwen is not None
    return qwen


def test_a_link_follows_exactly_one_hop(root: Path) -> None:
    hop = root / "elsewhere/hop.txt"
    target = _outside(root, "api_key.txt", 0o600)
    hop.symlink_to(target)
    qwen = _qwen_link(root, hop)
    with pytest.raises(ChildEnvironmentError, match=r"server\.token.*another link"):
        child._read_credential_bytes(qwen, "server", {})
    with pytest.raises(route.RouteRefused, match=r"server\.token.*another link"):
        route.account_token(qwen, "server", "qwen account", "unavailable")


def test_a_link_whose_target_directory_others_can_write_is_refused(root: Path) -> None:
    target = _outside(root, "api_key.txt", 0o600)
    (root / "elsewhere").chmod(0o775)
    qwen = _qwen_link(root, target)
    with pytest.raises(ChildEnvironmentError) as refused:
        child._read_credential_bytes(qwen, "server", {})
    message = str(refused.value)
    assert "server.token" in message and "go-w" in message
    assert "elsewhere" not in message and str(root) not in message


def test_a_link_to_a_relative_path_is_refused(root: Path) -> None:
    _outside(root, "api_key.txt", 0o600)
    qwen = _qwen_link(root, "../elsewhere/api_key.txt")
    with pytest.raises(ChildEnvironmentError, match=r"server\.token.*absolute"):
        child._read_credential_bytes(qwen, "server", {})


def test_the_qwen_layout_passes_with_a_0755_target_directory(root: Path) -> None:

    target = _outside(root, "api_key.txt", 0o600)
    (root / "elsewhere").chmod(0o755)
    qwen = _qwen_link(root, target)
    assert child._read_credential_bytes(qwen, "server", {}) == b"sk-outside\n"


def test_a_followed_or_listed_link_follows_one_hop_too(root: Path) -> None:
    hop = root / "elsewhere/hop.txt"
    hop.symlink_to(_outside(root, "key.txt", 0o600))
    (root / "claude/hop.token").symlink_to(hop)
    claude = _claude(root)
    assert "hop" not in claude.ordered_accounts()
    assert "hop.token" in _notes(claude) and "another link" in _notes(claude)
    fugu = Provider(name="fugu", kind="claude", secrets=root / "claude")
    assert "hop" not in fugu.discovered_accounts()


def test_a_provider_with_no_accounts_table_lists_no_name_outside_the_grammar(
    tmp_path: Path,
) -> None:
    """A pasted key as a file name is never an account, and an open directory
    lists nothing, as the notes of a read say."""
    directory = tmp_path / "fugu"
    directory.mkdir()
    directory.chmod(0o700)
    for name in ("good.token", "Sk-Pasted-Key.token"):
        (directory / name).write_text("sk\n")
        (directory / name).chmod(0o600)
    fugu = Provider(name="fugu", kind="claude", secrets=directory)
    assert fugu.discovered_accounts() == ["good"]
    directory.chmod(0o755)
    assert fugu.discovered_accounts() == []


def test_the_child_refusal_names_the_token_file_and_its_fix_not_the_host_path(
    root: Path,
) -> None:
    claude = _claude(root)
    (root / "claude/first.token").chmod(0o644)
    with pytest.raises(ChildEnvironmentError) as refused:
        child._read_credential_bytes(claude, "first", {})
    message = str(refused.value)
    assert "first.token" in message and "chmod 600" in message
    assert str(root) not in message


def test_the_key_writer_refuses_a_hard_linked_token_of_a_declared_account(
    root: Path,
) -> None:
    """_put_key's st_nlink guard: the account is declared, so it still exists.
    The reader refuses the file with the fence's own text for more than one link."""
    first = root / "claude/first.token"
    other = root / "copy-of-first"
    os.link(first, other)
    with pytest.raises(RosterEditRefused, match="change it where it points"):
        write_account_key(root / "spawn.toml", "claude", "first", "sk-new", new=False)
    assert first.read_text() == other.read_text() == "sk\n"
    claude = _claude(root)
    with pytest.raises(ChildEnvironmentError) as refused:
        child._read_credential_bytes(claude, "first", {})
    assert "first.token has more than one link: copy it to a file of its own" in str(
        refused.value
    )


# --- round 7: one typed refusal, the right fix, one lookup of the target directory ---


def test_a_link_into_a_missing_directory_says_so_with_its_errno(root: Path) -> None:
    qwen = _qwen_link(root, root / "missing-directory/api_key.txt")
    with pytest.raises(ChildEnvironmentError) as refused:
        child._read_credential_bytes(qwen, "server", {})
    message = str(refused.value)
    assert "server.token" in message and "ENOENT" in message
    assert "not there" in message and "go-w" not in message


def test_a_secrets_directory_nspawn_cannot_search_names_chmod_700_on_it(
    root: Path,
) -> None:
    """EACCES from lstat or readlink comes from a directory on the path, never
    from the file's own mode."""
    directory = root / "qwen"
    directory.chmod(0o600)
    try:
        with pytest.raises(owner_roster.TokenFileRefused) as refused:
            owner_roster.read_token_file(directory / "server.token")
    finally:
        directory.chmod(0o700)
    message = refused.value.message
    assert "server.token" in message and "EACCES" in message
    assert "chmod 700" in message and "chmod 600" not in message


def test_links_in_the_target_directory_path_stay_followed(root: Path) -> None:
    """O_NOFOLLOW covers only the target file itself."""
    _outside(root, "api_key.txt", 0o600)
    (root / "elsewhere-link").symlink_to(root / "elsewhere")
    qwen = _qwen_link(root, root / "elsewhere-link/api_key.txt")
    assert child._read_credential_bytes(qwen, "server", {}) == b"sk-outside\n"


def test_dropped_tightened_notes_get_a_count_of_their_own() -> None:
    shown = picker._run_notes([f"tightened t{n}.token to 0600" for n in range(200)])
    assert "more token files tightened" in shown
    assert "provider list" not in shown


# --- round 8: the target directory needs the owner's read and search bits ----------


def test_a_target_directory_you_cannot_search_is_refused_with_chmod_u_rx(
    root: Path,
) -> None:
    """A 0600 directory opens, but nothing in it can be looked up."""
    target = _outside(root, "api_key.txt", 0o600)
    (root / "claude/linked.token").symlink_to(target)
    qwen = _qwen_link(root, target)
    (root / "elsewhere").chmod(0o600)
    try:
        with pytest.raises(ChildEnvironmentError) as refused:
            child._read_credential_bytes(qwen, "server", {})
        notes = _notes(_claude(root))
    finally:
        (root / "elsewhere").chmod(0o700)
    message = str(refused.value)
    assert "server.token" in message and "chmod u+rx" in message
    assert "chmod 600" not in message and "secrets directory" not in message
    assert "linked.token" in notes and "chmod u+rx" in notes
    assert "chmod 700" not in notes


@pytest.mark.parametrize(
    ("case", "errno_name", "fix"),
    [
        ("not-a-directory", "ENOTDIR", "check the link's target path"),
        ("loop", "ELOOP", "check the link's target path"),
        ("no-read", "EACCES", "chmod u+rx"),
    ],
)
def test_a_target_directory_nspawn_cannot_open_names_its_errno_and_its_fix(
    root: Path, case: str, errno_name: str, fix: str
) -> None:
    file = _outside(root, "api_key.txt", 0o600)
    if case == "not-a-directory":
        target = file / "inner"
    elif case == "loop":
        (root / "loop").symlink_to(root / "loop")
        target = root / "loop/api_key.txt"
    else:
        target = file
    qwen = _qwen_link(root, target)
    if case == "no-read":
        (root / "elsewhere").chmod(0o100)
    try:
        with pytest.raises(ChildEnvironmentError) as refused:
            child._read_credential_bytes(qwen, "server", {})
    finally:
        (root / "elsewhere").chmod(0o700)
    message = str(refused.value)
    assert "server.token" in message and errno_name in message and fix in message
    if case != "no-read":
        assert "chmod" not in message


@pytest.mark.parametrize(
    ("number", "says"),
    [
        (errno.EACCES, "cannot reach it (EACCES), so run chmod 700 on the directory"),
        (errno.EIO, "cannot read it (EIO), so check the file system that holds it"),
    ],
)
def test_the_two_unreadable_listing_notes(
    root: Path, monkeypatch: pytest.MonkeyPatch, number: int, says: str
) -> None:
    real_lstat = os.lstat

    def failing(
        path: os.PathLike[str] | str, *rest: object, **keywords: object
    ) -> os.stat_result:
        if str(path).endswith("odd.token"):
            raise OSError(number, os.strerror(number))
        return real_lstat(path, *rest, **keywords)  # type: ignore[arg-type]

    _token(root, "odd")
    monkeypatch.setattr(os, "lstat", failing)
    notes = _notes(_claude(root))
    assert f"odd.token is not an account: nSPAWN {says}" in notes
