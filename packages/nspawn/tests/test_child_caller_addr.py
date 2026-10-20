"""The child learns WHO spawned it, so it can answer without being told.

Measured 2026-08-31: the only way a child could message its caller was for the
dispatching prompt to paste the address by hand. The supervisor already has it
for free — it inherits `CLAUDE_CODE_MESSAGING_SOCKET` from the session that
started it (observed in `/proc/<supervisor pid>/environ`).

The caller's own `CLAUDE_CODE_MESSAGING_SOCKET` already reaches the child by
plain ambient inheritance, and it is LEFT ALONE: claude rebinds it to its own
pid (observed — three children answered from `<their pid>.sock` while carrying
the parent's value), and stripping a variable the binary may read for its socket
NAMESPACE would risk silencing every child to tidy one name. What is added is
the RENAMED copy, which survives that rebind and is what ENV LAW requires of a
variable this repository invents.
"""

from __future__ import annotations

import pytest

from nspawn import child
from nspawn.owner_roster import Provider


@pytest.fixture(autouse=True)
def _credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """The credential has to live in the REAL environment, not in `ambient`.

    Measured by the adversarial review of this very candidate: these two tests
    passed only because a human kept typing `NOTT_TEST_CALLER_KEY=k` in front of
    pytest. `_credential_from_auth_env` (`child.py:206`) reads
    `os.environ.get(provider.auth_env)` and ignores the `ambient` mapping its
    own caller threads down to it, so `build_child_env` refused before ever
    reaching the line these tests exist to measure — a deterministic RED under
    the member gate, which runs each file in its own process with no such
    prefix.

    DEBT, NOT FIXED HERE: `_read_credential_bytes` accepts `ambient` and passes
    it along, then this one leaf reads the process environment instead. That
    asymmetry is pre-existing and outside this candidate's write set; it is
    named in the pull request, not invaded.
    """
    monkeypatch.setenv("NOTT_TEST_CALLER_KEY", "k")


_PLAIN = Provider(name="testroute", auth_env="NOTT_TEST_CALLER_KEY")
_BASE = {"PATH": "/usr/bin", "HOME": "/home/test-user", "NOTT_TEST_CALLER_KEY": "k"}


def test_the_child_carries_the_caller_address_under_its_own_name() -> None:
    env, _ = child.build_child_env(
        _PLAIN,
        None,
        base_environ=dict(_BASE),
        ambient={
            **_BASE,
            "CLAUDE_CODE_MESSAGING_SOCKET": "/run/user/1000/cc-socks/7.sock",
        },
    )
    assert env[child.CALLER_ADDR_ENV] == "uds:/run/user/1000/cc-socks/7.sock"


def test_no_caller_session_means_no_variable_not_an_empty_one() -> None:
    """An empty address reads as an address and would be sent to. Absent is absent."""
    env, _ = child.build_child_env(
        _PLAIN, None, base_environ=dict(_BASE), ambient=dict(_BASE)
    )
    assert child.CALLER_ADDR_ENV not in env
