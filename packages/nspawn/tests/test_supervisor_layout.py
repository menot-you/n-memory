"""The original supervisor cases remain collectable across loadfile groups."""

import hashlib
import subprocess
import sys
import tomllib
from collections import Counter
from pathlib import Path


def test_supervisor_cases_remain_distributed_and_complete() -> None:
    member = Path(__file__).resolve().parents[1]
    files = sorted(
        str(path.relative_to(member))
        for path in (member / "tests").glob("test_supervisor*.py")
        if path.name != Path(__file__).name
    )
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", *files],
        cwd=member,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    cases = [line for line in result.stdout.splitlines() if line.startswith("tests/")]
    names = sorted(line.split("::", 1)[1] for line in cases)
    # Update deliberately when the suite adds/removes a case or parameter instance.
    # Drift since #691's hash, all merged: b8699f7bc renamed two claim cases;
    # #704 +5 (and committed its pre-merge hash); #760 +2; #766 +1, #777 -1; #772 +1;
    # spawn-mutation-cure +3 (the writable-execute survivors close case, the
    # concurrent preparation sibling close case and the catalog cap behind a
    # slower inspector); main-reds +1 (the close that still refuses a live child
    # whose identity is refused); p3-nspawn-sqlite renamed three store-failure
    # cases whose names still said PostgreSQL (count unchanged); law-delivery +12
    # (2026-09-26: a child that appends the law silences the plugin's copy, twelve
    # parameter instances, eight of them proving the roster's own file decides and
    # a dispatch that quotes the law's title silences nothing); child-final-state
    # +2 (the child env bound to the session it launches, fresh and resumed);
    # nspawn-close-race +5 (a close of a child the row never published, in
    # `spawning` and after the driver's own `failed`; its live-stranger,
    # poisoned-claim and published-identity counterparts); nspawn-fenced-close
    # +5 (a close with no child refuses a row that gained an identity, keeps a
    # poisoned claim without a slice, releases it once the slice is gone, and
    # accepts a close another process landed; a closed row keeps a poisoned
    # claim without a slice); e3a-nspawn-custody +1 (a served close of a run
    # the retired body-accepted mode left with a pending recovery is a typed
    # refusal); nspawn-served-store-error +2 (a store failure in a served close
    # is an error frame, busy and sqlite); p0-role-binding +2 (the listing states
    # each route's kind and implemented capabilities, and carries `[roles]`);
    # pc-claude-review +2 (a Claude route proxied through `[defaults.env]` or an
    # account's `base_url` lists no cage); review-profile +3 (the listing carries
    # `[role_profile]`; a credential retry keeps the caller's model request, omitted
    # and explicit) +2 (and its effort request, with and without a class) +2 (a
    # retry the next account refuses names that refusal; the class reaches it) +3
    # (an account-env refusal on that retry; a caged review admitted beside a
    # roster env literal, an uncaged run refused by it) +2 (and a proxied review
    # refused by it; a codex review never judged by Claude-only roster args);
    # writer-profile +1 (provider report carries validated writer model/effort);
    # nspawn-wall-known-reset +5 in test_supervisor_quota.py (a refresh from any
    # source reconciles stored usage; an older cached reading, or a fresh probe
    # of an older snapshot, never overwrites a newer one; a future `updatedAt`
    # never outranks an honest reading; no wall is derived below 100%);
    # nspawn-simplify-r1 -2 (the two CodexBar snapshot cases left with the reader);
    # quota-menu-every-provider +2 in test_supervisor_quota.py (a refresh with no
    # source for the provider serves what its runs recorded; a refresh starts no
    # pull once its time is spent); nspawn-e2e-defects +2 in
    # test_supervisor_close.py (a close before the child's first turn names the
    # phase it stopped; closing a finished run invents no failure), and the
    # failover account-model case renamed by its new refusal text (count unchanged)
    # +2 (a cleanup failure after a refused or a landed completion never replaces
    # it, in test_supervisor_drive.py; a refused failure keeps its reason through
    # the exhaustion note, in test_supervisor_failover.py); catalog-deadline +5 in
    # test_supervisor_close.py (catalog and binding preparation beyond the default
    # cap, expiry killing the catalog child, binding RPCs on the run deadline, and
    # remaining-deadline accounting after prior setup); node-account-rotation +1 in
    # test_supervisor_failover.py (c1377e7d4: exhausted accounts keep the warning
    # when another route excludes an unknown account); nspawn-sovereign-prep +3
    # in test_supervisor_close.py (the 60 s preparation cap, and a cap expiry
    # without a caller deadline is a startup timeout, at the app-server and at
    # the stored reason), none dropped; nspawn-sovereign review-wait +1 in
    # test_supervisor.py (a5d408319: a result show with a timeout waits
    # for the answer); nspawn-sovereign-fix1 +1 in test_supervisor_status.py (a
    # result wait that ends first names the running phase); nspawn-picker-followup
    # +1 in test_supervisor_failover.py (a failover goes where the shared picker
    # sends it, not to the next declared account).
    # Stage 3 includes the current telemetry and status contracts.
    assert len(names) == 329
    assert hashlib.sha256("\n".join(names).encode()).hexdigest() == (
        "df81772e896c99a496ee182d96c30fed962623e8dd1fce91b91223f8dd1737a3"
    )
    # Stock mutmut judges a mutant only by tests its stats run recorded, and that
    # run collects exactly `pytest_add_cli_args_test_selection` (plus
    # `pytest_add_cli_args`). A supervisor file outside that collection would let
    # a supervisor mutant be judged by tests that never ran, and a live suite
    # inside it would judge mutants as a unit oracle. So pytest itself is asked
    # what the member's own table selects, not a restatement of its rule.
    table = tomllib.loads((member / "pyproject.toml").read_text(encoding="utf-8"))[
        "tool"
    ]["mutmut"]
    selected = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--rootdir=.",
            "--collect-only",
            "-q",
            *table["pytest_add_cli_args_test_selection"],
            *table["pytest_add_cli_args"],
        ],
        cwd=member,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    oracles = {
        line.split("::", 1)[0]
        for line in selected.stdout.splitlines()
        if line.startswith("tests/")
    }
    assert set(files) <= oracles, sorted(set(files) - oracles)
    live = sorted(path for path in oracles if path.startswith("tests/live/"))
    assert live == [], live
    groups = Counter(line.split("::", 1)[0] for line in cases)
    assert max(groups.values()) <= len(cases) // 2, groups
