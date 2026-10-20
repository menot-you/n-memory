"""`pytest.mark.needs`: a test names the host capabilities it runs on.

A missing one skips with its name; `NOTT_TEST_REQUIRE=all` turns that skip into
a failure, so a host that owes every capability (SERVER) never reads a skipped
measurement as a clean one; an unknown name is refused at collection. The plugin is `tests/needs.py`, which
`tests/conftest.py` registers; a pytester run names it with `-p needs`.
"""

from __future__ import annotations

import needs
import pytest

pytest_plugins = ["pytester"]

#: The inner runs load the plugin by name, as the conftest loads it for this suite.
PLUGIN = ("-p", "needs")

BODY = """
import pytest

@pytest.mark.needs("cgroup", "bwrap")
def test_host():
    pass
"""


@pytest.fixture
def absent_bwrap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(needs, "PLATFORM", "linux")
    monkeypatch.setitem(needs.PROBES, "bwrap", lambda: False)
    monkeypatch.setitem(needs.PROBES, "cgroup", lambda: True)


def test_an_unknown_capability_is_refused_at_collection(
    pytester: pytest.Pytester,
) -> None:
    pytester.makepyfile(
        """
        import pytest

        @pytest.mark.needs("gpu")
        def test_host():
            pass
        """
    )
    result = pytester.runpytest(*PLUGIN)
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*unknown host capability 'gpu'*"])


def test_a_missing_capability_skips_with_its_name(
    pytester: pytest.Pytester, absent_bwrap: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NOTT_TEST_REQUIRE", raising=False)
    pytester.makepyfile(BODY)
    result = pytester.runpytest(*PLUGIN, "-rs")
    result.assert_outcomes(skipped=1)
    result.stdout.fnmatch_lines(["*host capability missing: bwrap*"])


def test_require_all_turns_the_skip_into_a_failure(
    pytester: pytest.Pytester, absent_bwrap: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOTT_TEST_REQUIRE", "all")
    pytester.makepyfile(BODY)
    result = pytester.runpytest(*PLUGIN)
    # Refused in setup, so pytest counts it an error: the run is red either way.
    result.assert_outcomes(errors=1)
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines(["*host capability missing: bwrap*"])


def test_present_capabilities_run_the_test(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(needs.PROBES, "bwrap", lambda: True)
    monkeypatch.setitem(needs.PROBES, "cgroup", lambda: True)
    monkeypatch.setenv("NOTT_TEST_REQUIRE", "all")
    pytester.makepyfile(BODY)
    pytester.runpytest(*PLUGIN).assert_outcomes(passed=1)


def test_an_unknown_require_value_is_refused(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOTT_TEST_REQUIRE", "some")
    pytester.makepyfile(BODY)
    assert pytester.runpytest(*PLUGIN).ret == pytest.ExitCode.USAGE_ERROR


LINUX_ONLY = """
import pytest

@pytest.mark.needs("cgroup")
def test_host():
    pass
"""


def test_require_all_skips_a_capability_this_platform_does_not_owe(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A Mac owes no cgroup: under REQUIRE=all the test skips naming why, it does not fail.
    monkeypatch.setattr(needs, "PLATFORM", "darwin")
    monkeypatch.setitem(needs.PROBES, "cgroup", lambda: False)
    monkeypatch.setenv("NOTT_TEST_REQUIRE", "all")
    pytester.makepyfile(LINUX_ONLY)
    result = pytester.runpytest(*PLUGIN, "-rs")
    result.assert_outcomes(skipped=1)
    result.stdout.fnmatch_lines(
        ["*host capability missing: cgroup (not owed on darwin)*"]
    )


def test_require_all_fails_a_capability_this_platform_owes(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(needs, "PLATFORM", "darwin")
    monkeypatch.setitem(needs.PROBES, "launchd_coalition", lambda: False)
    monkeypatch.setenv("NOTT_TEST_REQUIRE", "all")
    pytester.makepyfile(
        """
        import pytest

        @pytest.mark.needs("launchd_coalition")
        def test_host():
            pass
        """
    )
    result = pytester.runpytest(*PLUGIN)
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*host capability missing: launchd_coalition*"])


def test_every_capability_names_the_platforms_that_owe_it() -> None:
    assert set(needs.OWED) == set(needs.PROBES)
    assert all(owed and owed <= {"linux", "darwin"} for owed in needs.OWED.values())
